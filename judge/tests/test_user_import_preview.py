"""M3: protected memory-only upload, validation and owner-scoped preview."""

import json
import uuid
from datetime import timedelta
from threading import Barrier, Event, Thread
from unittest.mock import PropertyMock, patch

import requests
from celery.exceptions import Retry
from django.conf import settings
from django.contrib import auth
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, close_old_connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from judge.models import Contest, ContestParticipation, Profile, UserImportBatch, UserImportOperation
from judge.tasks.user_import import validate_user_import
from judge.tests.test_managed_password_login import BACKEND
from judge.tests.test_user_import_csv import csv_bytes
from judge.tests.test_user_import_models import IMPORT_SETTINGS
from judge.tests.test_user_import_service import ImportServiceData, NEW_PASSWORD
from judge.utils.managed_login import PENDING_LOGIN_KEY
from judge.utils.user_import import ImportIssue, UserImportError, upload_request_digest
from judge.utils.user_import_storage import prepare_import_password
from judge.utils.user_import_validation import claim_import_validation, finish_import_validation, locked_validation, \
    reserve_import_upload, resolve_validation_rows


class RejectPasswordValidator:
    def validate(self, password, user=None):
        if password.startswith('Reject-'):
            # Custom validators need not have safe exception messages.
            raise ValidationError('Rejected password: ' + password)


class PreviewData(ImportServiceData):
    def new_csv(self, username='new_preview', password=NEW_PASSWORD):
        return csv_bytes([[username, password, 'New name', 'UTC', 'new@example.org', 'org_a', 'true']])

    def reserve(self, data=None, key=None, actor=None, muted=True):
        data = self.new_csv() if data is None else data
        key = key or uuid.uuid4()
        operation, _ = reserve_import_upload(actor or self.admin, key, upload_request_digest(data, muted), data, muted)
        return operation.batch

    def validate(self, batch):
        validate_user_import.run(str(batch.pk), batch.version)
        batch.refresh_from_db()
        return batch


