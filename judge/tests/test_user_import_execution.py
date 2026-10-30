"""M4 confirmation, one account transaction, retries and durable recovery."""

import json
import time
import uuid
from datetime import timedelta
from io import StringIO
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import OperationalError, close_old_connections, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import UserImportBatch, UserImportOperation
from judge.tasks.user_import import execute_user_import, validate_user_import
from judge.tests.test_managed_password_login import BACKEND
from judge.tests.test_user_import_models import IMPORT_SETTINGS
from judge.tests.test_user_import_preview import PreviewData
from judge.tests.test_user_import_service import NEW_PASSWORD
from judge.utils.user_import import UserImportError
from judge.utils.user_import_execution import claim_import_execution, execution_failure, reconcile_import_execution, \
    repair_import_cache, reserve_import_submission
from judge.utils.user_import_maintenance import cleanup_import_batches, recover_import_batches
from judge.utils.user_import_progress import read_import_progress, write_import_progress
from judge.utils.user_import_service import execute_import_batch
from judge.views.user_import import owned_batch, serialize_batch


class ExecutionData(PreviewData):
    def queued(self, rows=None, muted=True):
        batch = self.stage(rows or [
            ['new_execution', NEW_PASSWORD, 'New name', '', '', 'org_a', 'true'],
        ], muted=muted)
        return reserve_import_submission(self.admin, batch.pk, batch.version, batch.idempotency_key)[0]

    def due(self, batch):
        UserImportBatch.objects.filter(pk=batch.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))

    def run_execution(self, batch):
        execute_user_import.run(str(batch.pk), batch.version)
        batch.refresh_from_db()
        return batch


