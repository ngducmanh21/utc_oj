from importlib import import_module
from threading import Barrier, Thread
from unittest.mock import patch

from django.conf import settings
from django.contrib import auth
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.auth.models import AnonymousUser, Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from reversion import revisions
from reversion.models import Version

from judge.forms import ProfileForm
from judge.middleware import APIMiddleware, DMOJLoginMiddleware
from judge.models import Language, ManagedAccountEvent, Profile
from judge.utils.managed_login import LoginPolicyConflict, MAX_SESSION_EPOCH, SESSION_EPOCH_KEY, \
    session_epoch_matches, set_login_policy, stamp_session_epoch


class ManagedAccountData:
    fixtures = ['language_small']

    def create_user(self, username, **kwargs):
        user = User.objects.create_user(username, password='Initial-password-2026', **kwargs)
        Profile.objects.create(user=user, language=Language.objects.get(key='CPP17'))
        return user

    def create_actor(self, username='invigilator'):
        user = self.create_user(username, is_staff=True)
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=['change_user', 'manage_login_password_rotation'],
            content_type__app_label__in=['auth', 'judge'],
        ))
        return user


@override_settings(DEFAULT_USER_LANGUAGE='CPP17')
class LoginPolicyTests(ManagedAccountData, TestCase):
    def setUp(self):
        self.actor = self.create_actor()
        self.user = self.create_user('contestant')

    def change(self, enabled=True, epoch=0, actor=None, user=None, reason='Exam room A101'):
        return set_login_policy(self.actor if actor is None else actor,
                                self.user.pk if user is None else user.pk, enabled, epoch, reason)

    def test_new_profiles_default_to_normal_login(self):
        profile = self.user.profile
        self.assertFalse(profile.rotate_password_on_login)
        self.assertEqual(profile.account_session_epoch, 0)
        self.assertIsNone(profile.password_rotated_at)
        self.assertEqual(ManagedAccountEvent.objects.count(), 0)

    def test_enable_and_disable_revoke_tokens_without_changing_password(self):
        password_hash = self.user.password
        Profile.objects.filter(user=self.user).update(api_token='a' * 64)
        state = self.change()
        self.assertTrue(state.enabled)
        self.assertEqual(state.epoch, 1)
        self.assertEqual(state.user_id, self.user.pk)
        self.assertIsNone(state.rotated_at)
        profile = Profile.objects.get(user=self.user)
        self.assertIsNone(profile.api_token)
        Profile.objects.filter(pk=profile.pk).update(api_token='b' * 64)
        state = self.change(enabled=False, epoch=1)
        self.assertFalse(state.enabled)
        self.assertEqual(state.epoch, 2)
        self.assertIsNone(Profile.objects.get(pk=profile.pk).api_token)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, password_hash)
        self.assertTrue(self.user.check_password('Initial-password-2026'))
        events = list(ManagedAccountEvent.objects.order_by('pk'))
        self.assertEqual([event.event for event in events], ['policy_enabled', 'policy_disabled'])
        self.assertEqual([(event.previous_epoch, event.epoch) for event in events], [(0, 1), (1, 2)])
        self.assertEqual([(event.previous_enabled, event.enabled) for event in events], [(False, True), (True, False)])
        self.assertTrue(all(event.actor_id == self.actor.pk and event.target_id == self.user.pk for event in events))

    def test_matching_noop_does_not_change_epoch_token_or_audit(self):
        Profile.objects.filter(user=self.user).update(api_token='a' * 64)
        state = self.change(enabled=False)
        self.assertEqual(state.epoch, 0)
        self.assertEqual(Profile.objects.get(user=self.user).api_token, 'a' * 64)
        self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_stale_epoch_cannot_overwrite_policy(self):
        self.change()
        with self.assertRaises(LoginPolicyConflict):
            self.change(enabled=False, epoch=0)
        self.assertTrue(Profile.objects.get(user=self.user).rotate_password_on_login)
        self.assertEqual(ManagedAccountEvent.objects.count(), 1)

    def test_audit_failure_rolls_back_policy_epoch_and_token(self):
        Profile.objects.filter(user=self.user).update(api_token='a' * 64)
        with patch('judge.utils.managed_login.ManagedAccountEvent.objects.create', side_effect=RuntimeError('audit')):
            with self.assertRaises(RuntimeError):
                self.change()
        profile = Profile.objects.get(user=self.user)
        self.assertFalse(profile.rotate_password_on_login)
        self.assertEqual(profile.account_session_epoch, 0)
        self.assertEqual(profile.api_token, 'a' * 64)
        self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_requires_active_staff_with_both_permissions(self):
        for attrs in ({'is_active': False}, {'is_staff': False}):
            with self.subTest(attrs=attrs):
                User.objects.filter(pk=self.actor.pk).update(**attrs)
                with self.assertRaises(PermissionDenied):
                    self.change()
                User.objects.filter(pk=self.actor.pk).update(is_active=True, is_staff=True)
        for codename in ('change_user', 'manage_login_password_rotation'):
            with self.subTest(permission=codename):
                permission = Permission.objects.get(codename=codename)
                self.actor.user_permissions.remove(permission)
                with self.assertRaises(PermissionDenied):
                    self.change()
                self.actor.user_permissions.add(permission)
        self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_does_not_trust_cached_actor_permissions_or_staff_status(self):
        self.assertTrue(self.actor.has_perm('judge.manage_login_password_rotation'))
        self.actor.user_permissions.clear()
        with self.assertRaises(PermissionDenied):
            self.change()

    def test_anonymous_impersonated_and_deleted_actors_are_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.change(actor=AnonymousUser())
        self.actor.is_impersonate = True
        with self.assertRaises(PermissionDenied):
            self.change()
        self.actor.is_impersonate = False
        User.objects.filter(pk=self.actor.pk).delete()
        with self.assertRaises(PermissionDenied):
            self.change()

    def test_superuser_actor_can_manage_normal_accounts(self):
        self.actor.is_superuser = True
        self.actor.save(update_fields=['is_superuser'])
        self.actor.user_permissions.clear()
        self.assertTrue(self.change().enabled)

    def test_protected_and_inactive_targets_are_rejected(self):
        for attrs in ({'is_staff': True}, {'is_superuser': True}):
            with self.subTest(attrs=attrs):
                User.objects.filter(pk=self.user.pk).update(**attrs)
                with self.assertRaises(PermissionDenied):
                    self.change()
                User.objects.filter(pk=self.user.pk).update(is_staff=False, is_superuser=False)
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        with self.assertRaises(ValidationError):
            self.change()
        self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_invalid_inputs_leave_account_unchanged(self):
        inputs = [
            {'enabled': 1}, {'enabled': 'true'}, {'expected_epoch': True}, {'expected_epoch': -1},
            {'expected_epoch': '0'}, {'expected_epoch': MAX_SESSION_EPOCH + 1},
            {'reason': 'x' * 256}, {'reason': None}, {'user_id': True}, {'user_id': 0},
        ]
        for override in inputs:
            with self.subTest(override=override), self.assertRaises(ValidationError):
                set_login_policy(self.actor, **{
                    **{'user_id': self.user.pk, 'enabled': True, 'expected_epoch': 0, 'reason': 'Exam'},
                    **override,
                })
        self.assertFalse(ManagedAccountEvent.objects.exists())
        self.assertEqual(Profile.objects.get(user=self.user).account_session_epoch, 0)

    def test_policy_can_change_without_reason_and_still_records_audit(self):
        state = set_login_policy(self.actor, self.user.pk, True, 0)
        self.assertTrue(state.enabled)
        event = ManagedAccountEvent.objects.get()
        self.assertEqual(event.reason, '')
        self.assertEqual(event.actor_id, self.actor.pk)
        self.assertEqual((event.previous_epoch, event.epoch), (0, 1))
        set_login_policy(self.actor, self.user.pk, False, 1, reason='   ')
        self.assertEqual(ManagedAccountEvent.objects.first().reason, '')

    def test_epoch_cannot_wrap_or_decrease(self):
        Profile.objects.filter(user=self.user).update(account_session_epoch=MAX_SESSION_EPOCH)
        with self.assertRaises(ValidationError) as error:
            self.change(epoch=MAX_SESSION_EPOCH)
        self.assertEqual(error.exception.code, 'epoch_exhausted')
        self.assertEqual(Profile.objects.get(user=self.user).account_session_epoch, MAX_SESSION_EPOCH)

    def test_event_has_no_credential_payload_and_keeps_history_after_deletion(self):
        token = self.user.profile.generate_api_token()
        self.change(reason='  Verified by invigilator  ')
        event = ManagedAccountEvent.objects.get()
        self.assertEqual(event.reason, 'Verified by invigilator')
        self.assertEqual(event.target_username, self.user.username)
        self.assertNotIn('Initial-password-2026', str(ManagedAccountEvent.objects.values().get()))
        self.assertNotIn(token, str(ManagedAccountEvent.objects.values().get()))
        self.user.delete()
        self.actor.delete()
        event.refresh_from_db()
        self.assertIsNone(event.actor_id)
        self.assertIsNone(event.target_id)
        self.assertEqual(event.target_username, 'contestant')

    def test_permissions_exist_without_editable_audit_permissions(self):
        self.assertTrue(Permission.objects.filter(
            content_type__app_label='judge', content_type__model='profile',
            codename='manage_login_password_rotation',
        ).exists())
        permissions = Permission.objects.filter(content_type__app_label='judge',
                                                content_type__model='managedaccountevent')
        self.assertEqual(set(permissions.values_list('codename', flat=True)), {'view_managedaccountevent'})

    def test_profile_form_cannot_set_policy_or_epoch(self):
        form = ProfileForm(user=self.user, instance=self.user.profile, data={
            'about': '', 'timezone': self.user.profile.timezone, 'language': self.user.profile.language_id,
            'ace_theme': 'auto', 'site_theme': 'light', 'user_script': '',
            'rotate_password_on_login': True, 'account_session_epoch': 100,
            'password_rotated_at': '2026-10-20T00:00:00Z',
        })
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        profile = Profile.objects.get(user=self.user)
        self.assertFalse(profile.rotate_password_on_login)
        self.assertEqual(profile.account_session_epoch, 0)
        self.assertIsNone(profile.password_rotated_at)

    def test_profile_reversion_does_not_serialize_security_state(self):
        self.change()
        with revisions.create_revision():
            Profile.objects.get(user=self.user).save()
        fields = Version.objects.get_for_object(self.user.profile).first().field_dict
        for name in ('rotate_password_on_login', 'account_session_epoch', 'password_rotated_at'):
            self.assertNotIn(name, fields)

    def test_stale_profile_save_cannot_restore_policy_or_revoked_token(self):
        self.user.profile.generate_api_token()
        stale_profile = Profile.objects.get(user=self.user)
        self.change()
        stale_profile.about = 'Updated biography'
        stale_profile.save()
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.about, 'Updated biography')
        self.assertTrue(profile.rotate_password_on_login)
        self.assertEqual(profile.account_session_epoch, 1)
        self.assertIsNone(profile.api_token)

    def test_reverting_profile_preserves_current_policy_and_revocation(self):
        self.user.profile.generate_api_token()
        with revisions.create_revision():
            self.user.profile.about = 'Old biography'
            self.user.profile.save()
        version = Version.objects.get_for_object(self.user.profile).first()
        self.change()
        Profile.objects.filter(user=self.user).update(about='New biography')
        version.revert()
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.about, 'Old biography')
        self.assertTrue(profile.rotate_password_on_login)
        self.assertEqual(profile.account_session_epoch, 1)
        self.assertIsNone(profile.api_token)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17')
