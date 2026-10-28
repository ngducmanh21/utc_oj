import json
import time
from importlib import import_module
from threading import Barrier, Event, Thread
from unittest.mock import patch

import pyotp
from django.conf import settings
from django.contrib import auth
from django.contrib.auth.hashers import PBKDF2SHA1PasswordHasher
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from judge.forms import CustomAuthenticationForm, TwoFactorLoginForm, UserForm
from judge.models import ManagedAccountEvent, Profile, WebAuthnCredential
from judge.tests.test_managed_login import ManagedAccountData
from judge.utils.managed_login import COMPLETED_LOGIN_KEY, MAX_SESSION_EPOCH, ManagedLoginRejected, PENDING_LOGIN_KEY, \
    PENDING_LOGIN_MAX_AGE, SESSION_EPOCH_KEY, begin_password_login, complete_managed_login, \
    set_login_policy

PASSWORD = 'Initial-password-2026'
TOTP_KEY = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'
SCRATCH_CODE = 'ABCDEFGHIJKLMNOP'
BACKEND = 'django.contrib.auth.backends.ModelBackend'


class PasswordLoginData(ManagedAccountData):
    def enable_policy(self, user):
        actor = self.create_actor()
        set_login_policy(actor, user.pk, True, 0, 'Exam')
        return actor

    def enable_totp(self, user):
        Profile.objects.filter(user=user).update(is_totp_enabled=True, totp_key=TOTP_KEY,
                                                 scratch_codes=json.dumps([SCRATCH_CODE]))

    def factor_form(self, profile, code=None, **kwargs):
        return TwoFactorLoginForm(data={'totp_or_scratch_code': code or pyotp.TOTP(TOTP_KEY).now(), **kwargs},
                                  profile=profile, webauthn_challenge=None, webauthn_origin='https://testserver')

    def request(self, user=None, session=None):
        request = RequestFactory().post(reverse('auth_login'))
        request.user = user or AnonymousUser()
        request.session = session or import_module(settings.SESSION_ENGINE).SessionStore()
        return request

    def authenticated_user(self, user):
        user = User.objects.get(pk=user.pk)
        user.backend = BACKEND
        return user

    def rotations(self):
        return ManagedAccountEvent.objects.filter(event=ManagedAccountEvent.Event.PASSWORD_ROTATED)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', WEBAUTHN_RP_ID='testserver')