@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[], LANGUAGE_CODE='en')
class ImportExecutionAPITests(ExecutionData, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.admin, backend=BACKEND)
        self.assertEqual(self.client.get(reverse('user_import')).status_code, 200)
        self.csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        published = patch('judge.tasks.user_import.execute_user_import.apply_async')
        self.published = published.start()
        self.addCleanup(published.stop)

    def submit(self, batch, key=None, **changes):
        return self.client.post(reverse('user_import_submit'), json.dumps({
            'batch_id': str(batch.pk), 'version': batch.version, 'confirmed': True, **changes,
        }), content_type='application/json', HTTP_X_CSRFTOKEN=self.csrf,
            HTTP_IDEMPOTENCY_KEY=str(key or batch.idempotency_key))

    def ready_batch(self):
        return self.validate(self.reserve())

    def test_confirm_queues_server_snapshot_without_account_writes(self):
        batch = self.ready_batch()
        before = self.account_state()
        response = self.submit(batch)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['batch']['status'], 'QUEUED')
        self.assertEqual(response.json()['batch']['execution_attempts'], 0)
        self.assertEqual(self.account_state(), before)
        self.assertEqual(batch.operations.filter(action='submit').count(), 1)
        self.published.assert_called_once()
        self.assertEqual(self.published.call_args.kwargs['args'], [str(batch.pk), 1])
        self.assertNotIn(NEW_PASSWORD, response.content.decode() + str(self.published.call_args))

    def test_duplicate_confirm_and_refresh_do_not_allocate_attempts_or_new_jobs(self):
        batch = self.ready_batch()
        for _ in range(3):
            self.assertEqual(self.submit(batch).status_code, 202)
        restored = self.client.get(reverse('user_import') + '?batch=' + str(batch.pk),
                                   HTTP_ACCEPT='application/json')
        self.assertEqual(restored.json()['batch']['status'], 'QUEUED')
        batch.refresh_from_db()
        self.assertEqual((batch.execution_attempts, batch.operations.filter(action='submit').count()), (0, 1))
        self.published.assert_called_once()

    def test_broker_failure_preserves_confirmed_batch_and_same_key_can_dispatch_it(self):
        batch = self.ready_batch()
        self.published.side_effect = ConnectionError(NEW_PASSWORD)
        with self.assertLogs('judge.tasks.user_import', level='WARNING') as logs:
            response = self.submit(batch)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['batch']['status'], 'QUEUED')
        self.assertTrue(response.json()['batch']['execution_dispatch_pending'])
        self.assertNotIn(NEW_PASSWORD, response.content.decode() + str(logs.output))
        self.published.side_effect = None
        self.assertEqual(self.submit(batch).status_code, 202)
        self.assertEqual(batch.operations.filter(action='submit').count(), 1)
        batch.refresh_from_db()
        self.assertEqual(batch.execution_attempts, 0)

    def test_confirmation_requires_original_key_version_review_and_unmodified_snapshot(self):
        batch = self.ready_batch()
        self.assertEqual(self.submit(batch, key=uuid.uuid4()).status_code, 409)
        self.assertEqual(self.submit(batch, version=2).status_code, 409)
        self.assertEqual(self.submit(batch, confirmed=False).status_code, 422)
        self.assertEqual(self.submit(batch, rows=[]).status_code, 422)
        row = batch.rows.get()
        row.preview['mute']['after'] = False
        row.save(update_fields=['preview'])
        self.assertEqual(self.submit(batch).status_code, 409)
        self.assertFalse(batch.operations.filter(action='submit').exists())
        self.published.assert_not_called()

    def test_other_active_batch_blocks_confirmation_without_closing_preview(self):
        batch = self.ready_batch()
        self.reserve(self.new_csv('other_pending'))
        self.assertEqual(self.submit(batch).status_code, 409)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'PREVIEW_READY')

    def test_expired_confirmation_is_rejected_but_submitted_deadline_is_separate(self):
        batch = self.ready_batch()
        UserImportBatch.objects.filter(pk=batch.pk).update(preview_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.submit(batch).status_code, 409)
        self.assertFalse(batch.operations.filter(action='submit').exists())

    def test_completed_replay_keeps_password_changed_after_import_and_new_key_is_rejected(self):
        batch = self.ready_batch()
        self.assertEqual(self.submit(batch).status_code, 202)
        self.apply(batch)
        created = User.objects.get(username='new_preview')
        created.set_password('Changed-after-the-import-2026')
        created.save(update_fields=['password'])
        before = self.account_state()
        response = self.submit(batch)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['batch']['status'], 'COMPLETED')
        self.assertTrue(response.json()['replayed'])
        self.assertEqual(self.submit(batch, key=uuid.uuid4()).status_code, 409)
        self.assertEqual(self.account_state(), before)
        self.published.assert_called_once()

    def test_report_cleanup_retains_upload_and_submit_tombstones(self):
        batch = self.ready_batch()
        self.submit(batch)
        self.apply(batch)
        UserImportBatch.objects.filter(pk=batch.pk).update(finished_at=timezone.now() - timedelta(days=31))
        result = cleanup_import_batches()
        self.assertEqual(result['reports_removed'], 1)
        self.assertFalse(batch.rows.exists())
        before = self.account_state()
        response = self.submit(batch)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['batch']['applied_rows'], 1)
        self.assertTrue(response.json()['batch']['report_expired'])
        self.assertEqual(response.json()['batch']['rows'], [])
        replay = self.client.post(reverse('user_import_post_file'), {
            'csv_file': SimpleUploadedFile('same.csv', self.new_csv()), 'muted': 'true',
        }, HTTP_X_CSRFTOKEN=self.csrf, HTTP_IDEMPOTENCY_KEY=str(batch.idempotency_key))
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()['batch']['id'], str(batch.pk))
        self.assertEqual(UserImportOperation.objects.filter(batch=batch).count(), 2)
        self.assertFalse(batch.rows.exists())
        self.assertEqual(self.account_state(), before)

    def test_poll_with_stale_running_marker_never_exposes_success_before_completion(self):
        batch = self.queued()
        self.claim(batch)
        batch.refresh_from_db()
        stale = UserImportBatch.objects.get(pk=batch.pk)
        execute_import_batch(self.admin, batch.pk, 1, batch.attempt_token)
        with patch('judge.views.user_import.owned_batch', return_value=stale):
            response = self.client.get(reverse('user_import_status', args=[batch.pk]))
        payload = response.json()['batch']
        self.assertEqual((payload['status'], payload['applied_rows']), ('RUNNING', 0))
        self.assertEqual(payload['rows'][0]['status'], 'VALID')
        self.assertEqual(payload['rows'][0]['result'], {})
        final = self.client.get(reverse('user_import_status', args=[batch.pk])).json()['batch']
        self.assertEqual((final['status'], final['applied_rows']), ('COMPLETED', 1))