class SessionEpochTests(ManagedAccountData, TestCase):
    def setUp(self):
        self.actor = self.create_actor()
        self.user = self.create_user('contestant')
        self.factory = RequestFactory()
        self.store = import_module(settings.SESSION_ENGINE).SessionStore

    def login_session(self, legacy=False):
        request = self.factory.get('/')
        request.user = AnonymousUser()
        request.session = self.store()
        auth.login(request, User.objects.get(pk=self.user.pk), backend='django.contrib.auth.backends.ModelBackend')
        if legacy:
            del request.session[SESSION_EPOCH_KEY]
        request.session.save()
        return request.session

    def dispatch(self, session, token=None):
        request = self.factory.get('/problems/?page=2', HTTP_AUTHORIZATION='Bearer ' + token if token else '')
        request.LANGUAGE_CODE = 'en'
        request.session = self.store(session_key=session.session_key)
        handler = APIMiddleware(AuthenticationMiddleware(DMOJLoginMiddleware(lambda request: HttpResponse('OK'))))
        response = handler(request)
        return request, response

    def test_legacy_sessions_at_epoch_zero_stay_logged_in(self):
        request, response = self.dispatch(self.login_session(legacy=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.user.pk, self.user.pk)
        self.assertNotIn(SESSION_EPOCH_KEY, request.session)

    def test_successful_login_stamps_epoch_without_changing_password(self):
        password_hash = self.user.password
        Profile.objects.filter(user=self.user).update(account_session_epoch=4)
        session = self.login_session()
        self.assertEqual(session[SESSION_EPOCH_KEY], 4)
        self.assertEqual(self.dispatch(session)[1].status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, password_hash)

    def test_stamp_uses_persisted_epoch_not_cached_profile(self):
        self.assertEqual(self.user.profile.account_session_epoch, 0)
        Profile.objects.filter(user=self.user).update(account_session_epoch=5)
        request = self.factory.get('/')
        request.session = {}
        stamp_session_epoch(request, self.user)
        self.assertEqual(request.session[SESSION_EPOCH_KEY], 5)

    def test_toggle_on_then_off_does_not_revive_old_sessions(self):
        sessions = [self.login_session(legacy=True), self.login_session()]
        set_login_policy(self.actor, self.user.pk, True, 0, 'Exam')
        set_login_policy(self.actor, self.user.pk, False, 1, 'Exam finished')
        for session in sessions:
            with self.subTest(session_key=session.session_key):
                request, response = self.dispatch(session)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse('auth_login') + '?next='))
                self.assertFalse(request.user.is_authenticated)
                self.assertIsNone(request.profile)
                self.assertNotIn(auth.SESSION_KEY, request.session)
        current_session = self.login_session()
        self.assertEqual(current_session[SESSION_EPOCH_KEY], 2)
        self.assertEqual(self.dispatch(current_session)[1].status_code, 200)

    def test_invalid_session_stamps_are_rejected(self):
        profile = self.user.profile
        for value in (None, True, False, -1, '0', 0.0, [], {}, MAX_SESSION_EPOCH + 1):
            with self.subTest(value=value):
                self.assertFalse(session_epoch_matches({SESSION_EPOCH_KEY: value}, profile))
        self.assertTrue(session_epoch_matches({}, profile))
        self.assertTrue(session_epoch_matches({SESSION_EPOCH_KEY: 0}, profile))

    def test_ordinary_password_change_still_invalidates_session(self):
        session = self.login_session()
        self.user.set_password('Changed-password-2026')
        self.user.save(update_fields=['password'])
        request, _ = self.dispatch(session)
        self.assertFalse(request.user.is_authenticated)

    def test_old_api_token_is_revoked_but_new_token_after_disable_still_works(self):
        old_token = self.user.profile.generate_api_token()
        set_login_policy(self.actor, self.user.pk, True, 0, 'Exam')
        self.assertEqual(self.dispatch(self.store(), old_token)[1].status_code, 401)
        set_login_policy(self.actor, self.user.pk, False, 1, 'Exam finished')
        token = Profile.objects.get(user=self.user).generate_api_token()
        request, response = self.dispatch(self.store(), token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.user.pk, self.user.pk)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17')
