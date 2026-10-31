"""M5 gates using committed fixtures and independent MySQL/MariaDB connections."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, connection, connections
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.state import ProjectState
from django.test import RequestFactory, TransactionTestCase, override_settings

from judge.models import Profile
from judge.tasks.user_import import validate_user_import
from judge.tests.test_user_import_csv import csv_bytes
from judge.tests.test_user_import_models import IMPORT_SETTINGS
from judge.tests.test_user_import_preview import PreviewData
from judge.tests.test_user_import_service import NEW_PASSWORD
from judge.utils.managed_login import begin_password_login, locked_managed_account, on_admin_password_changed
from judge.utils.user_import_config import get_user_import_config
from judge.utils.user_import_service import execute_import_batch
from judge.utils.user_import_storage import prepare_import_password


class ImportMigrationAcceptanceTests(TransactionTestCase):
    def test_upgrade_from_managed_login_baseline_and_known_model_drift(self):
        executor = MigrationExecutor(connection)
        executor.migrate([('judge', '0239_managed_login_policy')])
        try:
            self.assertNotIn('judge_userimportbatch', connection.introspection.table_names())
            executor = MigrationExecutor(connection)
            self.assertEqual([(migration.app_label, migration.name, backwards)
                              for migration, backwards in executor.migration_plan(executor.loader.graph.leaf_nodes())],
                             [('judge', '0240_user_import', False)])
            executor.migrate(executor.loader.graph.leaf_nodes())
            self.assertTrue({'judge_userimportbatch', 'judge_userimportrow', 'judge_userimportoperation'} <=
                            set(connection.introspection.table_names()))
            executor = MigrationExecutor(connection)
            changes = MigrationAutodetector(executor.loader.project_state(), ProjectState.from_apps(apps)).changes(
                graph=executor.loader.graph)
            operations = [(app, type(op).__name__, op.model_name, op.name)
                          for app, migrations in changes.items() for migration in migrations
                          for op in migration.operations]
            # Pre-existing drift is recorded explicitly; any new operation fails
            # this gate instead of silently ignoring makemigrations' exit code.
            self.assertEqual(set(operations), {
                ('judge', 'AlterField', 'contest', name)
                for name in ('authors', 'curators', 'private_contestants', 'rate_exclude', 'testers')
            } | {('judge', 'AlterField', 'profile', 'timezone')})
            self.assertEqual(len(operations), 6)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())


@override_settings(**{**IMPORT_SETTINGS, 'DMOJ_USER_IMPORT_VALIDATION_WORKERS': 4}, AUTH_PASSWORD_VALIDATORS=[])
class ImportParallelValidationTests(PreviewData, TransactionTestCase):
    def rows(self, count):
        return [[f'parallel_{i}', NEW_PASSWORD, '', '', '', '', 'false'] for i in range(count)]

    def test_bounded_connections_collect_all_password_errors_before_any_account_write(self):
        rows = self.rows(12)
        for i in (1, 5, 9):
            rows[i][1] = 'Reject-private-value'
        batch = self.reserve(csv_bytes(rows))
        before = self.account_state()
        barrier = threading.Barrier(4, timeout=10)
        lock = threading.Lock()
        active = peak = started = 0
        connection_ids = set()

        def prepare(row, candidate, **kwargs):
            nonlocal active, peak, started
            with lock:
                active += 1
                started += 1
                first_wave = started <= 4
                peak = max(peak, active)
            try:
                connections['default'].ensure_connection()
                with lock:
                    connection_ids.add(connections['default'].connection.thread_id())
                if first_wave:
                    barrier.wait()
                return prepare_import_password(row, candidate, **kwargs)
            finally:
                with lock:
                    active -= 1

        with override_settings(AUTH_PASSWORD_VALIDATORS=[{
            'NAME': 'judge.tests.test_user_import_preview.RejectPasswordValidator',
        }]), patch('judge.tasks.user_import.prepare_import_password', side_effect=prepare):
            self.validate(batch)
        self.assertEqual(batch.status, 'INVALID')
        self.assertEqual(peak, 4)
        self.assertEqual(len(connection_ids), 4)
        self.assertEqual(started, 12)
        self.assertEqual([issue['row'] for issue in batch.errors], [2, 6, 10])
        self.assertEqual(self.account_state(), before)
        self.assertFalse(batch.rows.exclude(password_ciphertext='', prepared_password_ciphertext='').exists())

    @override_settings(DMOJ_USER_IMPORT_VALIDATION_WORKERS=2)
    def test_fatal_error_joins_inflight_writer_before_terminal_cleanup(self):
        batch = self.reserve(csv_bytes(self.rows(20)))
        before = self.account_state()
        entered = threading.Event()
        failed = threading.Event()
        release = threading.Event()
        started = []

        def prepare(row, candidate, **kwargs):
            started.append(row.row_number)
            if row.row_number == 1:
                if not entered.wait(10):
                    raise RuntimeError('Second validator did not start')
                failed.set()
                raise RuntimeError('Private callback error must not be logged')
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Writer gate timed out')
            return prepare_import_password(row, candidate, **kwargs)

        def validate():
            close_old_connections()
            try:
                validate_user_import.run(str(batch.pk), batch.version)
            finally:
                connections.close_all()

        with patch('judge.tasks.user_import.prepare_import_password', side_effect=prepare):
            worker = threading.Thread(target=validate)
            worker.start()
            try:
                self.assertTrue(failed.wait(10))
                batch.refresh_from_db()
                self.assertEqual(batch.status, 'VALIDATING')
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                worker.join(15)
        self.assertFalse(worker.is_alive())
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'FAILED')
        self.assertEqual(sorted(started), [1, 2])
        self.assertEqual(self.account_state(), before)
        self.assertFalse(batch.rows.exclude(password_ciphertext='', prepared_password_ciphertext='').exists())

    def test_validation_worker_limit_is_enforced_without_exposing_keys(self):
        for value in (0, 9, True, '8'):
            with self.subTest(value=value), override_settings(DMOJ_USER_IMPORT_VALIDATION_WORKERS=value):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    get_user_import_config()
                self.assertIn('DMOJ_USER_IMPORT_VALIDATION_WORKERS', str(caught.exception))

    def test_account_transaction_blocks_competing_login_admin_and_membership_writes_until_commit(self):
        from judge.utils import user_import_service as service
        login_user = self.user('lock_login')
        reset_user = self.user('lock_reset')
        member_user = self.user('lock_member')
        other_admin = self.user('lock_operator', is_staff=True, is_superuser=True)
        batch = self.stage([[user.username, '', 'Imported name', '', '', '', 'false']
                            for user in (login_user, reset_user, member_user)])
        token = self.claim(batch)
        paused = threading.Event()
        release = threading.Event()
        started = [threading.Event() for unused in range(3)]
        finished = [threading.Event() for unused in range(3)]
        real_row = service._apply_row

        def apply_row(actor, current, row, *args):
            result = real_row(actor, current, row, *args)
            if row.row_number == 1:
                paused.set()
                if not release.wait(10):
                    raise RuntimeError('Account transaction gate timed out')
            return result

        def independent(action, index=None):
            close_old_connections()
            try:
                if index is not None:
                    started[index].set()
                return action()
            finally:
                if index is not None:
                    finished[index].set()
                connections.close_all()

        def login():
            user = User.objects.get(pk=login_user.pk)
            user.backend = 'django.contrib.auth.backends.ModelBackend'
            request = RequestFactory().post('/accounts/login/')
            request.user = AnonymousUser()
            request.session = SessionStore()
            from judge.tests.test_user_import_service import PASSWORD
            begin_password_login(request, user, PASSWORD)

        def reset():
            with locked_managed_account(other_admin, reset_user.pk, ('auth.change_user',)) as (actor, user, profile):
                user.set_password(NEW_PASSWORD)
                user.save(update_fields=['password'])
                on_admin_password_changed(actor, user)

        def membership():
            Profile.objects.get(user=member_user).organizations.add(self.org_b)

        with patch.object(service, '_apply_row', side_effect=apply_row), ThreadPoolExecutor(max_workers=4) as pool:
            importing = pool.submit(independent, lambda: execute_import_batch(self.admin, batch.pk, 1, token))
            try:
                self.assertTrue(paused.wait(10))
                competitors = [pool.submit(independent, action, index)
                               for index, action in enumerate((login, reset, membership))]
                self.assertTrue(all(event.wait(5) for event in started))
                time.sleep(0.15)
                self.assertTrue(all(not event.is_set() for event in finished))
            finally:
                release.set()
            importing.result(timeout=15)
            for future in competitors:
                future.result(timeout=15)
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.applied_rows), ('COMPLETED', 3))
        reset_user.refresh_from_db()
        self.assertTrue(reset_user.check_password(NEW_PASSWORD))
        self.assertTrue(member_user.profile.organizations.filter(pk=self.org_b.pk).exists())
