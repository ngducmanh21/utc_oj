import base64
import json
import uuid
from datetime import timedelta, timezone as datetime_timezone
from threading import Barrier, Thread
from unittest.mock import Mock, patch

from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from judge.checks import check_user_import_config
from judge.models import Language, Profile, UserImportBatch, UserImportOperation, UserImportRow
from judge.tests.test_user_import_csv import csv_bytes
from judge.utils.user_import import UserImportError, get_or_create_import_operation, import_request_digest, \
    parse_idempotency_key, parse_user_csv, upload_request_digest
from judge.utils.user_import_config import get_user_import_config
from judge.utils.user_import_crypto import open_import_secret, seal_import_secret
from judge.utils.user_import_storage import get_import_password, get_prepared_import_password, \
    prepare_import_password, store_import_rows

KEY = base64.urlsafe_b64encode(b'a' * 32).decode()
OTHER_KEY = base64.urlsafe_b64encode(b'b' * 32).decode()
DIGEST_KEY = base64.urlsafe_b64encode(b'c' * 32).decode()
IMPORT_SETTINGS = {
    'DMOJ_USER_IMPORT_ENABLED': True,
    'DMOJ_USER_IMPORT_KEYS': {'one': KEY},
    'DMOJ_USER_IMPORT_ACTIVE_KEY_ID': 'one',
    'DMOJ_USER_IMPORT_DIGEST_KEY': DIGEST_KEY,
    'DEFAULT_USER_LANGUAGE': 'CPP17',
}


class UserImportConfigTests(SimpleTestCase):
    @override_settings(DMOJ_USER_IMPORT_ENABLED=False, DMOJ_USER_IMPORT_KEYS={},
                       DMOJ_USER_IMPORT_ACTIVE_KEY_ID='', DMOJ_USER_IMPORT_DIGEST_KEY='')
    def test_disabled_default_does_not_require_keys(self):
        self.assertFalse(get_user_import_config().enabled)
        self.assertEqual(check_user_import_config(None), [])
        with self.assertRaises(UserImportError) as error:
            upload_request_digest(b'csv', True)
        self.assertEqual(error.exception.code, 'disabled')

    @override_settings(DMOJ_USER_IMPORT_ENABLED=True, DMOJ_USER_IMPORT_KEYS={},
                       DMOJ_USER_IMPORT_ACTIVE_KEY_ID='', DMOJ_USER_IMPORT_DIGEST_KEY='')
    def test_enabling_without_keys_fails_closed(self):
        errors = check_user_import_config(None)
        self.assertEqual([error.id for error in errors], ['judge.E002'])
        with self.assertRaises(ImproperlyConfigured):
            get_user_import_config()

    @override_settings(**IMPORT_SETTINGS)
    def test_invalid_key_and_limits_never_print_secret_values(self):
        variants = [
            {'DMOJ_USER_IMPORT_KEYS': {'one': 'DO-NOT-LOG-THIS-SECRET'}},
            {'DMOJ_USER_IMPORT_DIGEST_KEY': 'DO-NOT-LOG-THIS-SECRET'},
            {'DMOJ_USER_IMPORT_MAX_ROWS': True},
            {'DMOJ_USER_IMPORT_MAX_BYTES': 0},
            {'DMOJ_USER_IMPORT_MAX_ATTEMPTS': -1},
            {'DMOJ_USER_IMPORT_PASSWORD_SECONDS': 999999},
            {'DMOJ_USER_IMPORT_ACTIVE_KEY_ID': 'missing'},
            {'DMOJ_USER_IMPORT_DIGEST_KEY': KEY},
        ]
        for variant in variants:
            with self.subTest(variant=list(variant)), override_settings(**variant):
                errors = check_user_import_config(None)
                self.assertEqual(len(errors), 1)
                self.assertNotIn('DO-NOT-LOG-THIS-SECRET', str(errors))
        self.assertNotIn(KEY, repr(get_user_import_config()))
        self.assertNotIn(DIGEST_KEY, repr(get_user_import_config()))