class ConcurrentPolicyTests(ManagedAccountData, TransactionTestCase):
    def test_two_writes_with_same_epoch_have_one_winner(self):
        if not connection.features.has_select_for_update:
            self.skipTest('Requires a database with SELECT FOR UPDATE support.')
        actors = [self.create_actor(), self.create_actor('second_invigilator')]
        user = self.create_user('contestant')
        barrier = Barrier(2)
        results = []

        def change(actor):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                set_login_policy(actor, user.pk, True, 0, 'Exam')
                results.append('success')
            except LoginPolicyConflict:
                results.append('conflict')
            except Exception as exc:
                results.append(type(exc).__name__)
            finally:
                close_old_connections()

        threads = [Thread(target=change, args=(actor,)) for actor in actors]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ['success', 'conflict'])
        self.assertEqual(Profile.objects.get(user=user).account_session_epoch, 1)
        self.assertEqual(ManagedAccountEvent.objects.count(), 1)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17')
class ManagedLoginMigrationTests(TransactionTestCase):
    fixtures = ['language_small']

    def test_existing_users_keep_password_and_default_policy_across_migration(self):
        previous = [('judge', '0238_problem_data_revision')]
        current = [('judge', '0239_managed_login_policy')]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(previous)
            old_apps = executor.loader.project_state(previous).apps
            old_user = old_apps.get_model('auth', 'User').objects.create(
                username='legacy_contestant', password='unchanged-password-hash',
            )
            language = old_apps.get_model('judge', 'Language').objects.get(key='CPP17')
            old_apps.get_model('judge', 'Profile').objects.create(user_id=old_user.pk, language_id=language.pk)
            executor = MigrationExecutor(connection)
            executor.migrate(current)
            profile = Profile.objects.get(user_id=old_user.pk)
            self.assertFalse(profile.rotate_password_on_login)
            self.assertEqual(profile.account_session_epoch, 0)
            self.assertIsNone(profile.password_rotated_at)
            self.assertEqual(User.objects.get(pk=old_user.pk).password, 'unchanged-password-hash')
            executor = MigrationExecutor(connection)
            executor.migrate(previous)
            restored_apps = executor.loader.project_state(previous).apps
            self.assertEqual(restored_apps.get_model('auth', 'User').objects.get(pk=old_user.pk).password,
                             'unchanged-password-hash')
            self.assertEqual(restored_apps.get_model('judge', 'Profile').objects.get(user_id=old_user.pk).language_id,
                             language.pk)
        finally:
            MigrationExecutor(connection).migrate(current)
