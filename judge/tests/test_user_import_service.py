import json
import uuid
from datetime import timedelta
from threading import Barrier, Event, Thread
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import AnonymousUser, Group, Permission, User
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import close_old_connections, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from social_django.models import UserSocialAuth

from judge.models import EFFECTIVE_MATH_ENGINES, Language, ManagedAccountEvent, Organization, Profile, \
    UserImportBatch, WebAuthnCredential
from judge.tests.test_user_import_csv import csv_bytes
from judge.tests.test_user_import_models import IMPORT_SETTINGS
from judge.utils.managed_login import MAX_SESSION_EPOCH, ManagedLoginRejected, begin_password_login, \
    locked_managed_account, on_admin_password_changed, set_login_policy, validate_pending_login
from judge.utils.user_import import UserImportError, parse_user_csv
from judge.utils.user_import_config import get_user_import_config
from judge.utils.user_import_service import ImportBatchConflict, capture_import_snapshot, execute_import_batch, \
    finalize_import_preview, invalidate_import_caches
from judge.utils.user_import_storage import prepare_import_password, store_import_rows

PASSWORD = 'Initial-import-password-2026'
NEW_PASSWORD = 'New-import-password-2026'


class ImportServiceData:
    fixtures = ['language_small']

    def user(self, username, **kwargs):
        user = User.objects.create_user(username, password=PASSWORD, **kwargs)
        Profile.objects.create(user=user, language=Language.objects.get(key='CPP17'))
        return user

    def setUp(self):
        super().setUp()
        self.admin = self.user('import_admin', is_staff=True, is_superuser=True)
        self.existing = self.user('existing', first_name='Old name', last_name='Old school', email='old@example.org')
        self.org_a = Organization.objects.create(slug='org_a', name='Org A', short_name='A')
        self.org_b = Organization.objects.create(slug='org_b', name='Org B', short_name='B')

    def batch(self, csv_rows, muted=False, actor=None):
        actor = actor or self.admin
        now = timezone.now()
        batch = UserImportBatch.objects.create(
            actor=actor, idempotency_key=uuid.uuid4(), muted=muted,
            preview_expires_at=now + timedelta(hours=1), secret_expires_at=now + timedelta(hours=24),
        )
        store_import_rows(batch, parse_user_csv(csv_bytes(csv_rows)))
        return batch

    def stage(self, rows, muted=False, actor=None):
        actor = actor or self.admin
        batch = self.batch(rows, muted, actor)
        pairs = capture_import_snapshot(actor, batch.pk, batch.version)
        for row, candidate in pairs:
            prepare_import_password(row, candidate)
        return finalize_import_preview(actor, batch.pk, batch.version)

    def claim(self, batch):
        batch.refresh_from_db()
        batch.status = UserImportBatch.Status.RUNNING
        batch.attempt_token = uuid.uuid4()
        batch.execution_attempts = 1
        batch.lease_expires_at = timezone.now() + timedelta(minutes=10)
        batch.save(update_fields=['status', 'attempt_token', 'execution_attempts', 'lease_expires_at'])
        return batch.attempt_token

    def apply(self, batch, actor=None):
        token = self.claim(batch)
        return execute_import_batch(actor or self.admin, batch.pk, batch.version, token)

    def account_state(self):
        return {
            'users': list(User.objects.order_by('pk').values()),
            'profiles': list(Profile.objects.order_by('pk').values()),
            'memberships': list(Profile.organizations.through.objects.order_by('pk').values()),
            'organizations': list(Organization.objects.order_by('pk').values()),
            'events': list(ManagedAccountEvent.objects.order_by('pk').values()),
        }


class PasswordChangedValidator:
    def validate(self, password, user=None):
        pass

    def password_changed(self, password, user=None):
        raise AssertionError('Must not silently invoke plaintext callbacks with an encoded hash')