@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[], LANGUAGE_CODE='en')
class ImportPreviewAPITests(PreviewData, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.admin, backend=BACKEND)
        self.dispatch = patch('judge.tasks.user_import.validate_user_import.apply_async')
        self.published = self.dispatch.start()
        self.addCleanup(self.dispatch.stop)
        execution = patch('judge.tasks.user_import.execute_user_import.apply_async')
        self.execution_published = execution.start()
        self.addCleanup(execution.stop)
        response = self.client.get(reverse('user_import'))
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value

    def upload(self, data=None, key=None, muted='true', **headers):
        return self.client.post(reverse('user_import_post_file'), {
            'csv_file': SimpleUploadedFile('accounts.csv', data if data is not None else self.new_csv(), 'text/csv'),
            'muted': muted,
        }, HTTP_IDEMPOTENCY_KEY=str(key or uuid.uuid4()), HTTP_X_CSRFTOKEN=self.csrf, **headers)

    def submit(self, batch, data=None, key=None, **headers):
        return self.client.post(reverse('user_import_submit'), json.dumps(data or {
            'batch_id': str(batch.pk), 'version': batch.version, 'confirmed': True,
        }), content_type='application/json', HTTP_IDEMPOTENCY_KEY=str(key or batch.idempotency_key),
            HTTP_X_CSRFTOKEN=self.csrf, **headers)

    def test_upload_only_reserves_encrypted_metadata_and_publishes_ids(self):
        before = self.account_state()
        response = self.upload()
        self.assertEqual(response.status_code, 202)
        payload = response.json()['batch']
        self.assertEqual(payload['status'], 'VALIDATING')
        batch = UserImportBatch.objects.get(pk=payload['id'])
        row = batch.rows.get()
        self.assertTrue(row.password_ciphertext)
        self.assertEqual(row.action, 'UNKNOWN')
        self.assertEqual(self.account_state(), before)
        self.published.assert_called_once()
        call = self.published.call_args
        self.assertEqual(call.kwargs['args'], [str(batch.pk), batch.version])
        self.assertNotIn(NEW_PASSWORD, response.content.decode())
        self.assertNotIn(row.password_ciphertext, response.content.decode())
        self.assertIn('no-store', response['Cache-Control'])

    def test_preview_does_not_run_account_or_expired_contest_maintenance(self):
        now = timezone.now()
        contest = Contest.objects.create(key='ended_import_preview', name='Ended contest',
                                         start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1))
        participation = ContestParticipation.objects.create(contest=contest, user=self.admin.profile)
        Profile.objects.filter(user=self.admin).update(current_contest=participation)
        before = self.account_state()
        self.assertEqual(self.client.get(reverse('user_import')).status_code, 200)
        response = self.upload()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.client.get(response.json()['batch']['status_url']).status_code, 200)
        self.assertEqual(self.account_state(), before)

    def test_duplicate_upload_same_key_reuses_batch_and_does_not_publish_again(self):
        key = uuid.uuid4()
        first, second = self.upload(key=key), self.upload(key=key)
        self.assertEqual((first.status_code, second.status_code), (202, 200))
        self.assertEqual(first.json()['batch']['id'], second.json()['batch']['id'])
        self.assertTrue(second.json()['replayed'])
        self.assertEqual(UserImportBatch.objects.count(), 1)
        self.published.assert_called_once()

    def test_same_key_changed_file_or_mute_conflicts_even_if_changed_csv_is_invalid(self):
        key = uuid.uuid4()
        self.upload(key=key)
        for variant in ({'data': self.new_csv('different')}, {'muted': 'false'}, {'data': b'bad csv'}):
            with self.subTest(variant=list(variant)):
                response = self.upload(key=key, **variant)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['error']['code'], 'idempotency_conflict')
        self.assertEqual(UserImportBatch.objects.count(), 1)

    def test_active_batch_rejects_a_different_upload_key(self):
        self.assertEqual(self.upload().status_code, 202)
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(UserImportBatch.objects.count(), 1)

    def test_same_csv_new_key_after_preview_gets_a_new_preview_without_account_writes(self):
        before = self.account_state()
        first = self.upload().json()['batch']
        self.validate(UserImportBatch.objects.get(pk=first['id']))
        second = self.upload()
        self.assertEqual(second.status_code, 202)
        self.assertNotEqual(first['id'], second.json()['batch']['id'])
        self.validate(UserImportBatch.objects.get(pk=second.json()['batch']['id']))
        self.assertEqual(self.account_state(), before)

    def test_refresh_and_lost_response_recover_by_batch_or_owner_operation_key(self):
        key = uuid.uuid4()
        first = self.upload(key=key).json()['batch']
        for query in ('?batch=' + first['id'], '?operation_key=' + str(key)):
            response = self.client.get(reverse('user_import') + query, HTTP_ACCEPT='application/json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['batch']['id'], first['id'])
        self.assertEqual(UserImportBatch.objects.count(), 1)
        missing = '?operation_key=' + str(uuid.uuid4())
        self.assertEqual(self.client.get(reverse('user_import') + missing).status_code, 200)
        self.assertEqual(self.client.get(reverse('user_import') + missing,
                                         HTTP_ACCEPT='application/json').status_code, 404)

    def test_preview_diff_password_policy_mute_and_no_secrets_in_response_or_html(self):
        Profile.objects.filter(user=self.existing).update(rotate_password_on_login=True, mute=False)
        response = self.upload(csv_bytes([
            ['existing', '', 'Changed', '', '', 'org_b', 'false'],
            ['new_preview', NEW_PASSWORD, '<script>window.csvXss=1</script>', '', '', 'org_a', 'true'],
        ]))
        batch = self.validate(UserImportBatch.objects.get(pk=response.json()['batch']['id']))
        self.assertEqual(batch.status, 'PREVIEW_READY')
        status = self.client.get(reverse('user_import_status', args=[batch.pk]))
        data = status.json()['batch']
        existing = data['rows'][0]['preview']
        self.assertEqual(existing['changes'], {'first_name': {'before': 'Old name', 'after': 'Changed'}})
        self.assertEqual(existing['mute'], {'before': False, 'after': True})
        self.assertEqual(existing['password_rotation'], {'before': True, 'after': False})
        self.assertEqual(existing['warnings'], ['password_not_restored'])
        self.assertEqual(existing['organizations_to_add'], ['org_b'])
        html = self.client.get(reverse('user_import') + '?batch=' + str(batch.pk)).content.decode()
        self.assertNotIn('<script>window.csvXss=1</script>', html)
        for value in (NEW_PASSWORD, batch.rows.last().prepared_password_ciphertext, 'credential_fingerprint'):
            self.assertNotIn(value, status.content.decode())
            self.assertNotIn(value, html)
        self.assertFalse(batch.rows.exclude(password_ciphertext='').exists())
        self.assertTrue(data['execution_available'])

    def test_sample_has_exactly_the_seven_headers_and_no_account_row(self):
        response = self.client.get(reverse('user_import_sample'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, csv_bytes())
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertIn('no-store', response['Cache-Control'])

    def test_real_csrf_rejects_missing_or_wrong_token_without_creating_a_batch(self):
        for token in (None, 'x' * 64):
            headers = {'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())}
            if token:
                headers['HTTP_X_CSRFTOKEN'] = token
            response = self.client.post(reverse('user_import_post_file'), {
                'csv_file': SimpleUploadedFile('accounts.csv', self.new_csv()),
            }, **headers)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()['error']['code'], 'csrf_failed')
            self.assertNotIn(NEW_PASSWORD, response.content.decode())
        self.assertEqual(UserImportBatch.objects.count(), 0)

    def test_methods_have_json_405_and_allow(self):
        for route, method in (('user_import_post_file', 'POST'), ('user_import_submit', 'POST')):
            response = self.client.get(reverse(route))
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response['Allow'], method)
        response = self.client.post(reverse('user_import_sample'), HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response['Allow'], 'GET')

    def test_missing_login_api_401_html_redirect_and_bearer_403(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get(reverse('user_import')).status_code, 302)
        for route in ('user_import_sample', 'user_import_post_file', 'user_import_submit'):
            response = client.get(reverse(route))
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()['error']['code'], 'authentication_required')
        for client in (client, self.client):
            response = client.get(reverse('user_import_sample'), HTTP_AUTHORIZATION='Bearer ' + 'x' * 48)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()['error']['code'], 'session_required')

    def test_non_superuser_and_revoked_privileges_403(self):
        self.client.force_login(self.existing, backend=BACKEND)
        self.assertEqual(self.client.get(reverse('user_import_sample')).status_code, 403)
        self.client.force_login(self.admin, backend=BACKEND)
        User.objects.filter(pk=self.admin.pk).update(is_superuser=False)
        self.assertEqual(self.client.get(reverse('user_import_sample')).status_code, 403)

    def test_pending_2fa_impersonation_and_forced_password_change_are_json_403(self):
        variants = ({PENDING_LOGIN_KEY: {'pending': True}}, {'_impersonate': self.existing.pk},
                    {'password_pwned': True})
        for values in variants:
            self.client.force_login(self.admin, backend=BACKEND)
            session = self.client.session
            session.update(values)
            session.save()
            response = self.client.get(reverse('user_import_sample'))
            self.assertEqual(response.status_code, 403)
            self.assertIn('error', response.json())
            self.client.logout()
        self.client.force_login(self.admin, backend=BACKEND)
        Profile.objects.filter(user=self.admin).update(is_totp_enabled=True)
        self.assertEqual(self.client.get(reverse('user_import_sample')).status_code, 403)

    def test_stale_session_epoch_is_json_401(self):
        Profile.objects.filter(user=self.admin).update(account_session_epoch=1)
        response = self.client.get(reverse('user_import_sample'))
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(auth.SESSION_KEY, self.client.session)

    def test_actor_b_cannot_read_submit_or_recover_actor_a_batch(self):
        batch = self.reserve()
        other = self.user('other_admin', is_staff=True, is_superuser=True)
        self.client.force_login(other, backend=BACKEND)
        for path in (reverse('user_import_status', args=[batch.pk]),
                     reverse('user_import') + '?batch=' + str(batch.pk),
                     reverse('user_import') + '?operation_key=' + str(batch.idempotency_key)):
            response = self.client.get(path, HTTP_ACCEPT='application/json')
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('new_preview', response.content.decode())
        self.assertEqual(self.submit(batch).status_code, 404)

    @override_settings(DMOJ_USER_IMPORT_ENABLED=False)
    def test_feature_disabled_404_and_tab_hidden(self):
        for route in ('user_import', 'user_import_sample'):
            self.assertEqual(self.client.get(reverse(route)).status_code, 404)
        response = self.client.get(reverse('user_list'))
        self.assertNotContains(response, reverse('user_import'))

    def test_nav_tab_follows_organizations_and_only_superusers_see_it(self):
        for route in ('user_list', 'contributors_list', 'organization_list'):
            response = self.client.get(reverse(route))
            self.assertContains(response, 'fa-upload')
            html = response.content.decode()
            self.assertLess(html.index('href="' + reverse('organization_list') + '"'),
                            html.index('href="' + reverse('user_import') + '"'))
        self.client.force_login(self.existing, backend=BACKEND)
        self.assertNotContains(self.client.get(reverse('user_list')), reverse('user_import'))

    def test_invalid_keys_csv_and_mute_422_without_persisting_plaintext(self):
        response = self.upload(key='invalid-key')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.upload(muted='TRUE').status_code, 422)
        response = self.upload(b'username,password_rotation\ninvalid username,TRUE\nother,yes\n')
        self.assertEqual(response.status_code, 422)
        self.assertGreaterEqual(len(response.json()['issues']), 3)
        self.assertEqual(UserImportBatch.objects.count(), 0)
        self.assertEqual(UserImportOperation.objects.count(), 0)

    @override_settings(FILE_UPLOAD_MAX_MEMORY_SIZE=1)
    def test_upload_never_falls_back_to_disk_even_above_django_memory_threshold(self):
        with patch('django.core.files.uploadhandler.TemporaryUploadedFile',
                   side_effect=AssertionError('CSV must never enter a disk upload handler')):
            self.assertEqual(self.upload().status_code, 202)

    @override_settings(DMOJ_USER_IMPORT_MAX_BYTES=128)
    def test_oversize_file_413(self):
        response = self.upload(b'x' * 129)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(UserImportBatch.objects.count(), 0)

    def test_unexpected_upload_fields_or_multiple_files_422(self):
        for fields in ({'csv_file': [SimpleUploadedFile('a.csv', self.new_csv()),
                                     SimpleUploadedFile('b.csv', self.new_csv())]},
                       {'other': SimpleUploadedFile('a.csv', self.new_csv())},
                       {'csv_file': SimpleUploadedFile('a.csv', self.new_csv()), 'rows': 'client snapshot'}):
            response = self.client.post(reverse('user_import_post_file'), fields,
                                        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()), HTTP_X_CSRFTOKEN=self.csrf)
            self.assertEqual(response.status_code, 422)

    def test_broker_failure_503_preserves_same_batch_for_retry(self):
        self.published.side_effect = RuntimeError('Broker unavailable')
        key = uuid.uuid4()
        with self.assertLogs('judge.tasks.user_import', level='WARNING'):
            response = self.upload(key=key)
        self.assertEqual(response.status_code, 503)
        batch_id = response.json()['batch']['id']
        self.published.side_effect = None
        retried = self.upload(key=key)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()['batch']['id'], batch_id)
        self.assertEqual(UserImportBatch.objects.count(), 1)

    def test_submit_queues_snapshot_only_and_rejects_client_snapshot_or_wrong_key(self):
        batch = self.validate(self.reserve())
        before = self.account_state()
        response = self.submit(batch)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['batch']['status'], 'QUEUED')
        self.assertEqual(self.submit(batch, key=uuid.uuid4()).status_code, 409)
        for field in ('rows', 'password', 'muted'):
            data = {'batch_id': str(batch.pk), 'version': batch.version, 'confirmed': True, field: 'injected'}
            self.assertEqual(self.submit(batch, data=data).status_code, 422)
        self.assertEqual(self.account_state(), before)
        self.assertEqual(UserImportOperation.objects.filter(action='submit').count(), 1)
        self.execution_published.assert_called_once()

    def test_completed_submit_replay_does_not_reissue_password_even_with_new_key(self):
        batch = self.validate(self.reserve())
        self.apply(batch)
        batch.refresh_from_db()
        before = self.account_state()
        response = self.submit(batch)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['replayed'])
        self.assertEqual(response.json()['batch']['applied_rows'], 1)
        self.assertEqual(self.submit(batch, key=uuid.uuid4()).status_code, 409)
        self.assertEqual(self.account_state(), before)

    def test_unexpected_upload_failure_is_safe_and_reservation_rolls_back(self):
        before = self.account_state()
        with patch('judge.utils.user_import_validation.seal_import_secret', side_effect=RuntimeError(NEW_PASSWORD)):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(NEW_PASSWORD, response.content.decode())
        self.assertEqual(UserImportBatch.objects.count(), 0)
        self.assertEqual(UserImportOperation.objects.count(), 0)
        self.assertEqual(self.account_state(), before)

    def test_invalid_batch_cannot_be_confirmed_and_session_db_failure_is_safe_503(self):
        batch = self.validate(self.reserve(csv_bytes([['missing_password', '', '', '', '', '', 'false']])))
        self.assertEqual(batch.status, 'INVALID')
        self.assertEqual(self.submit(batch).status_code, 422)
        with patch.object(User, 'profile', new_callable=PropertyMock,
                          side_effect=OperationalError('DO-NOT-EXPOSE-DB-DETAILS')):
            response = self.client.get(reverse('user_import_sample'))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('DO-NOT-EXPOSE-DB-DETAILS', response.content.decode())

    def test_vietnamese_preview_messages_and_csv_rules_are_translatable(self):
        response = self.client.get(reverse('user_import'), HTTP_ACCEPT_LANGUAGE='vi')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Tải lên và xem trước')
        self.assertContains(response, 'Xem trước lượt nhập')

    def test_expired_preview_clears_hash_and_stale_version_conflicts(self):
        batch = self.validate(self.reserve())
        response = self.submit(batch, data={'batch_id': str(batch.pk), 'version': 2, 'confirmed': True})
        self.assertEqual(response.status_code, 409)
        UserImportBatch.objects.filter(pk=batch.pk).update(preview_expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.get(reverse('user_import_status', args=[batch.pk]))
        self.assertEqual(response.json()['batch']['status'], 'EXPIRED')
        self.assertFalse(batch.rows.exclude(prepared_password_ciphertext='').exists())

    def test_owner_status_paginates_and_old_user_routes_still_resolve(self):
        batch = self.reserve(csv_bytes([[f'preview_{n}', NEW_PASSWORD, '', '', '', '', 'false'] for n in range(55)]))
        response = self.client.get(reverse('user_import_status', args=[batch.pk]) + '?page=2')
        data = response.json()['batch']
        self.assertEqual((data['page'], data['pages'], len(data['rows'])), (2, 2, 5))
        self.assertEqual(self.client.get(reverse('user_import_status', args=[batch.pk]) + '?page=0').status_code, 422)
        for path, name in (('/users/', 'user_list'), ('/users/find', 'user_ranking_redirect'),
                           ('/user/existing', 'user_page')):
            self.assertEqual(resolve(path).url_name, name)
        self.assertEqual(resolve('/users/2').kwargs['page'], 2)


@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[])
class ImportValidationTaskTests(PreviewData, TestCase):
    def test_validation_prepares_hashes_and_snapshot_without_changing_accounts(self):
        batch = self.reserve()
        before = self.account_state()
        self.validate(batch)
        self.assertEqual(batch.status, 'PREVIEW_READY')
        self.assertEqual(batch.validation_attempts, 1)
        self.assertTrue(batch.snapshot_digest)
        row = batch.rows.get()
        self.assertEqual((row.status, row.action), ('VALID', 'CREATE'))
        self.assertEqual(row.password_ciphertext, '')
        self.assertTrue(row.prepared_password_ciphertext)
        self.assertEqual(self.account_state(), before)
        self.validate(batch)
        self.assertEqual(batch.validation_attempts, 1)

    @override_settings(AUTH_PASSWORD_VALIDATORS=[{'NAME': __name__ + '.RejectPasswordValidator'}])
    def test_collects_account_and_password_errors_across_entire_file_and_clears_all_secrets(self):
        batch = self.reserve(csv_bytes([
            ['good', NEW_PASSWORD, '', '', '', '', 'true'],
            ['no_password', '', '', '', '', '', 'false'],
            ['bad_org', 'Reject-secret-one', '', '', '', 'nonexistent', 'true'],
            ['bad_password', 'Reject-secret-two', '', '', '', '', 'true'],
            ['import_admin', '', '', '', '', '', 'false'],
        ]))
        before = self.account_state()
        self.validate(batch)
        self.assertEqual(batch.status, 'INVALID')
        errors = {(error['row'], error['code']) for error in batch.errors}
        self.assertTrue({(2, 'password_required'), (3, 'unknown_organization'), (3, 'invalid_password'),
                         (4, 'invalid_password'), (5, 'ineligible_account')} <= errors)
        self.assertEqual(self.account_state(), before)
        self.assertFalse(batch.rows.exclude(password_ciphertext='').exists())
        self.assertFalse(batch.rows.exclude(prepared_password_ciphertext='').exists())
        self.assertNotIn('Reject-secret', json.dumps(batch.errors))
        self.assertEqual(batch.rows.get(row_number=1).status, 'NOT_APPLIED')

    def test_expired_raw_password_fails_even_without_cleanup(self):
        batch = self.reserve()
        UserImportBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(hours=1))
        self.validate(batch)
        self.assertEqual(batch.status, 'EXPIRED')
        self.assertEqual(batch.validation_attempts, 0)
        self.assertFalse(batch.rows.exclude(password_ciphertext='').exists())

    def test_password_validation_uses_the_post_import_candidate(self):
        batch = self.reserve(csv_bytes([
            ['existing', NEW_PASSWORD, 'Candidate', '', 'candidate@example.org', '', 'true'],
        ]))
        with patch('judge.utils.user_import_storage.validate_password') as validator:
            self.validate(batch)
        candidate = validator.call_args.kwargs['user']
        self.assertEqual((candidate.pk, candidate.first_name, candidate.last_name, candidate.email),
                         (self.existing.pk, 'Candidate', 'Old school', 'candidate@example.org'))

    def test_change_during_password_validation_conflicts_and_discards_prepared_hashes(self):
        batch = self.reserve(csv_bytes([['existing', NEW_PASSWORD, 'Changed', '', '', '', 'true']]))

        def change(password, user):
            User.objects.filter(pk=self.existing.pk).update(first_name='Concurrent admin change')

        with patch('judge.utils.user_import_storage.validate_password', side_effect=change):
            self.validate(batch)
        self.assertEqual(batch.status, 'CONFLICT')
        self.assertEqual(User.objects.get(pk=self.existing.pk).first_name, 'Concurrent admin change')
        self.assertFalse(batch.rows.exclude(prepared_password_ciphertext='').exists())

    def test_actor_permission_revoked_before_worker_prevents_preview(self):
        batch = self.reserve()
        User.objects.filter(pk=self.admin.pk).update(is_superuser=False)
        self.validate(batch)
        self.assertEqual(batch.status, 'FAILED')
        self.assertEqual(batch.errors[0]['code'], 'permission_denied')
        self.assertFalse(batch.rows.exclude(password_ciphertext='').exists())

    def test_duplicate_claim_does_not_spend_attempts_and_old_attempt_cannot_write(self):
        batch = self.reserve()
        claimed = claim_import_validation(batch.pk, batch.version)
        self.assertIsNone(claim_import_validation(batch.pk, batch.version))
        batch.refresh_from_db()
        self.assertEqual(batch.validation_attempts, 1)
        _, _, rows, _ = resolve_validation_rows(batch.pk, batch.version, claimed.attempt_token)
        row = rows[0]
        candidate = User(username=row.username)
        UserImportBatch.objects.filter(pk=batch.pk).update(attempt_token=uuid.uuid4())
        with self.assertRaises(UserImportError) as error:
            prepare_import_password(row, candidate, validation_token=claimed.attempt_token)
        self.assertEqual(error.exception.code, 'validation_superseded')
        finish_import_validation(batch.pk, batch.version, claimed.attempt_token, 'FAILED',
                                 [ImportIssue('stale', 'Old attempt must not finish this batch.')])
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'VALIDATING')
        with transaction.atomic(), self.assertRaises(UserImportError):
            locked_validation(batch.pk, batch.version, claimed.attempt_token)

    def test_validation_retry_uses_db_budget_and_prepared_hash_without_rehashing(self):
        batch = self.reserve()
        with patch('judge.tasks.user_import.finalize_import_preview', side_effect=OperationalError(1213, 'Deadlock')):
            with patch.object(validate_user_import, 'retry', side_effect=Retry()) as retry:
                with self.assertRaises(Retry):
                    self.validate(batch)
                retry.assert_called_once()
        row = batch.rows.get()
        prepared = row.prepared_password_ciphertext
        self.assertTrue(prepared)
        with patch('judge.utils.user_import_storage.make_password', side_effect=AssertionError('Must reuse hash')):
            self.validate(batch)
        self.assertEqual(batch.status, 'PREVIEW_READY')
        self.assertEqual(batch.validation_attempts, 2)
        self.assertEqual(batch.rows.get().prepared_password_ciphertext, prepared)

    def test_budget_exhaustion_is_terminal_and_removes_secrets(self):
        batch = self.reserve()
        UserImportBatch.objects.filter(pk=batch.pk).update(validation_attempts=3)
        self.validate(batch)
        self.assertEqual(batch.status, 'FAILED')
        self.assertEqual(batch.validation_attempts, 3)
        self.assertFalse(batch.rows.exclude(password_ciphertext='').exists())

    @override_settings(AUTH_PASSWORD_VALIDATORS=[{'NAME': 'judge.utils.pwned.PwnedPasswordsValidator'}])
    def test_hibp_timeout_uses_existing_fallback_and_reports_generic_password_error(self):
        batch = self.reserve(self.new_csv(password='password123'))
        with patch('judge.utils.pwned.requests.get', side_effect=requests.Timeout):
            self.validate(batch)
        self.assertEqual(batch.status, 'INVALID')
        self.assertEqual(batch.errors[0]['code'], 'invalid_password')
        self.assertNotIn('password123', json.dumps(batch.errors))

    def test_unexpected_validator_error_is_sanitized_in_logs_and_batch(self):
        batch = self.reserve()
        with patch('judge.utils.user_import_storage.validate_password', side_effect=RuntimeError(NEW_PASSWORD)):
            with self.assertLogs('judge.tasks.user_import', level='WARNING') as logs:
                self.validate(batch)
        self.assertEqual(batch.status, 'FAILED')
        self.assertNotIn(NEW_PASSWORD, json.dumps(batch.errors) + str(logs.output))
        self.assertFalse(batch.rows.exclude(password_ciphertext='').exists())