@override_settings(**IMPORT_SETTINGS)
class UserImportSecretTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.context = {'batch_id': uuid.uuid4(), 'row_number': 1, 'purpose': 'password',
                        'expires_at': self.now + timedelta(minutes=15)}

    def test_authenticated_secret_preserves_password_and_is_not_plaintext(self):
        value = '  Unique-password,\nwith spaces  '
        token = seal_import_secret(value, **self.context, now=self.now)
        self.assertNotIn(value, token)
        self.assertEqual(open_import_secret(token, **self.context, now=self.now), value)

    def test_secret_is_bound_to_batch_row_purpose_and_expiry(self):
        token = seal_import_secret('secret', **self.context, now=self.now)
        for variant in ({'batch_id': uuid.uuid4()}, {'row_number': 2}, {'purpose': 'prepared_hash'},
                        {'expires_at': self.context['expires_at'] + timedelta(seconds=1)}):
            with self.subTest(variant=list(variant)), self.assertRaises(UserImportError) as error:
                open_import_secret(token, **{**self.context, **variant}, now=self.now)
            self.assertEqual(error.exception.code, 'invalid_secret')

    def test_expiry_is_enforced_without_cleanup_and_cannot_be_extended(self):
        token = seal_import_secret('secret', **self.context, now=self.now)
        with self.assertRaises(UserImportError) as error:
            open_import_secret(token, **self.context, now=self.context['expires_at'])
        self.assertEqual(error.exception.code, 'expired_secret')
        with self.assertRaises(UserImportError):
            seal_import_secret('secret', **{**self.context, 'expires_at': self.now + timedelta(hours=1)}, now=self.now)

    def test_tampered_or_unknown_key_tokens_are_rejected(self):
        token = seal_import_secret('secret', **self.context, now=self.now)
        for bad in ('bad', 'missing:' + token.split(':', 1)[1], token[:-6] + 'AAAAAA', None):
            with self.subTest(kind=type(bad).__name__), self.assertRaises(UserImportError):
                open_import_secret(bad, **self.context, now=self.now)

    def test_rotation_reads_old_keys_and_new_tokens_use_active_key(self):
        token = seal_import_secret('secret', **self.context, now=self.now)
        with override_settings(DMOJ_USER_IMPORT_KEYS={'one': KEY, 'two': OTHER_KEY},
                               DMOJ_USER_IMPORT_ACTIVE_KEY_ID='two'):
            self.assertEqual(open_import_secret(token, **self.context, now=self.now), 'secret')
            self.assertTrue(seal_import_secret('new', **self.context, now=self.now).startswith('two:'))

    def test_equivalent_timezone_offsets_do_not_break_secret_context(self):
        context = {**self.context, 'expires_at': self.context['expires_at'].astimezone(
            datetime_timezone(timedelta(hours=7)))}
        token = seal_import_secret('secret', **context, now=self.now)
        self.assertEqual(open_import_secret(token, **self.context, now=self.now), 'secret')

    def test_request_digests_bind_action_and_payload_without_random_hash_salts(self):
        payload = {'username': 'new', 'password': 'sensitive-value', 'password_rotation': True}
        digest = import_request_digest('upload', payload)
        self.assertEqual(digest, import_request_digest('upload', dict(reversed(list(payload.items())))))
        self.assertNotEqual(digest, import_request_digest('submit', payload))
        self.assertNotEqual(digest, import_request_digest('upload', {**payload, 'password': 'changed'}))
        data = csv_bytes([['new', 'Password-2026', '', '', '', '', 'true']])
        self.assertEqual(upload_request_digest(data, True), upload_request_digest(data, True))
        self.assertNotEqual(upload_request_digest(data, True), upload_request_digest(data, False))
        self.assertNotIn('sensitive-value', digest)
        with override_settings(DMOJ_USER_IMPORT_KEYS={'two': OTHER_KEY}, DMOJ_USER_IMPORT_ACTIVE_KEY_ID='two'):
            self.assertEqual(digest, import_request_digest('upload', payload))

    def test_key_parsing_is_strict(self):
        key = uuid.uuid4()
        self.assertEqual(parse_idempotency_key(str(key)), key)
        for bad in ('', 'not-a-uuid', None, 123, True):
            with self.subTest(kind=type(bad).__name__), self.assertRaises(UserImportError):
                parse_idempotency_key(bad)


class ImportModelData:
    fixtures = ['language_small']

    def actor(self, name='import_admin'):
        return User.objects.create_user(name, is_staff=True, is_superuser=True)

    def batch(self, actor=None, key=None, **kwargs):
        now = timezone.now()
        return UserImportBatch.objects.create(
            actor=actor or self.admin, idempotency_key=key or uuid.uuid4(),
            preview_expires_at=now + timedelta(hours=1), secret_expires_at=now + timedelta(hours=24), **kwargs,
        )