class PasswordLoginEndpointTests(PasswordLoginData, TestCase):
    def setUp(self):
        self.user = self.create_user('contestant')
        self.actor = self.enable_policy(self.user)
        self.password_hash = self.user.password
        validator = patch('judge.views.user.PwnedPasswordsValidator.validate')
        self.pwned = validator.start()
        self.addCleanup(validator.stop)

    def login(self, client=None, password=PASSWORD, **kwargs):
        return (client or self.client).post(reverse('auth_login'), {
            'username': self.user.username, 'password': password, 'next': reverse('user_edit_profile'), **kwargs,
        })

    def factor(self, client=None, code=None, **kwargs):
        return (client or self.client).post(reverse('login_2fa'), {
            'totp_or_scratch_code': code or pyotp.TOTP(TOTP_KEY).now(), **kwargs,
        })

    def assert_unchanged(self):
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, self.password_hash)
        self.assertEqual(Profile.objects.get(user=self.user).account_session_epoch, 1)
        self.assertFalse(self.rotations().exists())

    def assert_signed_out(self, client=None):
        self.assertNotIn(auth.SESSION_KEY, (client or self.client).session)

    def test_password_login_rotates_once_and_keeps_only_new_session(self):
        old = Client()
        old.force_login(self.user, backend=BACKEND)
        Profile.objects.filter(user=self.user).update(api_token='a' * 64)
        session = self.client.session
        session['anonymous_marker'] = True
        session.save()
        old_key = session.session_key
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('user_edit_profile'))
        self.assertNotEqual(self.client.session.session_key, old_key)
        self.user.refresh_from_db()
        new_hash = self.user.password
        self.assertNotEqual(new_hash, self.password_hash)
        self.assertFalse(self.user.check_password(PASSWORD))
        profile = self.user.profile
        self.assertEqual(profile.account_session_epoch, 2)
        self.assertIsNotNone(profile.password_rotated_at)
        self.assertIsNone(profile.api_token)
        self.assertEqual(self.client.session[auth.HASH_SESSION_KEY], self.user.get_session_auth_hash())
        self.assertEqual(self.client.session[COMPLETED_LOGIN_KEY], 2)
        self.assertEqual(self.client.session[SESSION_EPOCH_KEY], 2)
        self.assertNotIn(PENDING_LOGIN_KEY, self.client.session)
        for _ in range(2):
            self.assertEqual(self.client.get(reverse('user_edit_profile')).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, new_hash)
        old.get(reverse('user_edit_profile'))
        self.assert_signed_out(old)
        self.assertEqual(self.login(Client()).status_code, 200)
        event = self.rotations().get()
        self.assertIsNone(event.actor_id)
        self.assertEqual((event.previous_epoch, event.epoch), (1, 2))
        for payload in (str(self.client.session.items()), str(self.rotations().values().get()), str(response.content)):
            self.assertNotIn(PASSWORD, payload)
            self.assertNotIn(new_hash, payload)

    def test_off_keeps_existing_password_and_pwned_behavior(self):
        set_login_policy(self.actor, self.user.pk, False, 1, 'Finished')
        self.pwned.side_effect = ValidationError('Password is common')
        self.assertEqual(self.login().status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, self.password_hash)
        self.assertEqual(self.client.session[SESSION_EPOCH_KEY], 2)
        self.assertTrue(self.client.session['password_pwned'])
        self.assertFalse(self.rotations().exists())
        with override_settings(VNOJ_OFFICIAL_CONTEST_MODE=False):
            self.assertTrue(self.client.get(reverse('user_edit_profile')).url.startswith(reverse('password_change')))

    def test_on_clears_password_pwned_and_old_second_factor_flags(self):
        self.pwned.side_effect = ValidationError('Password is common')
        self.enable_totp(self.user)
        session = self.client.session
        session['2fa_passed'] = True
        session['webauthn_assert'] = 'stale-challenge'
        session.save()
        self.login()
        self.assertNotIn('password_pwned', self.client.session)
        self.assertNotIn('2fa_passed', self.client.session)
        self.assertNotIn('webauthn_assert', self.client.session)
        self.factor()
        self.assertEqual(self.client.get(reverse('user_edit_profile')).status_code, 200)

    def test_wrong_password_get_and_csrf_rejection_do_not_rotate(self):
        self.assertEqual(self.client.get(reverse('auth_login')).status_code, 200)
        self.assertEqual(self.login(password='wrong-password').status_code, 200)
        response = self.login(Client(enforce_csrf_checks=True))
        # The site's CSRF failure handler redirects back to the same form.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('auth_login'))
        self.assert_unchanged()
        self.assert_signed_out()

    def test_inactive_banned_or_protected_account_cannot_rotate(self):
        for update in ({'is_active': False}, {'is_staff': True}, {'is_superuser': True}):
            with self.subTest(update=update):
                User.objects.filter(pk=self.user.pk).update(**update)
                self.assertEqual(self.login().status_code, 200)
                self.assert_signed_out()
                self.assert_unchanged()
                User.objects.filter(pk=self.user.pk).update(is_active=True, is_staff=False, is_superuser=False)
        self.user.profile.ban_user('Banned in test')
        self.assertEqual(self.login().status_code, 200)
        self.assert_unchanged()

    def test_safe_redirect_after_password_login(self):
        response = self.login(next='https://attacker.example/')
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('attacker.example', response.url)

    def test_pending_totp_only_rotates_after_success_and_cannot_replay(self):
        self.enable_totp(self.user)
        response = self.login()
        self.assertTrue(response.url.startswith(reverse('login_2fa') + '?next='))
        pending_key = self.client.session.session_key
        proof = self.client.session[PENDING_LOGIN_KEY]
        self.assertEqual(proof['user_id'], self.user.pk)
        self.assertEqual(proof['epoch'], 1)
        for secret in (PASSWORD, TOTP_KEY, SCRATCH_CODE, self.password_hash):
            self.assertNotIn(secret, json.dumps(proof))
        self.assert_unchanged()
        response = self.client.get(reverse('user_edit_profile'))
        self.assertTrue(response.url.startswith(reverse('login_2fa')))
        self.assertEqual(self.client.get(reverse('login_2fa')).status_code, 200)
        self.assertEqual(self.factor(code='invalid').status_code, 200)
        self.assert_unchanged()
        response = self.factor()
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(self.client.session.session_key, pending_key)
        self.assertNotIn(PENDING_LOGIN_KEY, self.client.session)
        self.assertTrue(self.client.session['2fa_passed'])
        self.assertEqual(self.client.get(reverse('user_edit_profile')).status_code, 200)
        self.assertEqual(self.factor().status_code, 302)
        self.assertEqual(self.rotations().count(), 1)

    def test_pending_cannot_skip_factor_with_2fa_passed_flag(self):
        self.enable_totp(self.user)
        self.login()
        session = self.client.session
        session['2fa_passed'] = True
        session.save()
        self.assertEqual(self.client.get(reverse('login_2fa')).status_code, 200)
        for route in ('user_edit_profile', 'generate_api_token', 'disable_2fa', 'webauthn_attest'):
            response = self.client.post(reverse(route))
            self.assertTrue(response.url.startswith(reverse('login_2fa')))
        self.assert_unchanged()

    def test_pending_csrf_failure_does_not_consume_factor_or_password(self):
        self.enable_totp(self.user)
        self.login()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.cookies = self.client.cookies
        response = self.factor(client=csrf_client)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('login_2fa'))
        self.assert_unchanged()
        self.assertEqual(Profile.objects.get(user=self.user).last_totp_timecode, 0)

    def test_expired_or_malformed_pending_proof_fails_closed(self):
        self.enable_totp(self.user)
        invalid = [None, {}, {'issued_at': int(time.time()) - PENDING_LOGIN_MAX_AGE - 1},
                   {'issued_at': int(time.time()) + 60}, {'issued_at': True},
                   {'epoch': True}, {'user_id': True}, {'backend': 'wrong.backend'}, {'auth_hash': 'stale'}]
        for changes in invalid:
            with self.subTest(changes=changes):
                client = Client()
                self.login(client)
                session = client.session
                session[PENDING_LOGIN_KEY] = ({**session[PENDING_LOGIN_KEY], **changes} if changes else changes)
                session.save()
                response = client.get(reverse('login_2fa'))
                self.assertTrue(response.url.startswith(reverse('auth_login')))
                self.assert_signed_out(client)
                self.assert_unchanged()

    def test_factor_changes_invalidate_pending_even_on_get(self):
        self.enable_totp(self.user)
        for changes in ({'is_totp_enabled': False}, {'totp_key': pyotp.random_base32()},
                        {'scratch_codes': json.dumps([])}, {'is_webauthn_enabled': True}):
            with self.subTest(changes=changes):
                self.enable_totp(self.user)
                Profile.objects.filter(user=self.user).update(is_webauthn_enabled=False)
                client = Client()
                self.login(client)
                Profile.objects.filter(user=self.user).update(**changes)
                response = client.get(reverse('login_2fa'))
                self.assertTrue(response.url.startswith(reverse('auth_login')))
                self.assert_signed_out(client)
                self.assert_unchanged()

    def test_policy_toggle_invalidates_pending(self):
        self.enable_totp(self.user)
        self.login()
        set_login_policy(self.actor, self.user.pk, False, 1, 'Finished')
        response = self.factor()
        self.assertTrue(response.url.startswith(reverse('auth_login')))
        self.assert_signed_out()
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, self.password_hash)
        self.assertFalse(self.rotations().exists())

    def test_two_browsers_pending_only_first_factor_can_complete(self):
        self.enable_totp(self.user)
        other = Client()
        self.login()
        self.login(other)
        self.assertEqual(self.factor().status_code, 302)
        response = self.factor(other)
        self.assertTrue(response.url.startswith(reverse('auth_login')))
        self.assert_signed_out(other)
        self.assertEqual(self.client.get(reverse('user_edit_profile')).status_code, 200)
        self.assertEqual(self.rotations().count(), 1)

    def test_pending_without_proof_cannot_complete_with_boolean_flag(self):
        self.enable_totp(self.user)
        self.login()
        session = self.client.session
        session.pop(PENDING_LOGIN_KEY)
        session['2fa_passed'] = True
        session.save()
        response = self.client.get(reverse('user_edit_profile'))
        self.assertTrue(response.url.startswith(reverse('auth_login')))
        self.assert_signed_out()
        self.assert_unchanged()

    def test_factor_redirect_rejects_external_next_url(self):
        self.enable_totp(self.user)
        self.login()
        response = self.client.post(reverse('login_2fa') + '?next=https://attacker.example/', {
            'totp_or_scratch_code': pyotp.TOTP(TOTP_KEY).now(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('attacker.example', response.url)

    def test_changed_webauthn_credential_invalidates_pending(self):
        profile = self.user.profile
        Profile.objects.filter(pk=profile.pk).update(is_webauthn_enabled=True)
        credential = WebAuthnCredential.objects.create(user=profile, name='Test key', cred_id='a2V5',
                                                       public_key='test-public-key', counter=0)
        self.login()
        WebAuthnCredential.objects.filter(pk=credential.pk).update(public_key='replacement-key')
        response = self.client.get(reverse('webauthn_assert'))
        self.assertTrue(response.url.startswith(reverse('auth_login')))
        self.assert_signed_out()
        self.assert_unchanged()

    def test_admin_password_change_invalidates_pending_and_allows_new_password(self):
        self.enable_totp(self.user)
        self.login()
        self.user.set_password('Reissued-password-2026')
        self.user.save(update_fields=['password'])
        response = self.factor()
        self.assertTrue(response.url.startswith(reverse('auth_login')))
        self.assert_signed_out()
        self.assertFalse(self.rotations().exists())
        self.assertEqual(self.login(password='Reissued-password-2026').status_code, 302)
        self.assertEqual(self.factor().status_code, 302)
        self.assertEqual(self.rotations().count(), 1)

    def test_pending_logout_does_not_consume_password(self):
        self.enable_totp(self.user)
        self.login()
        self.client.post(reverse('auth_logout'))
        self.assert_signed_out()
        self.assert_unchanged()

    def test_scratch_code_is_consumed_together_with_password(self):
        self.enable_totp(self.user)
        self.login()
        self.assertEqual(self.factor(code=SCRATCH_CODE).status_code, 302)
        self.assertEqual(json.loads(Profile.objects.get(user=self.user).scratch_codes), [])
        self.assertEqual(self.rotations().count(), 1)

    def test_audit_failure_after_login_rolls_back_password_and_discards_session(self):
        with patch.object(ManagedAccountEvent.objects, 'create', side_effect=DatabaseError('Test failure')):
            self.assertEqual(self.login().status_code, 503)
        self.assert_unchanged()
        self.assert_signed_out()

    def test_audit_failure_after_factor_rolls_back_scratch_consumption(self):
        self.enable_totp(self.user)
        self.login()
        with patch.object(ManagedAccountEvent.objects, 'create', side_effect=DatabaseError('Test failure')):
            self.assertEqual(self.factor(code=SCRATCH_CODE).status_code, 503)
        self.assert_unchanged()
        self.assert_signed_out()
        self.assertEqual(json.loads(Profile.objects.get(user=self.user).scratch_codes), [SCRATCH_CODE])

    def test_audit_failure_after_totp_rolls_back_replay_counter(self):
        self.enable_totp(self.user)
        self.login()
        with patch.object(ManagedAccountEvent.objects, 'create', side_effect=DatabaseError('Test failure')):
            self.assertEqual(self.factor().status_code, 503)
        self.assert_unchanged()
        self.assert_signed_out()
        self.assertEqual(Profile.objects.get(user=self.user).last_totp_timecode, 0)

    def test_off_totp_login_retains_existing_behavior(self):
        set_login_policy(self.actor, self.user.pk, False, 1, 'Finished')
        self.enable_totp(self.user)
        self.login()
        self.assertNotIn(PENDING_LOGIN_KEY, self.client.session)
        self.assertTrue(self.client.get(reverse('user_edit_profile')).url.startswith(reverse('login_2fa')))
        self.assertEqual(self.factor(code='invalid').status_code, 200)
        self.assertEqual(self.factor().status_code, 302)
        self.assertEqual(self.client.get(reverse('user_edit_profile')).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, self.password_hash)
        self.assertFalse(self.rotations().exists())

    def test_off_password_login_is_repeatable_in_different_browsers(self):
        set_login_policy(self.actor, self.user.pk, False, 1, 'Finished')
        other = Client()
        for client in (self.client, other):
            self.assertEqual(self.login(client).status_code, 302)
            self.assertEqual(client.get(reverse('user_edit_profile')).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, self.password_hash)
        self.assertFalse(self.rotations().exists())

    def test_webauthn_challenge_allowed_and_completion_consumes_password(self):
        profile = self.user.profile
        Profile.objects.filter(pk=profile.pk).update(is_webauthn_enabled=True)
        credential = WebAuthnCredential.objects.create(user=profile, name='Test key', cred_id='a2V5',
                                                       public_key='test-public-key', counter=0)
        self.login()
        response = self.client.get(reverse('webauthn_assert'))
        self.assertEqual(response.status_code, 200)
        self.assert_unchanged()
        response_data = json.dumps({'id': credential.cred_id, 'response': {'signature': 'test'}})
        with patch('judge.forms.webauthn.WebAuthnAssertionResponse') as assertion:
            assertion.return_value.verify.return_value = 1
            response = self.factor(webauthn_response=response_data)
            self.assertEqual(response.status_code, 302)
            assertion.return_value.verify.assert_called_once()
        credential.refresh_from_db()
        self.assertEqual(credential.counter, 1)
        self.assertNotIn('webauthn_assert', self.client.session)
        self.assertEqual(self.rotations().count(), 1)

    def test_invalid_webauthn_response_keeps_pending_password(self):
        Profile.objects.filter(user=self.user).update(is_webauthn_enabled=True)
        WebAuthnCredential.objects.create(user=self.user.profile, name='Test key', cred_id='a2V5',
                                          public_key='test-public-key', counter=0)
        self.login()
        self.client.get(reverse('webauthn_assert'))
        for response_data in ('{', '[]', 'null', '{"response":null}', '{"id":"unknown","response":{}}'):
            with self.subTest(response_data=response_data):
                self.assertEqual(self.factor(webauthn_response=response_data).status_code, 200)
                self.assert_unchanged()


@override_settings(DEFAULT_USER_LANGUAGE='CPP17')
class PasswordLoginServiceTests(PasswordLoginData, TestCase):
    def setUp(self):
        self.user = self.create_user('contestant')
        self.actor = self.enable_policy(self.user)

    def test_stale_authenticated_user_is_rechecked_under_lock(self):
        stale = self.authenticated_user(self.user)
        self.user.set_password('Reissued-password-2026')
        self.user.save(update_fields=['password'])
        request = self.request()
        with self.assertRaises(ManagedLoginRejected):
            begin_password_login(request, stale, PASSWORD)
        self.assertNotIn(auth.SESSION_KEY, request.session)
        self.assertFalse(self.rotations().exists())

    def test_service_rechecks_disabled_or_banned_account_after_authentication(self):
        stale = self.authenticated_user(self.user)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(ManagedLoginRejected):
            begin_password_login(self.request(), stale, PASSWORD)
        User.objects.filter(pk=self.user.pk).update(is_active=True)
        self.user.profile.ban_user('Banned in test')
        with self.assertRaises(ManagedLoginRejected):
            begin_password_login(self.request(), stale, PASSWORD)
        self.assertFalse(self.rotations().exists())

    def test_session_persistence_failure_rolls_back_rotation(self):
        request = self.request()
        store = type(request.session)
        original_save = store.save

        def fail_final_save(session, *args, **kwargs):
            if COMPLETED_LOGIN_KEY in session:
                raise DatabaseError('Test session failure')
            return original_save(session, *args, **kwargs)

        with patch.object(store, 'save', fail_final_save), self.assertRaises(DatabaseError):
            begin_password_login(request, self.authenticated_user(self.user), PASSWORD)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        self.assertFalse(request.user.is_authenticated)
        self.assertNotIn(auth.SESSION_KEY, request.session)
        self.assertFalse(self.rotations().exists())

    def test_epoch_exhaustion_does_not_consume_password(self):
        Profile.objects.filter(user=self.user).update(account_session_epoch=MAX_SESSION_EPOCH)
        with self.assertRaises(ManagedLoginRejected):
            begin_password_login(self.request(), self.authenticated_user(self.user), PASSWORD)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_pending_rechecks_factor_configuration_under_lock(self):
        self.enable_totp(self.user)
        request = self.request()
        begin_password_login(request, self.authenticated_user(self.user), PASSWORD)
        form = self.factor_form(request.profile)
        Profile.objects.filter(user=self.user).update(totp_key=pyotp.random_base32())
        with self.assertRaises(ManagedLoginRejected):
            complete_managed_login(request, request.session[PENDING_LOGIN_KEY], form)
        self.assertFalse(self.rotations().exists())
        self.assertNotIn(auth.SESSION_KEY, request.session)

    def test_pending_cannot_reuse_a_prevalidated_form(self):
        self.enable_totp(self.user)
        request = self.request()
        begin_password_login(request, self.authenticated_user(self.user), PASSWORD)
        form = self.factor_form(request.profile, code='invalid')
        form._errors = {}
        form.cleaned_data = {'totp_or_scratch_code': pyotp.TOTP(TOTP_KEY).now()}
        self.assertFalse(complete_managed_login(request, request.session[PENDING_LOGIN_KEY], form))
        self.assertTrue(form.errors)
        self.assertFalse(self.rotations().exists())

    def test_policy_is_read_again_after_form_authentication(self):
        stale = self.authenticated_user(self.user)
        set_login_policy(self.actor, self.user.pk, False, 1, 'Finished')
        request = self.request()
        self.assertFalse(begin_password_login(request, stale, PASSWORD))
        self.assertEqual(request.session[SESSION_EPOCH_KEY], 2)
        self.assertNotIn(COMPLETED_LOGIN_KEY, request.session)
        self.assertFalse(self.rotations().exists())

    def test_no_rotation_on_get_or_impersonation(self):
        request = self.request()
        request.method = 'GET'
        with self.assertRaises(ManagedLoginRejected):
            begin_password_login(request, self.authenticated_user(self.user), PASSWORD)
        request = self.request(self.actor)
        request.user.is_impersonate = True
        with self.assertRaises(ManagedLoginRejected):
            begin_password_login(request, self.authenticated_user(self.user), PASSWORD)
        self.assertFalse(self.rotations().exists())

    def test_stale_user_form_does_not_restore_consumed_password(self):
        form = UserForm(data={'first_name': 'Contestant'}, instance=User.objects.get(pk=self.user.pk))
        self.assertTrue(form.is_valid(), form.errors)
        begin_password_login(self.request(), self.authenticated_user(self.user), PASSWORD)
        password_hash = User.objects.get(pk=self.user.pk).password
        form.save()
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, password_hash)
        self.assertFalse(self.user.check_password(PASSWORD))
        if 'first_name' in form.fields:
            self.assertEqual(self.user.first_name, 'Contestant')


@override_settings(DEFAULT_USER_LANGUAGE='CPP17')
class ConcurrentPasswordLoginTests(PasswordLoginData, TransactionTestCase):
    def setUp(self):
        if not connection.features.has_select_for_update:
            self.skipTest('Requires a database with SELECT FOR UPDATE support.')
        self.user = self.create_user('contestant')
        self.actor = self.enable_policy(self.user)

    def race(self, actions):
        barrier = Barrier(len(actions))
        results = []

        def run(action):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                action()
                results.append('success')
            except ManagedLoginRejected:
                results.append('rejected')
            except Exception as error:
                results.append(type(error).__name__)
            finally:
                close_old_connections()

        threads = [Thread(target=run, args=(action,)) for action in actions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        return results

    def test_two_password_logins_have_one_winner(self):
        stale_users = [self.authenticated_user(self.user) for _ in range(2)]
        requests = [self.request() for _ in range(2)]
        actions = [lambda i=i: begin_password_login(requests[i], stale_users[i], PASSWORD) for i in range(2)]
        self.assertCountEqual(self.race(actions), ['success', 'rejected'])
        self.assertEqual(self.rotations().count(), 1)
        self.assertEqual(Profile.objects.get(user=self.user).account_session_epoch, 2)
        self.assertEqual(sum(auth.SESSION_KEY in request.session for request in requests), 1)

    def test_two_pending_second_factors_have_one_winner(self):
        self.enable_totp(self.user)
        requests = [self.request() for _ in range(2)]
        for request in requests:
            begin_password_login(request, self.authenticated_user(self.user), PASSWORD)
        actions = [
            lambda request=request: complete_managed_login(
                request, request.session[PENDING_LOGIN_KEY], self.factor_form(request.profile),
            ) for request in requests
        ]
        self.assertCountEqual(self.race(actions), ['success', 'rejected'])
        self.assertEqual(self.rotations().count(), 1)
        self.assertEqual(sum(auth.SESSION_KEY in request.session for request in requests), 1)

    def test_commit_failure_rolls_back_and_discards_authenticated_session(self):
        request = self.request()
        with patch.object(connection, '_commit', side_effect=DatabaseError('Commit failed')):
            with self.assertRaises(DatabaseError):
                begin_password_login(request, self.authenticated_user(self.user), PASSWORD)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        self.assertEqual(Profile.objects.get(user=self.user).account_session_epoch, 1)
        self.assertFalse(self.rotations().exists())
        self.assertFalse(request.user.is_authenticated)
        self.assertNotIn(auth.SESSION_KEY, request.session)

    @override_settings(PASSWORD_HASHERS=[
        'django.contrib.auth.hashers.MD5PasswordHasher',
        'django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher',
    ])
    def test_legacy_hash_upgrade_cannot_restore_password_after_concurrent_rotation(self):
        old_hash = PBKDF2SHA1PasswordHasher().encode(PASSWORD, 'test-salt', iterations=1)
        User.objects.filter(pk=self.user.pk).update(password=old_hash)
        authenticated = self.authenticated_user(self.user)
        checking = Event()
        release_check = Event()
        rotation_done = Event()
        results = []
        original_check = User.check_password

        def delayed_check(user, raw_password):
            checking.set()
            if not release_check.wait(timeout=10):
                raise TimeoutError()
            return original_check(user, raw_password)

        def validate_form():
            close_old_connections()
            try:
                form = CustomAuthenticationForm(request=self.request(),
                                                data={'username': self.user.username, 'password': PASSWORD})
                results.append(('form', form.is_valid()))
            finally:
                close_old_connections()

        def rotate():
            close_old_connections()
            try:
                begin_password_login(self.request(), authenticated, PASSWORD)
                results.append(('rotation', True))
            finally:
                rotation_done.set()
                close_old_connections()

        with patch.object(User, 'check_password', delayed_check):
            validator = Thread(target=validate_form)
            rotator = Thread(target=rotate)
            validator.start()
            try:
                self.assertTrue(checking.wait(timeout=10))
                rotator.start()
                self.assertFalse(rotation_done.wait(timeout=0.5))
            finally:
                release_check.set()
                validator.join(timeout=20)
                if rotator.ident is not None:
                    rotator.join(timeout=20)
            self.assertFalse(validator.is_alive())
            self.assertFalse(rotator.is_alive())
        self.assertCountEqual(results, [('form', True), ('rotation', True)])
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password(PASSWORD))
        self.assertEqual(self.rotations().count(), 1)