@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[])
class ImportBatchServiceTests(ImportServiceData, TestCase):
    def test_mixed_create_update_mapping_and_defaults(self):
        self.existing.profile.organizations.add(self.org_a)
        profile = self.existing.profile
        profile.language = Language.objects.get(key='PY3')
        profile.timezone = 'UTC'
        profile.save(update_fields=['language', 'timezone'])
        batch = self.stage([
            ['new', NEW_PASSWORD, 'New name', 'UTC school', 'new@example.org', 'org_a&org_b', 'true'],
            ['existing', '', 'Updated name', '', '', 'org_b&org_a', 'false'],
        ], muted=True)
        before_hash = self.existing.password
        result = self.apply(batch)
        self.assertEqual((result.total, result.created, result.updated), (2, 1, 1))
        new = User.objects.get(username='new')
        self.assertTrue(new.check_password(NEW_PASSWORD))
        self.assertEqual((new.first_name, new.last_name, new.email), ('New name', 'UTC school', 'new@example.org'))
        self.assertTrue(new.is_active)
        self.assertFalse(new.is_staff)
        self.assertFalse(new.is_superuser)
        self.assertEqual(new.profile.language.key, 'CPP17')
        self.assertEqual(new.profile.timezone, settings.DEFAULT_USER_TIME_ZONE)
        self.assertTrue(new.profile.rotate_password_on_login)
        self.assertEqual(new.profile.account_session_epoch, 1)
        self.assertIsNone(new.profile.password_rotated_at)
        self.existing.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(self.existing.password, before_hash)
        self.assertEqual((self.existing.first_name, self.existing.last_name, self.existing.email),
                         ('Updated name', 'Old school', 'old@example.org'))
        self.assertEqual((profile.language.key, profile.timezone), ('PY3', 'UTC'))
        for target in (new.profile, profile):
            self.assertTrue(target.mute)
            self.assertEqual(set(target.organizations.values_list('pk', flat=True)), {self.org_a.pk, self.org_b.pk})
        for org in (self.org_a, self.org_b):
            org.refresh_from_db()
            self.assertEqual(org.member_count, 2)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'COMPLETED')
        self.assertEqual(batch.rows.filter(status='SUCCESS').count(), 2)
        self.assertFalse(batch.rows.exclude(password_ciphertext='', prepared_password_ciphertext='').exists())
        for row in batch.rows.all():
            self.assertNotIn(NEW_PASSWORD, json.dumps([row.input_data, row.baseline, row.preview, row.result]))

    def test_all_policy_password_combinations(self):
        for old in (False, True):
            for enabled in (False, True):
                for provided in (False, True):
                    with self.subTest(old=old, enabled=enabled, provided=provided):
                        name = f'case_{int(old)}_{int(enabled)}_{int(provided)}'
                        user = self.user(name)
                        rotated_at = timezone.now() - timedelta(days=1)
                        Profile.objects.filter(user=user).update(rotate_password_on_login=old, account_session_epoch=5,
                                                                 api_token='a' * 64, password_rotated_at=rotated_at)
                        old_hash, old_auth_hash = user.password, user.get_session_auth_hash()
                        batch = self.stage([[name, NEW_PASSWORD if provided else '', '', '', '', '',
                                             'true' if enabled else 'false']])
                        self.apply(batch)
                        user.refresh_from_db()
                        profile = Profile.objects.get(user=user)
                        delta = int(old and provided) + int(old != enabled)
                        self.assertEqual(profile.account_session_epoch, 5 + delta)
                        self.assertEqual(profile.rotate_password_on_login, enabled)
                        self.assertEqual(profile.password_rotated_at, rotated_at)
                        self.assertEqual(profile.api_token, None if delta else 'a' * 64)
                        self.assertEqual(user.password == old_hash, not provided)
                        self.assertEqual(user.get_session_auth_hash() == old_auth_hash, not provided)
                        self.assertTrue(user.check_password(NEW_PASSWORD if provided else PASSWORD))
                        events = list(ManagedAccountEvent.objects.filter(target=user).order_by('pk'))
                        expected = (['credential_reissued'] if old and provided else [])
                        if old != enabled:
                            expected.append('policy_enabled' if enabled else 'policy_disabled')
                        self.assertEqual([event.event for event in events], expected)
                        self.assertTrue(all(event.actor_id == self.admin.pk for event in events))

    def test_new_account_rotation_off_and_empty_fields(self):
        batch = self.stage([['new', NEW_PASSWORD, '', '', '', '', 'false']], muted=False)
        self.apply(batch)
        new = User.objects.get(username='new')
        self.assertEqual((new.first_name, new.last_name, new.email), ('', '', ''))
        self.assertFalse(new.profile.mute)
        self.assertFalse(new.profile.rotate_password_on_login)
        self.assertEqual(new.profile.account_session_epoch, 0)

    def test_metadata_only_does_not_revoke_sessions_or_credentials(self):
        profile = self.existing.profile
        Profile.objects.filter(pk=profile.pk).update(api_token='b' * 64)
        before = self.existing.get_session_auth_hash()
        batch = self.stage([['existing', '', 'Changed', '', '', '', 'false']])
        self.apply(batch)
        self.existing.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(self.existing.get_session_auth_hash(), before)
        self.assertEqual((profile.account_session_epoch, profile.api_token), (0, 'b' * 64))
        self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_credential_reissue_invalidates_pending_2fa_without_removing_factors(self):
        profile = self.existing.profile
        profile.is_totp_enabled = True
        profile.totp_key = 'JBSWY3DPEHPK3PXP'
        profile.rotate_password_on_login = True
        profile.save(update_fields=['is_totp_enabled', 'totp_key', 'rotate_password_on_login'])
        batch = self.stage([['existing', NEW_PASSWORD, '', '', '', '', 'true']])
        user = User.objects.get(pk=self.existing.pk)
        user.backend = 'django.contrib.auth.backends.ModelBackend'
        request = RequestFactory().post('/accounts/login/')
        request.user, request.session = AnonymousUser(), SessionStore()
        self.assertTrue(begin_password_login(request, user, PASSWORD))
        self.apply(batch)
        user = User.objects.get(pk=self.existing.pk)
        profile = Profile.objects.get(user=user)
        with self.assertRaises(ManagedLoginRejected):
            validate_pending_login(request, user, profile)
        self.assertTrue(profile.is_totp_enabled)
        self.assertEqual(profile.totp_key, 'JBSWY3DPEHPK3PXP')

    def test_organizations_union_and_performance_points_are_preserved(self):
        Profile.objects.filter(user=self.existing).update(performance_points=100)
        self.existing.profile.organizations.add(self.org_a)
        batch = self.stage([['existing', '', '', '', '', 'org_b&org_a', 'false']])
        self.apply(batch)
        self.assertEqual(self.existing.profile.organizations.count(), 2)
        for org in (self.org_a, self.org_b):
            org.refresh_from_db()
            self.assertEqual(org.member_count, 1)
            self.assertAlmostEqual(org.performance_points, 100 * settings.VNOJ_ORG_PP_SCALE)

    def test_preserves_oauth_2fa_groups_permissions_and_profile_preferences(self):
        profile = self.existing.profile
        profile.is_totp_enabled, profile.is_webauthn_enabled = True, True
        profile.totp_key = 'JBSWY3DPEHPK3PXP'
        profile.scratch_codes = '["12345678"]'
        profile.about = 'Keep my profile'
        profile.save(update_fields=['is_totp_enabled', 'is_webauthn_enabled', 'totp_key', 'scratch_codes', 'about'])
        credential = WebAuthnCredential.objects.create(user=profile, name='key', cred_id='existing-credential',
                                                       public_key='existing-public-key', counter=10)
        social = UserSocialAuth.objects.create(user=self.existing, provider='github', uid='existing-social')
        group = Group.objects.create(name='Keep group')
        self.existing.groups.add(group)
        permission = Permission.objects.get(codename='change_user', content_type__app_label='auth')
        self.existing.user_permissions.add(permission)
        before_profile = Profile.objects.values().get(pk=profile.pk)
        batch = self.stage([['existing', NEW_PASSWORD, 'Changed', '', '', '', 'true']])
        self.apply(batch)
        profile.refresh_from_db()
        after = Profile.objects.values().get(pk=profile.pk)
        for name in before_profile:
            if name not in ('rotate_password_on_login', 'account_session_epoch', 'api_token'):
                self.assertEqual(after[name], before_profile[name], name)
        self.assertEqual(list(self.existing.groups.values_list('pk', flat=True)), [group.pk])
        self.assertEqual(list(self.existing.user_permissions.values_list('pk', flat=True)), [permission.pk])
        self.assertEqual(UserSocialAuth.objects.get(pk=social.pk).uid, 'existing-social')
        self.assertEqual(WebAuthnCredential.objects.get(pk=credential.pk).counter, 10)

    def test_snapshot_and_validation_only_write_importer_tables(self):
        batch = self.batch([['existing', NEW_PASSWORD, 'Changed', '', '', 'org_a', 'true'],
                            ['new', NEW_PASSWORD, '', '', '', '', 'false']])
        before = self.account_state()
        pairs = capture_import_snapshot(self.admin, batch.pk, 1)
        self.assertEqual(pairs[0][1].first_name, 'Changed')
        for row, candidate in pairs:
            prepare_import_password(row, candidate)
        ready = finalize_import_preview(self.admin, batch.pk, 1)
        self.assertEqual(ready.status, 'PREVIEW_READY')
        self.assertEqual(self.account_state(), before)

    def test_snapshot_capture_and_finalize_retries_do_not_extend_preview(self):
        batch = self.batch([['existing', NEW_PASSWORD, '', '', '', '', 'false']])
        pairs = capture_import_snapshot(self.admin, batch.pk, 1)
        original = UserImportBatch.objects.get(pk=batch.pk).snapshot_digest
        again = capture_import_snapshot(self.admin, batch.pk, 1)
        self.assertEqual(UserImportBatch.objects.get(pk=batch.pk).snapshot_digest, original)
        for row, candidate in again:
            prepare_import_password(row, candidate)
        ready = finalize_import_preview(self.admin, batch.pk, 1)
        expiry, digest = ready.preview_expires_at, ready.snapshot_digest
        retry = finalize_import_preview(self.admin, batch.pk, 1)
        self.assertEqual((retry.preview_expires_at, retry.snapshot_digest), (expiry, digest))
        self.assertEqual(pairs[0][0].pk, again[0][0].pk)

    def test_all_passwords_must_be_validated_before_preview(self):
        batch = self.batch([['new', NEW_PASSWORD, '', '', '', '', 'true']])
        capture_import_snapshot(self.admin, batch.pk, 1)
        with self.assertRaises(ImportBatchConflict):
            finalize_import_preview(self.admin, batch.pk, 1)
        self.assertFalse(User.objects.filter(username='new').exists())

    def test_account_change_during_password_validation_blocks_finalize(self):
        batch = self.batch([['existing', NEW_PASSWORD, '', '', '', '', 'true']])
        pairs = capture_import_snapshot(self.admin, batch.pk, 1)
        for row, candidate in pairs:
            prepare_import_password(row, candidate)
        User.objects.filter(pk=self.existing.pk).update(email='changed-after-validation@example.org')
        with self.assertRaises(ImportBatchConflict):
            finalize_import_preview(self.admin, batch.pk, 1)

    def test_security_and_target_field_changes_after_preview_are_conflicts(self):
        changes = [('password', {'password': 'a-new-encoded-value'}), ('name', {'first_name': 'Someone else'}),
                   ('email', {'email': 'changed@example.org'})]
        for suffix, fields in changes:
            with self.subTest(suffix=suffix):
                user = self.user('conflict_' + suffix)
                batch = self.stage([[user.username, NEW_PASSWORD, 'Proposed', '', '', '', 'true']])
                User.objects.filter(pk=user.pk).update(**fields)
                before = self.account_state()
                with self.assertRaises(ImportBatchConflict):
                    self.apply(batch)
                self.assertEqual(self.account_state(), before)

    def test_unrelated_fields_can_change_and_are_not_overwritten(self):
        batch = self.stage([['existing', '', '', '', 'updated@example.org', '', 'false']])
        User.objects.filter(pk=self.existing.pk).update(first_name='New concurrent name')
        Profile.objects.filter(user=self.existing).update(last_access=timezone.now(), about='Concurrent profile')
        self.apply(batch)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.first_name, 'New concurrent name')
        self.assertEqual(self.existing.email, 'updated@example.org')
        self.assertEqual(self.existing.profile.about, 'Concurrent profile')

    def test_create_does_not_become_update_if_username_appears(self):
        batch = self.stage([['new', NEW_PASSWORD, 'Proposed', '', '', '', 'false']])
        other = self.user('new', first_name='Concurrent account')
        before = self.account_state()
        with self.assertRaises(ImportBatchConflict):
            self.apply(batch)
        self.assertEqual(self.account_state(), before)
        self.assertEqual(User.objects.get(pk=other.pk).first_name, 'Concurrent account')

    def test_batch_a_preview_then_batch_b_import_same_new_username(self):
        batch_a = self.stage([['new_x', NEW_PASSWORD, 'Proposed by A', '', '', '', 'true']], muted=True)
        self.assertEqual(batch_a.rows.get().action, 'CREATE')
        self.assertIsNone(batch_a.rows.get().target_id)
        password_b = 'B-issued-password-2026'
        batch_b = self.stage([['new_x', password_b, 'Created by B', '', '', '', 'false']], muted=False)
        self.apply(batch_b)
        before = self.account_state()
        with self.assertRaises(ImportBatchConflict) as error:
            self.apply(batch_a)
        self.assertEqual(error.exception.code, 'username_conflict')
        self.assertEqual([issue.row for issue in error.exception.issues], [1])
        self.assertEqual(self.account_state(), before)
        x = User.objects.get(username='new_x')
        self.assertEqual(x.first_name, 'Created by B')
        self.assertTrue(x.check_password(password_b))
        self.assertFalse(x.check_password(NEW_PASSWORD))
        self.assertFalse(x.profile.mute)
        self.assertFalse(x.profile.rotate_password_on_login)
        self.assertEqual(UserImportBatch.objects.get(pk=batch_b.pk).status, 'COMPLETED')
        self.assertEqual(UserImportBatch.objects.get(pk=batch_a.pk).applied_rows, 0)

    def test_password_validator_policy_change_requires_new_preview(self):
        batch = self.stage([['existing', NEW_PASSWORD, '', '', '', '', 'true']])
        validators = [{'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
                       'OPTIONS': {'min_length': 100}}]
        with override_settings(AUTH_PASSWORD_VALIDATORS=validators), self.assertRaises(ImportBatchConflict):
            self.apply(batch)
        self.assertTrue(User.objects.get(pk=self.existing.pk).check_password(PASSWORD))

    def test_deleted_update_target_is_not_recreated(self):
        batch = self.stage([['existing', NEW_PASSWORD, '', '', '', '', 'false']])
        self.existing.delete()
        with self.assertRaises(ImportBatchConflict):
            self.apply(batch)
        self.assertFalse(User.objects.filter(username='existing').exists())

    def test_changed_or_deleted_organization_blocks_whole_batch(self):
        for operation in ('rename', 'replace'):
            with self.subTest(operation=operation):
                org = Organization.objects.create(slug='temp_' + operation, name='Temp', short_name='T')
                batch = self.stage([['new_' + operation, NEW_PASSWORD, '', '', '', org.slug, 'true']])
                if operation == 'rename':
                    Organization.objects.filter(pk=org.pk).update(slug='renamed')
                else:
                    slug = org.slug
                    org.delete()
                    Organization.objects.create(slug=slug, name='Replacement', short_name='R')
                with self.assertRaises(ImportBatchConflict):
                    self.apply(batch)
                self.assertFalse(User.objects.filter(username='new_' + operation).exists())

    def test_privilege_or_activity_change_after_preview_is_rejected(self):
        for fields in ({'is_staff': True}, {'is_superuser': True}, {'is_active': False}):
            batch = self.stage([['existing', '', 'Changed', '', '', '', 'false']])
            User.objects.filter(pk=self.existing.pk).update(**fields)
            with self.assertRaises(ImportBatchConflict):
                self.apply(batch)
            User.objects.filter(pk=self.existing.pk).update(is_staff=False, is_superuser=False, is_active=True)

    def test_cached_actor_rights_and_changed_actor_credentials_are_not_trusted(self):
        batch = self.stage([['existing', '', 'Changed', '', '', '', 'false']])
        User.objects.filter(pk=self.admin.pk).update(is_superuser=False)
        with self.assertRaises(PermissionDenied):
            self.apply(batch)
        User.objects.filter(pk=self.admin.pk).update(is_superuser=True, password='changed-credential')
        with self.assertRaises(ImportBatchConflict):
            execute_import_batch(self.admin, batch.pk, 1, batch.attempt_token)

    def test_other_actor_and_impersonation_cannot_apply(self):
        batch = self.stage([['existing', '', 'Changed', '', '', '', 'false']])
        token = self.claim(batch)
        other = self.user('other_admin', is_staff=True, is_superuser=True)
        with self.assertRaises(PermissionDenied):
            execute_import_batch(other, batch.pk, 1, token)
        self.admin.is_impersonate = True
        with self.assertRaises(PermissionDenied):
            execute_import_batch(self.admin, batch.pk, 1, token)

    def test_version_attempt_lease_and_expiry_guards(self):
        batch = self.stage([['existing', '', 'Changed', '', '', '', 'false']])
        token = self.claim(batch)
        with self.assertRaises(ImportBatchConflict):
            execute_import_batch(self.admin, batch.pk, 2, token)
        with self.assertRaises(UserImportError):
            execute_import_batch(self.admin, batch.pk, 1, uuid.uuid4())
        for updates in ({'lease_expires_at': timezone.now() - timedelta(seconds=1)}, {'execution_attempts': 4}):
            with self.subTest(updates=list(updates)):
                UserImportBatch.objects.filter(pk=batch.pk).update(**updates)
                with self.assertRaises(UserImportError):
                    execute_import_batch(self.admin, batch.pk, 1, token)
                self.claim(batch)
                token = batch.attempt_token
        UserImportBatch.objects.filter(pk=batch.pk).update(preview_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(ImportBatchConflict):
            execute_import_batch(self.admin, batch.pk, 1, token)

    def test_tampered_preview_or_input_or_prepared_password_is_rejected(self):
        for changed in ('input', 'preview', 'ciphertext'):
            with self.subTest(changed=changed):
                batch = self.stage([['existing', NEW_PASSWORD, 'Proposed', '', '', '', 'true']])
                row = batch.rows.get()
                if changed == 'input':
                    row.input_data['name'] = 'Changed after preview'
                elif changed == 'preview':
                    row.preview['warnings'] = ['changed']
                else:
                    row.prepared_password_ciphertext += 'tampered'
                row.save()
                with self.assertRaises(ImportBatchConflict):
                    self.apply(batch)
        self.assertTrue(User.objects.get(pk=self.existing.pk).check_password(PASSWORD))

    def test_replayed_completion_does_not_reissue_credentials_or_add_events(self):
        batch = self.stage([['new', NEW_PASSWORD, '', '', '', '', 'true'],
                            ['existing', NEW_PASSWORD, '', '', '', '', 'true']])
        result = self.apply(batch)
        before = self.account_state()
        retry = execute_import_batch(self.admin, batch.pk, 1, batch.attempt_token)
        self.assertTrue(retry.replayed)
        self.assertEqual((result.total, result.created, result.updated), (retry.total, retry.created, retry.updated))
        self.assertEqual(self.account_state(), before)

    def test_disable_does_not_restore_previously_consumed_password(self):
        Profile.objects.filter(user=self.existing).update(rotate_password_on_login=True, account_session_epoch=3)
        self.existing.set_password('Unknown-consumed-password')
        self.existing.save(update_fields=['password'])
        before = self.existing.password
        batch = self.stage([['existing', '', '', '', '', '', 'false']])
        self.assertIn('password_not_restored', batch.rows.get().preview['warnings'])
        self.apply(batch)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.password, before)
        self.assertFalse(self.existing.check_password(PASSWORD))

    def test_epoch_exhaustion_rolls_back_new_user_and_password_issue(self):
        Profile.objects.filter(user=self.existing).update(rotate_password_on_login=True,
                                                          account_session_epoch=MAX_SESSION_EPOCH - 1)
        batch = self.stage([['new', NEW_PASSWORD, '', '', '', '', 'true'],
                            ['existing', NEW_PASSWORD, '', '', '', '', 'false']])
        before = self.account_state()
        with self.assertRaises(ValidationError):
            self.apply(batch)
        self.assertEqual(self.account_state(), before)

    def test_late_failure_rolls_back_every_account_membership_event_and_result(self):
        from judge.utils import user_import_service as service
        for failure_at in (60, 100):
            with self.subTest(failure_at=failure_at):
                data = [[f'new_{failure_at}_{i}', NEW_PASSWORD, '', '', '', 'org_a', 'true'] for i in range(99)]
                data.insert(0, ['existing', NEW_PASSWORD, 'Changed', '', '', 'org_b', 'true'])
                batch = self.stage(data, muted=True)
                before = self.account_state()
                original = service._apply_row
                calls = 0

                def fail(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    result = original(*args, **kwargs)
                    if calls == failure_at:
                        raise RuntimeError('Injected late failure')
                    return result

                with self.captureOnCommitCallbacks(execute=True), patch('judge.signals.cache.delete_many') as delete:
                    with patch.object(service, '_apply_row', side_effect=fail), self.assertRaises(RuntimeError):
                        self.apply(batch)
                    delete.assert_not_called()
                self.assertEqual(self.account_state(), before)
                batch.refresh_from_db()
                self.assertEqual((batch.status, batch.applied_rows), ('RUNNING', 0))
                self.assertEqual(batch.rows.filter(status='SUCCESS').count(), 0)
                self.assertEqual(batch.rows.exclude(prepared_password_ciphertext='').count(), 100)

    def test_cache_invalidation_is_deferred_until_commit(self):
        batch = self.stage([['existing', '', 'Changed', '', '', 'org_a', 'true']], muted=True)
        with patch('judge.signals.cache.delete_many') as delete:
            with self.captureOnCommitCallbacks(execute=True):
                self.apply(batch)
                delete.assert_not_called()
            self.assertGreater(delete.call_count, 0)

    def test_plaintext_password_changed_hooks_are_rejected_explicitly(self):
        path = 'judge.tests.test_user_import_service.PasswordChangedValidator'
        with override_settings(AUTH_PASSWORD_VALIDATORS=[{'NAME': path}]), self.assertRaises(ImproperlyConfigured):
            get_user_import_config()


@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[])
class ImportBatchTransactionTests(ImportServiceData, TransactionTestCase):
    def race(self, *actions):
        barrier = Barrier(len(actions))
        results = []

        def run(index, action):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                result = action()
                results.append((index, 'ok', result))
            except Exception as exc:
                results.append((index, type(exc).__name__, None))
            finally:
                close_old_connections()

        threads = [Thread(target=run, args=(i, action)) for i, action in enumerate(actions)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
            self.assertFalse(thread.is_alive())
        return sorted(results)

    def test_duplicate_workers_apply_one_batch_once(self):
        batch = self.stage([['new', NEW_PASSWORD, '', '', '', 'org_a', 'true']])
        token = self.claim(batch)
        actions = [lambda: execute_import_batch(self.admin, batch.pk, 1, token)] * 2
        results = self.race(*actions)
        self.assertEqual([state for _, state, _ in results], ['ok', 'ok'])
        self.assertCountEqual([result.replayed for _, _, result in results], [False, True])
        self.assertEqual(User.objects.filter(username='new').count(), 1)
        self.assertEqual(ManagedAccountEvent.objects.count(), 1)

    def test_overlapping_batches_in_reverse_csv_order_have_no_partial_writes(self):
        other = self.user('other')
        batch_a = self.stage([['existing', '', 'A existing', '', '', 'org_a', 'true'],
                              ['other', '', 'A other', '', '', 'org_b', 'true']])
        batch_b = self.stage([['other', '', 'B other', '', '', 'org_b', 'true'],
                              ['existing', '', 'B existing', '', '', 'org_a', 'true']])
        token_a, token_b = self.claim(batch_a), self.claim(batch_b)
        results = self.race(lambda: execute_import_batch(self.admin, batch_a.pk, 1, token_a),
                            lambda: execute_import_batch(self.admin, batch_b.pk, 1, token_b))
        self.assertCountEqual([state for _, state, _ in results], ['ok', 'ImportBatchConflict'])
        self.existing.refresh_from_db()
        other.refresh_from_db()
        prefix = self.existing.first_name[0]
        self.assertEqual(other.first_name[0], prefix)
        self.assertEqual(ManagedAccountEvent.objects.count(), 2)

    def test_competing_new_username_batches_do_not_create_two_accounts(self):
        other_admin = self.user('second_admin', is_staff=True, is_superuser=True)
        batch_a = self.stage([['new', NEW_PASSWORD, '', '', '', '', 'true']])
        batch_b = self.stage([['new', NEW_PASSWORD, '', '', '', '', 'true']], actor=other_admin)
        token_a, token_b = self.claim(batch_a), self.claim(batch_b)
        results = self.race(lambda: execute_import_batch(self.admin, batch_a.pk, 1, token_a),
                            lambda: execute_import_batch(other_admin, batch_b.pk, 1, token_b))
        states = [state for _, state, _ in results]
        self.assertEqual(states.count('ok'), 1)
        self.assertTrue(set(states) <= {'ok', 'ImportBatchConflict', 'IntegrityError', 'OperationalError'}, states)
        self.assertEqual(User.objects.filter(username='new').count(), 1)
        self.assertEqual(ManagedAccountEvent.objects.count(), 1)

    def test_b_creates_x_after_a_lookup_but_before_a_insert(self):
        from judge.utils import user_import_service as service
        other_admin = self.user('second_admin', is_staff=True, is_superuser=True)
        batch_a = self.stage([['existing', NEW_PASSWORD, 'A proposed update', '', '', '', 'true'],
                              ['new_x', NEW_PASSWORD, 'A proposed create', '', '', '', 'true']])
        batch_b = self.stage([['new_x', 'B-issued-password-2026', 'Created by B', '', '', '', 'false']],
                             actor=other_admin)
        token_a, token_b = self.claim(batch_a), self.claim(batch_b)
        original = service._apply_row
        reached, release = Event(), Event()
        errors = []
        old_user = User.objects.values().get(pk=self.existing.pk)
        old_profile = Profile.objects.values().get(user=self.existing)

        def pause_before_x(actor, batch, row, *args):
            if batch.pk == batch_a.pk and row.username == 'new_x':
                reached.set()
                if not release.wait(timeout=15):
                    raise RuntimeError('Unique username race timed out')
            return original(actor, batch, row, *args)

        def apply_a():
            close_old_connections()
            try:
                execute_import_batch(self.admin, batch_a.pk, 1, token_a)
            except Exception as exc:
                errors.append((type(exc).__name__, getattr(exc, 'code', '')))
            finally:
                close_old_connections()

        with patch.object(service, '_apply_row', side_effect=pause_before_x):
            worker = Thread(target=apply_a)
            worker.start()
            try:
                self.assertTrue(reached.wait(timeout=10))
                execute_import_batch(other_admin, batch_b.pk, 1, token_b)
            finally:
                release.set()
                worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [('ImportBatchConflict', 'username_conflict')])
        self.assertEqual(User.objects.values().get(pk=self.existing.pk), old_user)
        self.assertEqual(Profile.objects.values().get(user=self.existing), old_profile)
        x = User.objects.get(username='new_x')
        self.assertTrue(x.check_password('B-issued-password-2026'))
        self.assertEqual(x.first_name, 'Created by B')
        self.assertEqual(ManagedAccountEvent.objects.filter(target=self.existing).count(), 0)
        self.assertEqual(UserImportBatch.objects.get(pk=batch_a.pk).applied_rows, 0)
        self.assertEqual(UserImportBatch.objects.get(pk=batch_b.pk).status, 'COMPLETED')

    def test_login_and_policy_import_are_serialized(self):
        Profile.objects.filter(user=self.existing).update(rotate_password_on_login=True)
        batch = self.stage([['existing', '', '', '', '', '', 'false']])
        token = self.claim(batch)

        def login():
            user = User.objects.get(pk=self.existing.pk)
            user.backend = 'django.contrib.auth.backends.ModelBackend'
            request = RequestFactory().post('/accounts/login/')
            request.user = AnonymousUser()
            request.session = SessionStore()
            begin_password_login(request, user, PASSWORD)
            return request.session[SESSION_KEY]

        results = self.race(lambda: execute_import_batch(self.admin, batch.pk, 1, token), login)
        self.assertEqual(results[1][1], 'ok')
        self.assertIn(results[0][1], ('ok', 'ImportBatchConflict'))
        profile = Profile.objects.get(user=self.existing)
        self.assertEqual(profile.account_session_epoch, 1)
        self.existing.refresh_from_db()
        if results[0][1] == 'ok':
            self.assertFalse(profile.rotate_password_on_login)
            self.assertTrue(self.existing.check_password(PASSWORD))
        else:
            self.assertTrue(profile.rotate_password_on_login)
            self.assertFalse(self.existing.check_password(PASSWORD))

    def test_admin_password_reset_and_import_do_not_overwrite_credentials(self):
        Profile.objects.filter(user=self.existing).update(rotate_password_on_login=True)
        other_admin = self.user('second_admin', is_staff=True, is_superuser=True)
        batch = self.stage([['existing', '', 'Imported name', '', '', '', 'true']])
        token = self.claim(batch)

        def reset():
            with locked_managed_account(other_admin, self.existing.pk, ('auth.change_user',)) as (actor, user, _):
                user.set_password(NEW_PASSWORD)
                user.save(update_fields=['password'])
                on_admin_password_changed(actor, user)

        results = self.race(lambda: execute_import_batch(self.admin, batch.pk, 1, token), reset)
        self.assertEqual(results[1][1], 'ok')
        self.assertIn(results[0][1], ('ok', 'ImportBatchConflict'))
        self.existing.refresh_from_db()
        self.assertTrue(self.existing.check_password(NEW_PASSWORD))
        self.assertEqual(self.existing.profile.account_session_epoch, 1)

    def test_admin_policy_edit_and_import_change_epoch_only_once(self):
        Profile.objects.filter(user=self.existing).update(rotate_password_on_login=True)
        other_admin = self.user('second_admin', is_staff=True, is_superuser=True)
        batch = self.stage([['existing', '', '', '', '', '', 'false']])
        token = self.claim(batch)
        results = self.race(lambda: execute_import_batch(self.admin, batch.pk, 1, token),
                            lambda: set_login_policy(other_admin, self.existing.pk, False, 0))
        states = [state for _, state, _ in results]
        self.assertEqual(states.count('ok'), 1, states)
        self.assertTrue(set(states) <= {'ok', 'ImportBatchConflict', 'LoginPolicyConflict'}, states)
        self.assertFalse(Profile.objects.get(user=self.existing).rotate_password_on_login)
        self.assertEqual(Profile.objects.get(user=self.existing).account_session_epoch, 1)
        self.assertEqual(ManagedAccountEvent.objects.count(), 1)

    def test_other_connection_sees_no_partial_batch_and_all_changes_after_commit(self):
        from judge.utils import user_import_service as service
        batch = self.stage([['first_new', NEW_PASSWORD, '', '', '', '', 'false'],
                            ['last_new', NEW_PASSWORD, '', '', '', '', 'false']])
        token = self.claim(batch)
        reached, release = Event(), Event()
        original = service._apply_row
        errors = []

        def wait_after_first(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[2].row_number == 1:
                reached.set()
                if not release.wait(timeout=15):
                    raise RuntimeError('Visibility test timed out')
            return result

        def apply():
            close_old_connections()
            try:
                execute_import_batch(self.admin, batch.pk, 1, token)
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                close_old_connections()

        with patch.object(service, '_apply_row', side_effect=wait_after_first):
            worker = Thread(target=apply)
            worker.start()
            try:
                self.assertTrue(reached.wait(timeout=10))
                self.assertFalse(User.objects.filter(username__in=['first_new', 'last_new']).exists())
                current = UserImportBatch.objects.get(pk=batch.pk)
                self.assertEqual((current.status, current.applied_rows), ('RUNNING', 0))
            finally:
                release.set()
                worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(User.objects.filter(username__in=['first_new', 'last_new']).count(), 2)
        self.assertEqual(UserImportBatch.objects.get(pk=batch.pk).applied_rows, 2)

    def test_callback_failure_after_commit_does_not_mark_import_failed(self):
        batch = self.stage([['existing', NEW_PASSWORD, '', '', '', 'org_a', 'true']], muted=True)
        token = self.claim(batch)
        with patch('judge.signals.cache.delete_many', side_effect=RuntimeError('Cache offline')):
            with self.assertLogs('django.db.backends.base', level='ERROR'):
                result = execute_import_batch(self.admin, batch.pk, 1, token)
        self.assertEqual(result.total, 1)
        self.assertEqual(UserImportBatch.objects.get(pk=batch.pk).status, 'COMPLETED')
        before = self.account_state()
        retry = execute_import_batch(self.admin, batch.pk, 1, token)
        self.assertTrue(retry.replayed)
        self.assertEqual(self.account_state(), before)
        key = make_template_fragment_key('user_about', (self.existing.profile.pk, EFFECTIVE_MATH_ENGINES[0]))
        cache.set(key, 'stale')
        invalidate_import_caches(batch.pk)
        self.assertIsNone(cache.get(key))
        self.assertEqual(self.account_state(), before)

    def test_executor_requires_its_own_outer_transaction(self):
        batch = self.stage([['existing', '', 'Changed', '', '', '', 'false']])
        token = self.claim(batch)
        with transaction.atomic(), self.assertRaises(RuntimeError):
            execute_import_batch(self.admin, batch.pk, 1, token)