@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[])
class UserImportModelTests(ImportModelData, TestCase):
    def setUp(self):
        self.admin = self.actor()

    def test_defaults_and_unique_row_number(self):
        batch = self.batch()
        self.assertEqual(batch.status, 'VALIDATING')
        self.assertTrue(batch.muted)
        self.assertEqual(batch.execution_attempts, 0)
        UserImportRow.objects.create(batch=batch, row_number=1, line_number=2, username='new')
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserImportRow.objects.create(batch=batch, row_number=1, line_number=3, username='other')

    def test_batch_cannot_publish_partial_account_counts(self):
        for status, applied in [('RUNNING', 1), ('COMPLETED', 1), ('COMPLETED', 0)]:
            with self.subTest(status=status, applied=applied), self.assertRaises(IntegrityError), transaction.atomic():
                self.batch(total_rows=2, status=status, applied_rows=applied)
        completed = self.batch(total_rows=2, status='COMPLETED', applied_rows=2)
        self.assertEqual(completed.applied_rows, 2)

    def test_credentials_cannot_be_stored_in_metadata_json(self):
        batch = self.batch()
        for attribute in ('input_data', 'preview', 'baseline', 'errors', 'result'):
            with self.subTest(attribute=attribute), self.assertRaises(ValidationError):
                UserImportRow.objects.create(batch=batch, row_number=1, line_number=2, username='new',
                                             **{attribute: {'nested': [{'password': 'DO-NOT-STORE'}]}})
        self.assertFalse(batch.rows.exists())

    def test_database_rejects_two_active_secret_forms(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserImportRow.objects.create(batch=self.batch(), row_number=1, line_number=2, username='new',
                                         password_ciphertext='one', prepared_password_ciphertext='two')

    def test_store_rows_keeps_secrets_out_of_metadata_and_leaves_accounts_untouched(self):
        batch = self.batch()
        password = '  CSV-secret-password-2026  '
        before = list(User.objects.values())
        rows = store_import_rows(batch, parse_user_csv(csv_bytes([['new', password, 'Name', '', '', '', 'true']])))
        row = rows[0]
        self.assertEqual(row.action, 'CREATE')
        self.assertEqual(get_import_password(row), password)
        metadata = json.dumps([row.input_data, row.preview, row.baseline, row.result, row.errors])
        self.assertNotIn(password, metadata)
        self.assertNotIn(password, row.password_ciphertext)
        self.assertEqual(list(User.objects.values()), before)
        self.assertFalse(Profile.objects.filter(user__username='new').exists())

    def test_invalid_csv_is_not_partially_persisted(self):
        batch = self.batch()
        parsed = parse_user_csv(csv_bytes([['valid', 'Password-2026', '', '', '', '', 'true'],
                                           ['invalid', '', '', '', '', '', 'false']]))
        with self.assertRaises(UserImportError):
            store_import_rows(batch, parsed)
        self.assertFalse(batch.rows.exists())
        self.assertFalse(User.objects.filter(username='valid').exists())

    def test_prepare_password_atomically_removes_recoverable_plaintext(self):
        batch = self.batch()
        password = 'Strong-CSV-password-2026'
        row = store_import_rows(batch, parse_user_csv(csv_bytes([['new', password, '', '', '', '', 'true']])))[0]
        before = User.objects.count()
        encoded = prepare_import_password(row, User(username='new'))
        row.refresh_from_db()
        self.assertEqual(row.password_ciphertext, '')
        self.assertIsNone(row.password_expires_at)
        self.assertNotIn(encoded, row.prepared_password_ciphertext)
        self.assertTrue(check_password(password, get_prepared_import_password(row)))
        self.assertEqual(User.objects.count(), before)

    @override_settings(AUTH_PASSWORD_VALIDATORS=[{
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    }])
    def test_invalid_password_is_removed_without_echoing_its_value(self):
        row = store_import_rows(self.batch(), parse_user_csv(csv_bytes([['new', 'abc', '', '', '', '', 'true']])))[0]
        with self.assertRaises(UserImportError) as error:
            prepare_import_password(row, User(username='new'))
        row.refresh_from_db()
        self.assertEqual(error.exception.code, 'invalid_password')
        self.assertNotIn('abc', str(error.exception))
        self.assertEqual(row.password_ciphertext, '')
        self.assertEqual(row.prepared_password_ciphertext, '')
        self.assertEqual(row.status, 'INVALID')
        self.assertEqual(row.errors[0]['code'], 'invalid_password')
        with self.assertRaises(UserImportError):
            prepare_import_password(row, User(username='new'))

    @override_settings(AUTH_PASSWORD_VALIDATORS=[{'NAME': 'judge.utils.pwned.PwnedPasswordsValidator'}])
    def test_hibp_outage_uses_existing_common_password_fallback(self):
        parsed = parse_user_csv(csv_bytes([['new', 'password', '', '', '', '', 'true']]))
        row = store_import_rows(self.batch(), parsed)[0]
        with patch('judge.utils.pwned._get_pwned', return_value=None), self.assertRaises(UserImportError):
            prepare_import_password(row, User(username='new'))
        row.refresh_from_db()
        self.assertEqual(row.password_ciphertext, '')

    def test_expired_raw_and_prepared_passwords_are_not_read_even_before_cleanup(self):
        parsed = parse_user_csv(csv_bytes([['new', 'Password-2026', '', '', '', '', 'true']]))
        row = store_import_rows(self.batch(), parsed)[0]
        with self.assertRaises(UserImportError):
            get_import_password(row, now=row.password_expires_at)
        prepare_import_password(row, User(username='new'))
        row.refresh_from_db()
        with self.assertRaises(UserImportError):
            get_prepared_import_password(row, now=row.prepared_password_expires_at)

    def test_retry_password_preparation_reuses_existing_hash(self):
        parsed = parse_user_csv(csv_bytes([['new', 'Password-2026', '', '', '', '', 'true']]))
        row = store_import_rows(self.batch(), parsed)[0]
        first = prepare_import_password(row, User(username='new'))
        row.refresh_from_db()
        expiry = row.prepared_password_expires_at
        self.assertEqual(prepare_import_password(row, User(username='new')), first)
        row.refresh_from_db()
        self.assertEqual(row.prepared_password_expires_at, expiry)

    def test_blank_existing_password_stores_no_secret(self):
        user = User.objects.create_user('existing', password='Existing-password-2026')
        Profile.objects.create(user=user, language=Language.objects.get(key='CPP17'))
        original_hash = user.password
        row = store_import_rows(self.batch(), parse_user_csv(csv_bytes([['existing', '', '', '', '', '', 'false']])))[0]
        self.assertIsNone(prepare_import_password(row, user))
        self.assertEqual(row.password_ciphertext, '')
        user.refresh_from_db()
        self.assertEqual(user.password, original_hash)

    def test_failed_storage_rolls_back_all_import_rows(self):
        parsed = parse_user_csv(csv_bytes([['one', 'Password-2026', '', '', '', '', 'true'],
                                           ['two', 'Password-2026', '', '', '', '', 'true']]))
        batch = self.batch()
        from judge.utils import user_import_storage
        original = user_import_storage.seal_import_secret
        calls = 0

        def seal(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('test failure')
            return original(*args, **kwargs)

        with patch.object(user_import_storage, 'seal_import_secret', side_effect=seal), self.assertRaises(RuntimeError):
            store_import_rows(batch, parsed)
        self.assertFalse(batch.rows.exists())

    def test_idempotency_returns_same_batch_and_rejects_changed_payload(self):
        key = uuid.uuid4()
        create = Mock(side_effect=lambda: self.batch(key=key))
        first, created = get_or_create_import_operation(self.admin, 'upload', key, 'a' * 64, create)
        again, recreated = get_or_create_import_operation(self.admin, 'upload', str(key), 'a' * 64, create)
        self.assertTrue(created)
        self.assertFalse(recreated)
        self.assertEqual(first.batch_id, again.batch_id)
        self.assertEqual(create.call_count, 1)
        with self.assertRaises(UserImportError) as error:
            get_or_create_import_operation(self.admin, 'upload', key, 'b' * 64, create)
        self.assertEqual(error.exception.code, 'idempotency_conflict')
        self.assertEqual(UserImportBatch.objects.count(), 1)

    def test_new_key_same_digest_is_a_new_batch_and_actor_scope_is_separate(self):
        other = self.actor('other_admin')
        key = uuid.uuid4()
        one, _ = get_or_create_import_operation(self.admin, 'upload', key, 'a' * 64, lambda: self.batch(key=key))
        two_key = uuid.uuid4()
        two, _ = get_or_create_import_operation(self.admin, 'upload', two_key, 'a' * 64,
                                                lambda: self.batch(key=two_key))
        three, _ = get_or_create_import_operation(other, 'upload', key, 'a' * 64,
                                                  lambda: self.batch(actor=other, key=key))
        self.assertEqual(len({one.batch_id, two.batch_id, three.batch_id}), 3)

    def test_submit_and_upload_keys_have_separate_scopes(self):
        key = uuid.uuid4()
        upload, _ = get_or_create_import_operation(self.admin, 'upload', key, 'a' * 64, lambda: self.batch(key=key))
        submit, created = get_or_create_import_operation(self.admin, 'submit', key, 'b' * 64, lambda: upload.batch)
        self.assertTrue(created)
        self.assertEqual(submit.batch_id, upload.batch_id)

    def test_reservation_failure_does_not_leave_an_unrecoverable_key(self):
        key = uuid.uuid4()
        with self.assertRaises(RuntimeError):
            get_or_create_import_operation(self.admin, 'upload', key, 'a' * 64,
                                           Mock(side_effect=RuntimeError('test failure')))
        self.assertFalse(UserImportOperation.objects.exists())
        self.assertFalse(UserImportBatch.objects.exists())

    def test_wrong_batch_owner_and_non_admin_requests_are_rejected(self):
        other = self.actor('other_admin')
        key = uuid.uuid4()
        with self.assertRaises(UserImportError):
            get_or_create_import_operation(self.admin, 'upload', key, 'a' * 64,
                                           lambda: self.batch(actor=other, key=key))
        self.assertFalse(UserImportBatch.objects.exists())
        other.is_superuser = False
        with self.assertRaises(PermissionDenied):
            get_or_create_import_operation(other, 'upload', key, 'a' * 64, lambda: self.batch(key=key))


@override_settings(**IMPORT_SETTINGS)
class UserImportConcurrencyTests(ImportModelData, TransactionTestCase):
    def test_two_simultaneous_upload_requests_create_one_batch(self):
        self.admin = self.actor()
        barrier = Barrier(2)
        key = uuid.uuid4()
        results, errors = [], []

        def reserve():
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.admin.pk)
                barrier.wait(timeout=10)
                operation, created = get_or_create_import_operation(actor, 'upload', key, 'a' * 64,
                                                                    lambda: self.batch(actor=actor, key=key))
                results.append((operation.batch_id, created))
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                close_old_connections()

        threads = [Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len({batch_id for batch_id, _ in results}), 1)
        self.assertCountEqual([created for _, created in results], [True, False])
        self.assertEqual(UserImportBatch.objects.count(), 1)
        self.assertEqual(UserImportOperation.objects.count(), 1)


class UserImportMigrationTests(TransactionTestCase):
    fixtures = ['language_small']

    def test_apply_reverse_preserves_existing_managed_accounts(self):
        user = User.objects.create_user('migration_user', password='Existing-password-2026')
        Profile.objects.create(user=user, language=Language.objects.get(key='CPP17'))
        Profile.objects.filter(user=user).update(rotate_password_on_login=True, account_session_epoch=7,
                                                 api_token='a' * 64, password_rotated_at=timezone.now())
        before_user = User.objects.values().get(pk=user.pk)
        before_profile = Profile.objects.values().get(user=user)
        executor = MigrationExecutor(connection)
        final = executor.loader.graph.leaf_nodes()
        previous = [('judge', '0239_managed_login_policy')]
        current = [('judge', '0240_user_import')]
        try:
            executor.migrate(previous)
            self.assertNotIn('judge_userimportbatch', connection.introspection.table_names())
            MigrationExecutor(connection).migrate(current)
            self.assertIn('judge_userimportoperation', connection.introspection.table_names())
            self.assertEqual(User.objects.values().get(pk=user.pk), before_user)
            self.assertEqual(Profile.objects.values().get(user=user), before_profile)
            MigrationExecutor(connection).migrate(previous)
            self.assertEqual(User.objects.values().get(pk=user.pk), before_user)
            self.assertEqual(Profile.objects.values().get(user=user), before_profile)
        finally:
            MigrationExecutor(connection).migrate(final)