@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[], LANGUAGE_CODE='en')
class ImportExecutionTaskTests(ExecutionData, TransactionTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        publisher = patch('judge.tasks.user_import.execute_user_import.apply_async')
        self.publisher = publisher.start()
        self.addCleanup(publisher.stop)

    def mixed(self):
        return self.queued([
            ['new_first', NEW_PASSWORD, '', '', '', 'org_a', 'true'],
            ['existing', NEW_PASSWORD, 'Imported name', '', '', 'org_b', 'true'],
            ['new_last', NEW_PASSWORD, '', '', '', 'org_b', 'false'],
        ])

    def test_one_commit_success_and_duplicate_worker_only_repairs_cache(self):
        batch = self.mixed()
        self.run_execution(batch)
        self.assertEqual((batch.status, batch.applied_rows, batch.created_users, batch.updated_users),
                         ('COMPLETED', 3, 2, 1))
        self.assertEqual(batch.execution_attempts, 1)
        self.assertEqual(batch.errors, [])
        self.assertEqual(batch.rows.filter(status='SUCCESS').count(), 3)
        self.assertFalse(batch.rows.exclude(password_ciphertext='', prepared_password_ciphertext='').exists())
        before = self.account_state()
        self.run_execution(batch)
        self.assertEqual(batch.execution_attempts, 1)
        self.assertEqual(self.account_state(), before)

    def test_late_python_failure_rolls_back_all_changes_and_reports_safe_line(self):
        batch = self.mixed()
        before = self.account_state()
        from judge.utils.user_import_service import _apply_row

        def fail(actor, current, row, *args):
            result = _apply_row(actor, current, row, *args)
            if row.row_number == 2:
                raise RuntimeError(NEW_PASSWORD)
            return result

        with patch('judge.utils.user_import_service._apply_row', side_effect=fail):
            self.run_execution(batch)
        self.assertEqual((batch.status, batch.applied_rows), ('FAILED', 0))
        self.assertEqual(batch.errors[0]['row'], 2)
        self.assertEqual(batch.errors[0]['line'], 3)
        self.assertNotIn(NEW_PASSWORD, json.dumps(batch.errors))
        self.assertEqual(self.account_state(), before)
        self.assertEqual(batch.rows.filter(status='NOT_APPLIED').count(), 3)
        self.assertFalse(batch.rows.exclude(prepared_password_ciphertext='').exists())

    def test_transient_failure_retries_whole_batch_with_db_counter_and_original_hashes(self):
        batch = self.mixed()
        before = self.account_state()
        prepared = list(batch.rows.values_list('prepared_password_ciphertext', flat=True))
        with patch('judge.utils.user_import_service._apply_row', side_effect=OperationalError(1213, NEW_PASSWORD)):
            self.run_execution(batch)
        self.assertEqual((batch.status, batch.applied_rows, batch.execution_attempts), ('QUEUED', 0, 1))
        self.assertEqual(self.account_state(), before)
        self.assertEqual(list(batch.rows.values_list('prepared_password_ciphertext', flat=True)), prepared)
        self.assertGreater(self.publisher.call_args.kwargs['countdown'], 0)
        self.due(batch)
        self.run_execution(batch)
        self.assertEqual((batch.status, batch.applied_rows, batch.execution_attempts), ('COMPLETED', 3, 2))

    def test_budget_survives_delivery_and_http_replay_and_stops_after_three_attempts(self):
        batch = self.mixed()
        before = self.account_state()
        for expected in (1, 2, 3):
            self.due(batch)
            with patch('judge.utils.user_import_service._apply_row', side_effect=OperationalError(1205, 'Timeout')):
                self.run_execution(batch)
            self.assertEqual(batch.execution_attempts, expected)
            same, replayed = reserve_import_submission(self.admin, batch.pk, 1, batch.idempotency_key)
            self.assertTrue(replayed)
            self.assertEqual(same.execution_attempts, expected)
        self.assertEqual((batch.status, batch.applied_rows), ('FAILED', 0))
        self.assertEqual(batch.errors[0]['code'], 'attempts_exhausted')
        self.assertEqual(self.account_state(), before)
        self.run_execution(batch)
        self.assertEqual(batch.execution_attempts, 3)
        self.assertEqual(self.publisher.call_count, 2)

    def test_lost_commit_ack_reads_completed_marker_and_never_reapplies(self):
        batch = self.mixed()

        def lose_ack(*args, **kwargs):
            commit = connection.commit

            def committed():
                commit()
                raise OperationalError(2013, 'Lost COMMIT response')

            with patch.object(connection, 'commit', side_effect=committed):
                return execute_import_batch(*args, **kwargs)

        with patch('judge.tasks.user_import.execute_import_batch', side_effect=lose_ack):
            self.run_execution(batch)
        self.assertEqual((batch.status, batch.applied_rows, batch.execution_attempts), ('COMPLETED', 3, 1))
        before = self.account_state()
        self.run_execution(batch)
        self.assertEqual(self.account_state(), before)
        self.publisher.assert_not_called()

    def test_unknown_outcome_at_last_attempt_stays_running_until_primary_reconciliation(self):
        batch = self.mixed()
        UserImportBatch.objects.filter(pk=batch.pk).update(execution_attempts=2)

        def lose_before_commit(*args, **kwargs):
            with patch.object(connection, 'commit', side_effect=OperationalError(2013, 'Unknown commit outcome')):
                return execute_import_batch(*args, **kwargs)

        before = self.account_state()
        with patch('judge.tasks.user_import.execute_import_batch', side_effect=lose_before_commit), \
                patch('judge.tasks.user_import.reconcile_import_execution', return_value='unknown'):
            self.run_execution(batch)
        self.assertEqual((batch.status, batch.execution_attempts, batch.applied_rows), ('RUNNING', 3, 0))
        self.assertEqual(self.account_state(), before)
        self.assertTrue(batch.rows.exclude(prepared_password_ciphertext='').exists())
        self.due(batch)
        self.assertEqual(reconcile_import_execution(batch.pk, 1), 'failed')
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.execution_attempts, batch.applied_rows), ('FAILED', 3, 0))
        self.assertFalse(batch.rows.exclude(prepared_password_ciphertext='').exists())

    def test_primary_unavailable_does_not_infer_rollback_even_when_budget_exhausted(self):
        batch = self.mixed()
        claim_import_execution(batch.pk, 1)
        UserImportBatch.objects.filter(pk=batch.pk).update(execution_attempts=3)
        self.due(batch)
        with patch('judge.utils.user_import_execution.transaction.atomic', side_effect=OperationalError(2006, 'Down')):
            with self.assertLogs('judge.utils.user_import_execution', level='WARNING'):
                self.assertEqual(reconcile_import_execution(batch.pk, 1), 'unknown')
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.execution_attempts), ('RUNNING', 3))

    def test_reconciliation_cannot_reenter_an_open_account_transaction(self):
        batch = self.mixed()
        claim_import_execution(batch.pk, 1)
        self.due(batch)
        with transaction.atomic():
            self.assertEqual(reconcile_import_execution(batch.pk, 1), 'unknown')
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.execution_attempts), ('RUNNING', 1))

    def test_old_attempt_is_fenced_after_recovery_and_cannot_close_the_new_one(self):
        batch = self.mixed()
        old, actor = claim_import_execution(batch.pk, 1)
        self.due(batch)
        self.assertEqual(reconcile_import_execution(batch.pk, 1), 'queued')
        with self.assertRaises(UserImportError):
            execute_import_batch(actor, batch.pk, 1, old.attempt_token)
        self.due(batch)
        new, _ = claim_import_execution(batch.pk, 1)
        self.assertNotEqual(old.attempt_token, new.attempt_token)
        self.assertEqual(reconcile_import_execution(batch.pk, 1, token=old.attempt_token,
                                                    failure=execution_failure(RuntimeError('Old'))), 'superseded')
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.attempt_token), ('RUNNING', new.attempt_token))

    def test_actor_revoked_or_secret_expired_closes_without_account_writes(self):
        for problem in ('actor', 'expiry'):
            with self.subTest(problem=problem):
                batch = self.queued()
                if problem == 'actor':
                    User.objects.filter(pk=self.admin.pk).update(is_superuser=False)
                else:
                    UserImportBatch.objects.filter(pk=batch.pk).update(
                        secret_expires_at=timezone.now() - timedelta(seconds=1))
                before = self.account_state()
                self.run_execution(batch)
                self.assertEqual(batch.status, 'FAILED' if problem == 'actor' else 'EXPIRED')
                self.assertEqual((batch.applied_rows, batch.execution_attempts), (0, 0))
                self.assertEqual(self.account_state(), before)
                User.objects.filter(pk=self.admin.pk).update(is_superuser=True)

    def test_accepted_job_can_start_after_preview_deadline_while_prepared_secret_is_valid(self):
        batch = self.queued()
        UserImportBatch.objects.filter(pk=batch.pk).update(preview_expires_at=timezone.now() - timedelta(seconds=1))
        self.run_execution(batch)
        self.assertEqual((batch.status, batch.applied_rows), ('COMPLETED', 1))

    def test_progress_cache_failure_does_not_rollback_and_cache_repair_never_reissues(self):
        batch = self.mixed()
        broken_cache = Mock()
        broken_cache.set.side_effect = RuntimeError('Cache offline')
        with patch('judge.utils.user_import_progress.caches', {'default': broken_cache}):
            with patch('judge.utils.user_import_execution.invalidate_import_caches',
                       side_effect=RuntimeError('Offline')):
                with self.assertLogs(level='WARNING'):
                    self.run_execution(batch)
        self.assertEqual((batch.status, batch.applied_rows), ('COMPLETED', 3))
        self.assertEqual(batch.errors[0]['code'], 'cache_repair_pending')
        before = self.account_state()
        repair_import_cache(batch.pk, 1)
        batch.refresh_from_db()
        self.assertEqual(batch.errors, [])
        self.assertEqual(self.account_state(), before)
        self.assertEqual(batch.execution_attempts, 1)

    @override_settings(DMOJ_USER_IMPORT_EXECUTION_LEASE_SECONDS=1)
    def test_poll_and_recovery_do_not_steal_an_expired_lease_from_active_transaction(self):
        batch = self.mixed()
        entered, release = Event(), Event()

        def progress(batch_id, token, done, total):
            write_import_progress(batch_id, token, done, total)
            if done == 1:
                entered.set()
                if not release.wait(timeout=10):
                    raise RuntimeError('Test release timed out')

        def work():
            close_old_connections()
            try:
                execute_user_import.run(str(batch.pk), 1)
            finally:
                close_old_connections()

        with patch('judge.tasks.user_import.write_import_progress', side_effect=progress):
            worker = Thread(target=work)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                time.sleep(1.1)
                current = UserImportBatch.objects.get(pk=batch.pk)
                payload = serialize_batch(current)
                self.assertEqual((payload['status'], payload['applied_rows']), ('RUNNING', 0))
                self.assertEqual(payload['progress'], {'processed': 1, 'total': 3, 'committed': False})
                stale = UserImportBatch.objects.get(pk=batch.pk)
                stale.status = 'PREVIEW_READY'
                stale.preview_expires_at = timezone.now() - timedelta(seconds=1)
                original = UserImportBatch.objects.filter
                first = True

                def stale_read(*args, **kwargs):
                    nonlocal first
                    if first:
                        first = False
                        return Mock(first=Mock(return_value=stale))
                    return original(*args, **kwargs)

                started = time.monotonic()
                with patch.object(UserImportBatch.objects, 'filter', side_effect=stale_read):
                    visible = owned_batch(SimpleNamespace(user=self.admin), batch.pk)
                self.assertEqual(visible.status, 'RUNNING')
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(reconcile_import_execution(batch.pk, 1), 'busy')
                fresh = UserImportBatch.objects.get(pk=batch.pk)
                self.assertEqual((fresh.attempt_token, fresh.execution_attempts), (current.attempt_token, 1))
                self.assertFalse(User.objects.filter(username='new_first').exists())
            finally:
                release.set()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.applied_rows, batch.execution_attempts), ('COMPLETED', 3, 1))

    def test_stale_progress_is_ignored_after_token_changes(self):
        batch = self.queued()
        current, _ = claim_import_execution(batch.pk, 1)
        write_import_progress(batch.pk, current.attempt_token, 1, 1)
        self.assertIsNotNone(read_import_progress(current))
        UserImportBatch.objects.filter(pk=batch.pk).update(attempt_token=uuid.uuid4())
        batch.refresh_from_db()
        self.assertIsNone(read_import_progress(batch))

    def test_feature_off_does_not_claim_or_spend_an_attempt(self):
        batch = self.queued()
        with override_settings(DMOJ_USER_IMPORT_ENABLED=False):
            self.run_execution(batch)
        self.assertEqual((batch.status, batch.execution_attempts), ('QUEUED', 0))