@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[])
class ImportUploadConcurrencyTests(PreviewData, TransactionTestCase):
    def test_hash_validation_releases_account_locks_and_duplicate_task_does_not_claim_again(self):
        batch = self.reserve(csv_bytes([['existing', NEW_PASSWORD, 'Imported name', '', '', '', 'true']]))
        entered, release = Event(), Event()

        def pause(password, user):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError('Validation test release timed out')

        def work():
            close_old_connections()
            try:
                validate_user_import.run(str(batch.pk), batch.version)
            finally:
                close_old_connections()

        with patch('judge.utils.user_import_storage.validate_password', side_effect=pause):
            worker = Thread(target=work)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                validate_user_import.run(str(batch.pk), batch.version)
                batch.refresh_from_db()
                self.assertEqual(batch.validation_attempts, 1)
                User.objects.filter(pk=self.existing.pk).update(first_name='Concurrent edit')
            finally:
                release.set()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'CONFLICT')
        self.assertFalse(batch.rows.exclude(prepared_password_ciphertext='').exists())

    def race(self, keys):
        barrier = Barrier(2)
        data = self.new_csv()
        results = []

        def upload(key):
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.admin.pk)
                barrier.wait(timeout=5)
                operation, created = reserve_import_upload(actor, key, upload_request_digest(data, True), data, True)
                results.append((str(operation.batch_id), created))
            except UserImportError as error:
                results.append(error.code)
            except Exception as error:
                results.append(type(error).__name__)
            finally:
                close_old_connections()

        workers = [Thread(target=upload, args=(key,)) for key in keys]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        return results

    def test_concurrent_same_key_creates_one_batch(self):
        key = uuid.uuid4()
        results = self.race([key, key])
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(result[1] for result in results), [False, True])
        self.assertEqual(len({result[0] for result in results}), 1)
        self.assertEqual(UserImportBatch.objects.count(), 1)
        self.assertEqual(UserImportOperation.objects.count(), 1)

    def test_concurrent_different_keys_admit_one_active_batch(self):
        results = self.race([uuid.uuid4(), uuid.uuid4()])
        self.assertIn('active_batch', results)
        self.assertEqual(UserImportBatch.objects.count(), 1)