@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[], LANGUAGE_CODE='en')
class ImportMaintenanceTests(ExecutionData, TransactionTestCase):
    def test_lost_execution_dispatch_and_validation_dispatch_are_recoverable(self):
        batch = self.queued()
        with patch('judge.tasks.user_import.execute_user_import.apply_async') as publish:
            result = recover_import_batches()
        self.assertEqual(result['dispatched'], 1)
        publish.assert_called_once()
        self.run_execution(batch)
        pending = self.reserve(self.new_csv('validation_recovery'))
        with patch('judge.tasks.user_import.validate_user_import.apply_async') as publish:
            self.assertEqual(recover_import_batches()['dispatched'], 1)
        publish.assert_called_once()
        validate_user_import.run(str(pending.pk), 1)
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'PREVIEW_READY')

    def test_old_delivery_receipt_is_redispatched_without_spending_budget(self):
        batch = self.queued()
        UserImportBatch.objects.filter(pk=batch.pk).update(
            task_id='lost-publish', updated_at=timezone.now() - timedelta(minutes=2),
        )
        with patch('judge.tasks.user_import.execute_user_import.apply_async') as publisher:
            recover_import_batches()
        publisher.assert_called_once()
        batch.refresh_from_db()
        self.assertEqual(batch.execution_attempts, 0)
        self.assertNotEqual(batch.task_id, 'lost-publish')

    def test_expired_validation_claim_is_fenced_and_resumed_with_original_budget(self):
        from judge.utils.user_import_validation import claim_import_validation
        batch = self.reserve()
        old = claim_import_validation(batch.pk, 1)
        self.due(batch)
        with patch('judge.tasks.user_import.validate_user_import.apply_async'):
            recover_import_batches()
        validate_user_import.run(str(batch.pk), 1)
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.validation_attempts), ('PREVIEW_READY', 2))
        self.assertNotEqual(batch.attempt_token, old.attempt_token)

    def test_cleanup_expires_unconfirmed_secrets_even_when_feature_is_off(self):
        batch = self.reserve()
        UserImportBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(hours=1))
        with override_settings(DMOJ_USER_IMPORT_ENABLED=False):
            result = cleanup_import_batches()
        batch.refresh_from_db()
        self.assertEqual((batch.status, result['expired']), ('EXPIRED', 1))
        self.assertFalse(batch.rows.exclude(password_ciphertext='').exists())
        self.assertTrue(batch.operations.exists())

    def test_cleanup_does_not_delete_queued_or_running_rows_before_outcome_is_known(self):
        batch = self.queued()
        claim_import_execution(batch.pk, 1)
        self.due(batch)
        UserImportBatch.objects.filter(pk=batch.pk).update(secret_expires_at=timezone.now() - timedelta(seconds=1))
        with patch('judge.utils.user_import_maintenance.reconcile_import_execution', return_value='unknown'):
            result = cleanup_import_batches()
        self.assertEqual(result['busy'], 1)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'RUNNING')
        self.assertTrue(batch.rows.exclude(prepared_password_ciphertext='').exists())

    def test_commands_dry_run_are_read_only_and_cleanup_is_idempotent(self):
        batch = self.reserve()
        UserImportBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(hours=1))
        before = list(UserImportBatch.objects.values())
        output = StringIO()
        call_command('recover_user_imports', dry_run=True, stdout=output)
        call_command('cleanup_user_imports', dry_run=True, stdout=output)
        self.assertEqual(list(UserImportBatch.objects.values()), before)
        self.assertNotIn(NEW_PASSWORD, output.getvalue())
        self.assertEqual(cleanup_import_batches()['expired'], 1)
        self.assertEqual(cleanup_import_batches()['expired'], 0)


@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[], LANGUAGE_CODE='en')
class ImportSubmissionConcurrencyTests(ExecutionData, TransactionTestCase):
    def test_concurrent_confirmations_and_workers_commit_once(self):
        batch = self.validate(self.reserve())
        barrier = Barrier(2)
        outcomes = []

        def confirm():
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.admin.pk)
                barrier.wait(timeout=5)
                outcomes.append(reserve_import_submission(actor, batch.pk, 1, batch.idempotency_key)[1])
            except UserImportError as error:
                outcomes.append(error.code)
            finally:
                close_old_connections()

        workers = [Thread(target=confirm) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        self.assertIn(False, outcomes)
        self.assertTrue(all(value in (True, False, 'batch_busy') for value in outcomes), outcomes)
        self.assertTrue(reserve_import_submission(self.admin, batch.pk, 1, batch.idempotency_key)[1])
        self.assertEqual(batch.operations.filter(action='submit').count(), 1)
        barriers = Barrier(2)

        def execute():
            close_old_connections()
            try:
                barriers.wait(timeout=5)
                execute_user_import.run(str(batch.pk), 1)
            finally:
                close_old_connections()

        jobs = [Thread(target=execute) for _ in range(2)]
        for worker in jobs:
            worker.start()
        for worker in jobs:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.applied_rows, batch.execution_attempts), ('COMPLETED', 1, 1))
        self.assertEqual(User.objects.filter(username='new_preview').count(), 1)
