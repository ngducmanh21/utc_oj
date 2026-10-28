"""M4: alternate authentication and self-service credential policy."""

import json
from threading import Event, Thread
from unittest.mock import patch

import pyotp
from django.conf import settings
from django.contrib import auth
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection, transaction
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from social_core.backends.base import BaseAuth
from social_core.pipeline.utils import partial_store
from social_core.utils import PARTIAL_TOKEN_SESSION_NAME
from social_django.models import UserSocialAuth
from social_django.utils import load_strategy

from judge.forms import ProfileForm
from judge.ip_auth import IPBasedAuthBackend
from judge.middleware import APIMiddleware
from judge.models import ManagedAccountEvent, Profile, WebAuthnCredential
from judge.tests.test_managed_password_login import BACKEND, PASSWORD, PasswordLoginData, SCRATCH_CODE, TOTP_KEY
from judge.utils.managed_login import COMPLETED_LOGIN_KEY, ManagedLoginRejected, PENDING_LOGIN_KEY, \
    begin_password_login, set_login_policy
from judge.views.user import generate_api_token


class PolicyOAuthBackend(BaseAuth):
    """Run the real pipeline/callback without contacting an OAuth provider."""
    name = 'policy-oauth'
    ID_KEY = 'id'

    def get_user_details(self, response):
        return {'email': response['email'], 'username': 'oauth_contestant'}

    def auth_url(self):
        return 'https://provider.example/authorize'

    def auth_complete(self, *args, **kwargs):
        data = self.strategy.request_data()
        return self.strategy.authenticate(self, response={'id': data['uid'], 'email': data['email']},
                                          *args, **kwargs)


OAUTH_BACKEND = 'judge.tests.test_managed_login_policy.PolicyOAuthBackend'


class PolicyData(PasswordLoginData):
    def setUp(self):
        cache.clear()
        self.user = self.create_user('contestant', email='contestant@example.com')
        self.actor = self.create_actor()
        validator = patch('judge.views.user.PwnedPasswordsValidator.validate')
        validator.start()
        self.addCleanup(validator.stop)

    def enable(self):
        profile = Profile.objects.get(user=self.user)
        return set_login_policy(self.actor, self.user.pk, True, profile.account_session_epoch)

    def disable(self):
        profile = Profile.objects.get(user=self.user)
        return set_login_policy(self.actor, self.user.pk, False, profile.account_session_epoch)

    def password_login(self, client=None):
        return (client or self.client).post(reverse('auth_login'), {
            'username': self.user.username, 'password': PASSWORD,
        })

    def managed_session(self, factors=False):
        if factors:
            self.enable_totp(self.user)
        self.enable()
        self.assertEqual(self.password_login().status_code, 302)
        if factors:
            response = self.client.post(reverse('login_2fa'), {'totp_or_scratch_code': pyotp.TOTP(TOTP_KEY).now()})
            self.assertEqual(response.status_code, 302)
        self.assertIn(COMPLETED_LOGIN_KEY, self.client.session)

    def reset_url(self, token=None):
        user = User.objects.get(pk=self.user.pk)
        return reverse('password_reset_confirm', kwargs={
            'uidb64': urlsafe_base64_encode(force_bytes(user.pk)),
            'token': token or default_token_generator.make_token(user),
        })

    def assert_no_credential_changes(self, before):
        user = User.objects.get(pk=self.user.pk)
        profile = Profile.objects.get(user=user)
        self.assertEqual(user.password, before[0])
        self.assertEqual((profile.totp_key, profile.scratch_codes, profile.is_totp_enabled,
                          profile.is_webauthn_enabled, profile.api_token), before[1:])

    def credentials(self):
        user = User.objects.get(pk=self.user.pk)
        profile = Profile.objects.get(user=user)
        return (user.password, profile.totp_key, profile.scratch_codes, profile.is_totp_enabled,
                profile.is_webauthn_enabled, profile.api_token)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', WEBAUTHN_RP_ID='testserver',
                   AUTH_PASSWORD_VALIDATORS=[], DMOJ_PASSWORD_RESET_LIMIT_COUNT=100)
class CredentialEndpointTests(PolicyData, TestCase):
    def test_managed_user_cannot_change_password_or_generate_tokens(self):
        self.managed_session()
        before = self.credentials()
        self.assertEqual(self.client.get(reverse('password_change')).status_code, 403)
        response = self.client.post(reverse('password_change'), {
            'old_password': PASSWORD, 'new_password1': 'Replacement-2026', 'new_password2': 'Replacement-2026',
        })
        self.assertEqual(response.status_code, 403)
        for name in ('generate_api_token', 'remove_api_token', 'generate_scratch_codes'):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name)).status_code, 403)
        self.assert_no_credential_changes(before)

    def test_managed_factor_management_get_and_post_are_denied(self):
        self.managed_session(factors=True)
        credential = WebAuthnCredential.objects.create(user=self.user.profile, name='Existing key',
                                                       cred_id='credential-id', public_key='public-key', counter=0)
        before = self.credentials()
        for name in ('enable_2fa', 'refresh_2fa', 'disable_2fa', 'webauthn_attest', 'webauthn_delete'):
            url = reverse(name, args=[credential.pk] if name == 'webauthn_delete' else [])
            with self.subTest(name=name):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, {'totp_token': pyotp.TOTP(TOTP_KEY).now()}).status_code, 403)
        self.assert_no_credential_changes(before)
        self.assertTrue(WebAuthnCredential.objects.filter(pk=credential.pk).exists())
        self.assertNotIn('totp_enable_key', self.client.session)
        self.assertNotIn('webauthn_attest', self.client.session)

    def test_existing_factor_still_finishes_managed_login(self):
        self.enable_totp(self.user)
        WebAuthnCredential.objects.create(user=self.user.profile, name='Existing key',
                                          cred_id='a2V5', public_key='cHVibGljLWtleQ', counter=0)
        Profile.objects.filter(user=self.user).update(is_webauthn_enabled=True)
        self.enable()
        self.password_login()
        self.assertIn(PENDING_LOGIN_KEY, self.client.session)
        self.assertEqual(self.client.get(reverse('webauthn_assert')).status_code, 200)
        before = self.credentials()
        self.assertEqual(self.client.get(reverse('enable_2fa')).status_code, 302)
        self.assert_no_credential_changes(before)
        response = self.client.post(reverse('login_2fa'), {'totp_or_scratch_code': SCRATCH_CODE})
        self.assertEqual(response.status_code, 302)
        self.assertIn(COMPLETED_LOGIN_KEY, self.client.session)
        self.assertEqual(self.rotations().count(), 1)

    def test_profile_hides_credential_controls_for_managed_user(self):
        self.managed_session(factors=True)
        response = self.client.get(reverse('user_edit_profile'))
        self.assertContains(response, 'Sign-in settings are managed by the invigilator.')
        for name in ('password_change', 'enable_2fa', 'disable_2fa', 'refresh_2fa', 'webauthn_attest',
                     'generate_api_token', 'generate_scratch_codes'):
            with self.subTest(name=name):
                self.assertNotContains(response, reverse(name))
        self.assertContains(response, 'name="first_name"')

    def test_off_password_change_preserves_current_session_and_rejects_old_password(self):
        self.client.force_login(self.user, backend=BACKEND)
        self.assertEqual(self.client.get(reverse('password_change')).status_code, 200)
        response = self.client.post(reverse('password_change'), {
            'old_password': PASSWORD, 'new_password1': 'Replacement-2026', 'new_password2': 'Replacement-2026',
        })
        self.assertRedirects(response, reverse('password_change_done'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Replacement-2026'))
        self.assertEqual(int(self.client.session[auth.SESSION_KEY]), self.user.pk)
        self.assertEqual(ManagedAccountEvent.objects.count(), 0)

    def test_off_token_generation_and_revocation_work(self):
        self.client.force_login(self.user, backend=BACKEND)
        response = self.client.post(reverse('generate_api_token'))
        token = response.json()['data']['token']
        response = self.client.get(reverse('user_edit_profile'), HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post(reverse('remove_api_token')).status_code, 200)
        response = self.client.get(reverse('user_edit_profile'), HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(response.status_code, 401)

    def test_off_totp_setup_refresh_disable_and_scratch_generation_work(self):
        self.client.force_login(self.user, backend=BACKEND)
        self.assertEqual(self.client.get(reverse('enable_2fa')).status_code, 200)
        key = self.client.session['totp_enable_key']
        response = self.client.post(reverse('enable_2fa'), {'totp_or_scratch_code': pyotp.TOTP(key).now()})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Profile.objects.get(user=self.user).is_totp_enabled)
        self.assertEqual(self.client.get(reverse('refresh_2fa')).status_code, 200)
        response = self.client.post(reverse('generate_scratch_codes'))
        code = response.json()['data']['codes'][0]
        response = self.client.post(reverse('disable_2fa'), {'totp_or_scratch_code': code})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Profile.objects.get(user=self.user).is_totp_enabled)

    def test_off_webauthn_registration_and_deletion_work(self):
        self.client.force_login(self.user, backend=BACKEND)
        response = self.client.get(reverse('webauthn_attest'))
        self.assertEqual(response.status_code, 200)
        with patch('judge.views.two_factor.webauthn.WebAuthnRegistrationResponse') as verifier:
            verified = verifier.return_value.verify.return_value
            verified.credential_id = b'credential-id'
            verified.public_key = b'public-key'
            verified.sign_count = 0
            response = self.client.post(reverse('webauthn_attest'), {
                'credential': json.dumps({'response': {}}), 'name': 'New key',
            })
        self.assertEqual(response.status_code, 200)
        credential = WebAuthnCredential.objects.get(user=self.user.profile)
        # The existing OFF flow asks for the new factor before further actions.
        session = self.client.session
        session['2fa_passed'] = True
        session.save()
        self.assertEqual(self.client.post(reverse('webauthn_delete', args=[credential.pk])).status_code, 200)
        self.assertFalse(WebAuthnCredential.objects.filter(pk=credential.pk).exists())

    def test_profile_form_cannot_overwrite_credentials_or_email_from_stale_instance(self):
        stale = self.user.profile
        form = ProfileForm(user=self.user, instance=stale, data={
            'about': '', 'timezone': stale.timezone, 'language': stale.language_id,
            'ace_theme': 'auto', 'site_theme': 'light', 'user_script': '',
            'email': 'outsider@example.com', 'totp_key': 'forged', 'ip_auth': '127.0.0.1',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.enable_totp(self.user)
        self.enable()
        form.save()
        self.assertEqual(Profile.objects.get(user=self.user).totp_key, TOTP_KEY)
        self.assertTrue(Profile.objects.get(user=self.user).is_totp_enabled)
        self.assertTrue(Profile.objects.get(user=self.user).rotate_password_on_login)
        self.assertIsNone(Profile.objects.get(user=self.user).ip_auth)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'contestant@example.com')

    def test_guard_rechecks_password_hash_after_authentication(self):
        self.client.force_login(self.user, backend=BACKEND)
        request = RequestFactory().post(reverse('generate_api_token'))
        request.user = self.user
        request.session = self.client.session
        request.profile = self.user.profile
        fresh = User.objects.get(pk=self.user.pk)
        fresh.set_password('Admin-reset-2026')
        fresh.save(update_fields=['password'])
        with self.assertRaises(PermissionDenied):
            generate_api_token(request)
        self.assertIsNone(Profile.objects.get(user=self.user).api_token)

    def test_credential_posts_still_require_csrf_for_off_user(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user, backend=BACKEND)
        before = self.credentials()
        for name in ('generate_api_token', 'generate_scratch_codes', 'password_change'):
            response = client.post(reverse(name))
            self.assertEqual(response.status_code, 302)  # Existing CSRF failure handler redirects.
            self.assertEqual(response.url, reverse(name))
        self.assert_no_credential_changes(before)

    def test_managed_profile_edit_accepts_normal_fields_and_ignores_forged_credentials(self):
        self.managed_session()
        profile = Profile.objects.get(user=self.user)
        before = self.credentials()
        response = self.client.post(reverse('user_edit_profile'), {
            'first_name': 'Updated contestant', 'about': '', 'timezone': profile.timezone,
            'language': profile.language_id, 'ace_theme': 'auto', 'site_theme': 'light', 'user_script': '',
            'email': 'outsider@example.com', 'password': 'Known-password', 'rotate_password_on_login': '',
            'totp_key': TOTP_KEY, 'is_totp_enabled': 'on', 'ip_auth': '192.0.2.5',
        })
        self.assertEqual(response.status_code, 302)
        self.assert_no_credential_changes(before)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Updated contestant')
        self.assertEqual(self.user.email, 'contestant@example.com')
        self.assertTrue(self.user.profile.rotate_password_on_login)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', AUTH_PASSWORD_VALIDATORS=[],
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', DMOJ_PASSWORD_RESET_LIMIT_COUNT=100)
class PasswordResetPolicyTests(PolicyData, TestCase):
    def test_managed_reset_request_is_neutral_and_sends_nothing(self):
        self.enable()
        before = self.credentials()
        for email in (self.user.email, 'missing@example.com'):
            response = self.client.post(reverse('password_reset'), {'email': email})
            self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 0)
        self.assert_no_credential_changes(before)

    def test_shared_email_only_sends_to_unmanaged_account(self):
        self.enable()
        other = self.create_user('other_contestant', email=self.user.email)
        self.client.post(reverse('password_reset'), {'email': self.user.email})
        self.assertEqual(len(mail.outbox), 1)
        other_uid = urlsafe_base64_encode(force_bytes(other.pk))
        self.assertIn('/password/reset/confirm/' + other_uid + '-', mail.outbox[0].body)

    def test_old_reset_link_rejected_while_policy_is_on(self):
        url = self.reset_url()
        self.enable()
        before = self.credentials()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context_data['validlink'])
        response = self.client.post(url, {'new_password1': 'Reset-2026', 'new_password2': 'Reset-2026'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context_data['validlink'])
        self.assert_no_credential_changes(before)

    def test_reset_form_opened_before_toggle_is_rejected_on_post(self):
        response = self.client.get(self.reset_url())
        self.assertEqual(response.status_code, 302)
        url = response.url
        self.assertTrue(self.client.get(url).context_data['validlink'])
        self.enable()
        before = self.credentials()
        response = self.client.post(url, {'new_password1': 'Reset-2026', 'new_password2': 'Reset-2026'})
        self.assertFalse(response.context_data['validlink'])
        self.assert_no_credential_changes(before)

    def test_off_reset_request_and_confirmation_still_work(self):
        response = self.client.post(reverse('password_reset'), {'email': self.user.email})
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        response = self.client.get(self.reset_url())
        response = self.client.post(response.url, {'new_password1': 'Reset-2026', 'new_password2': 'Reset-2026'})
        self.assertRedirects(response, reverse('password_reset_complete'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Reset-2026'))
        self.assertNotIn(auth.SESSION_KEY, self.client.session)

    def test_turning_policy_off_reenables_email_reset(self):
        self.enable()
        self.disable()
        response = self.client.post(reverse('password_reset'), {'email': self.user.email})
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', AUTH_PASSWORD_VALIDATORS=[])
class AlternateAuthenticationTests(PolicyData, TestCase):
    def test_retained_token_is_denied_without_marking_authentication_or_csrf(self):
        token = self.user.profile.generate_api_token()
        digest = self.user.profile.api_token
        self.enable()
        Profile.objects.filter(user=self.user).update(api_token=digest)
        request = RequestFactory().post('/api/v2/user', HTTP_AUTHORIZATION='Bearer ' + token)
        request.user = AnonymousUser()
        request.session = {}
        response = APIMiddleware(lambda request: HttpResponse('Unexpected success'))(request)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(request.user.is_authenticated)
        self.assertFalse(getattr(request, '_api_authenticated', False))
        self.assertFalse(getattr(request, 'csrf_processing_done', False))
        self.assertNotIn('2fa_passed', request.session)

    def test_disabled_user_token_is_also_denied(self):
        token = self.user.profile.generate_api_token()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        response = self.client.get(reverse('user_edit_profile'), HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(response.status_code, 401)

    def test_token_revoked_after_middleware_cannot_generate_a_new_credential(self):
        token = self.user.profile.generate_api_token()
        request = RequestFactory().post(reverse('generate_api_token'), HTTP_AUTHORIZATION='Bearer ' + token)
        request.session = {}

        def revoke_then_generate(request):
            self.enable()
            self.disable()
            with self.assertRaises(PermissionDenied):
                generate_api_token(request)
            return HttpResponse('Rejected')

        APIMiddleware(revoke_then_generate)(request)
        self.assertIsNone(Profile.objects.get(user=self.user).api_token)

    def test_ip_login_is_denied_when_on_and_restored_when_off(self):
        Profile.objects.filter(user=self.user).update(ip_auth='192.0.2.5')
        backend = IPBasedAuthBackend()
        self.assertEqual(backend.authenticate(None, '192.0.2.5').pk, self.user.pk)
        self.enable()
        self.assertIsNone(backend.authenticate(None, '192.0.2.5'))
        self.assertIsNone(auth.authenticate(ip_auth='192.0.2.5'))
        self.disable()
        self.assertEqual(auth.authenticate(ip_auth='192.0.2.5').pk, self.user.pk)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', AUTH_PASSWORD_VALIDATORS=[],
                   AUTHENTICATION_BACKENDS=(OAUTH_BACKEND, BACKEND), DMOJ_REGISTRATION_EMAIL_DOMAINS=['example.com'])
class OAuthPolicyTests(PolicyData, TestCase):
    def callback(self, uid='linked-id', client=None):
        return (client or self.client).get(reverse('social:complete', args=[PolicyOAuthBackend.name]), {
            'uid': uid, 'email': self.user.email,
        })

    def link(self):
        return UserSocialAuth.objects.create(user=self.user, provider=PolicyOAuthBackend.name, uid='linked-id')

    def store_partial(self, step, **kwargs):
        request = RequestFactory().get(reverse('social:complete', args=[PolicyOAuthBackend.name]))
        request.user = AnonymousUser()
        request.session = self.client.session
        strategy = load_strategy(request)
        backend = PolicyOAuthBackend(strategy, redirect_uri=request.path)
        partial = partial_store(strategy, backend, settings.SOCIAL_AUTH_PIPELINE.index(step), user=self.user,
                                uid='partial-id', details={'email': self.user.email}, response={}, **kwargs)
        request.session[PARTIAL_TOKEN_SESSION_NAME] = partial.token
        request.session.save()

    def test_linked_managed_oauth_login_denied_before_login_callback(self):
        association = self.link()
        self.enable()
        before = self.credentials()
        with patch('judge.social_auth.social_views._do_login') as login:
            response = self.callback()
        self.assertTrue(response.url.startswith(reverse('social_auth_error')))
        login.assert_not_called()
        self.assertNotIn(auth.SESSION_KEY, self.client.session)
        self.assert_no_credential_changes(before)
        association.refresh_from_db()
        self.assertEqual(association.extra_data, {})

    def test_email_association_to_managed_user_denied_without_creating_link(self):
        self.enable()
        response = self.callback(uid='new-id')
        self.assertTrue(response.url.startswith(reverse('social_auth_error')))
        self.assertFalse(UserSocialAuth.objects.exists())
        self.assertNotIn(auth.SESSION_KEY, self.client.session)

    def test_resumed_username_and_profile_steps_recheck_current_policy(self):
        for step in ('judge.social_auth.get_username_password', 'judge.social_auth.make_profile'):
            with self.subTest(step=step):
                self.store_partial(step, is_new=True)
                self.enable()
                response = self.callback()
                self.assertTrue(response.url.startswith(reverse('social_auth_error')))
                self.assertFalse(UserSocialAuth.objects.exists())
                self.assertNotIn(auth.SESSION_KEY, self.client.session)
                self.disable()

    def test_final_login_guard_still_applies_with_custom_pipeline(self):
        self.link()
        self.enable()
        pipeline = tuple(step for step in settings.SOCIAL_AUTH_PIPELINE
                         if step.startswith('social_core.') and not step.endswith('create_user'))
        with (override_settings(SOCIAL_AUTH_PIPELINE=pipeline),
              patch('judge.social_auth.social_views._do_login') as login):
            response = self.callback()
        self.assertTrue(response.url.startswith(reverse('social_auth_error')))
        login.assert_not_called()
        self.assertNotIn(auth.SESSION_KEY, self.client.session)

    def test_managed_session_cannot_begin_associate_or_disconnect(self):
        association = self.link()
        self.managed_session()
        self.assertEqual(self.client.get(reverse('social:begin', args=[PolicyOAuthBackend.name])).status_code, 403)
        self.assertEqual(self.callback(uid='new-id').status_code, 403)
        for name, args in (('social:disconnect', [PolicyOAuthBackend.name]),
                           ('social:disconnect_individual', [PolicyOAuthBackend.name, association.pk])):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name, args=args)).status_code, 403)
        self.assertEqual(UserSocialAuth.objects.filter(user=self.user).count(), 1)

    def test_off_oauth_login_and_disconnect_work_without_rotation(self):
        association = self.link()
        response = self.callback()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session[auth.SESSION_KEY]), self.user.pk)
        self.assertNotIn(COMPLETED_LOGIN_KEY, self.client.session)
        self.assertEqual(self.rotations().count(), 0)
        url = reverse('social:disconnect_individual', args=[PolicyOAuthBackend.name, association.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(UserSocialAuth.objects.filter(pk=association.pk).exists())

    def test_off_authenticated_oauth_association_still_works(self):
        self.client.force_login(self.user, backend=BACKEND)
        response = self.callback(uid='new-id')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(UserSocialAuth.objects.filter(user=self.user, uid='new-id').exists())

    def test_turning_off_policy_preserves_existing_association(self):
        association = self.link()
        self.enable()
        self.disable()
        self.assertTrue(UserSocialAuth.objects.filter(pk=association.pk).exists())
        self.assertEqual(self.callback().status_code, 302)
        self.assertEqual(int(self.client.session[auth.SESSION_KEY]), self.user.pk)

    def test_off_post_callback_keeps_social_auth_csrf_contract(self):
        self.link()
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse('social:complete', args=[PolicyOAuthBackend.name]), {
            'uid': 'linked-id', 'email': self.user.email,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(client.session[auth.SESSION_KEY]), self.user.pk)

    def test_off_oauth_disconnect_requires_csrf(self):
        association = self.link()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user, backend=BACKEND)
        response = client.post(reverse('social:disconnect', args=[PolicyOAuthBackend.name]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(UserSocialAuth.objects.filter(pk=association.pk).exists())


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', AUTH_PASSWORD_VALIDATORS=[],
                   WEBAUTHN_RP_ID='testserver', AUTHENTICATION_BACKENDS=(OAUTH_BACKEND, BACKEND))
class ImpersonationCredentialTests(PolicyData, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_user('administrator', is_staff=True, is_superuser=True)
        self.client.force_login(self.admin, backend=BACKEND)
        session = self.client.session
        session['_impersonate'] = self.user.pk
        session.save()

    def test_impersonation_cannot_manage_credentials_even_for_off_user(self):
        before = self.credentials()
        names = ('password_change', 'generate_api_token', 'remove_api_token', 'generate_scratch_codes',
                 'enable_2fa', 'refresh_2fa', 'disable_2fa', 'webauthn_attest', 'password_reset')
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code in (403, 405), True)
                self.assertEqual(self.client.post(reverse(name)).status_code, 403)
        self.assert_no_credential_changes(before)

    def test_impersonation_cannot_confirm_reset_or_associate_oauth(self):
        self.assertEqual(self.client.get(self.reset_url()).status_code, 403)
        self.assertEqual(self.client.get(reverse('social:begin', args=[PolicyOAuthBackend.name])).status_code, 403)
        self.assertEqual(self.client.get(reverse('social:complete', args=[PolicyOAuthBackend.name])).status_code, 403)
        response = self.client.post(reverse('social:disconnect', args=[PolicyOAuthBackend.name]))
        self.assertEqual(response.status_code, 403)

    def test_impersonation_does_not_consume_managed_password(self):
        self.enable()
        before = self.credentials()
        response = self.password_login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rotations().count(), 0)
        self.assert_no_credential_changes(before)

    def test_hidden_impersonation_marker_cannot_consume_password(self):
        self.enable()
        request = self.request()
        request.session['_impersonate'] = self.user.pk
        with self.assertRaises(ManagedLoginRejected):
            begin_password_login(request, self.authenticated_user(self.user), PASSWORD)
        self.assertEqual(self.rotations().count(), 0)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', AUTH_PASSWORD_VALIDATORS=[])
class CredentialPolicyRaceTests(PolicyData, TransactionTestCase):
    def setUp(self):
        super().setUp()
        if not connection.features.has_select_for_update:
            self.skipTest('Requires SELECT FOR UPDATE support.')

    def worker(self, action, results, name):
        def run():
            close_old_connections()
            try:
                results[name] = action()
            except Exception as exc:
                results[name] = exc
            finally:
                close_old_connections()
        thread = Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 20)
        return thread

    def test_toggle_waits_for_token_generation_then_revokes_token(self):
        self.client.force_login(self.user, backend=BACKEND)
        entered, release, attempted = Event(), Event(), Event()
        results = {}
        original = Profile.generate_api_token

        def generate(profile):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Token write was not released')
            return original(profile)

        def toggle():
            attempted.set()
            return set_login_policy(self.actor, self.user.pk, True, 0)

        with patch.object(Profile, 'generate_api_token', generate):
            upload = self.worker(lambda: self.client.post(reverse('generate_api_token')), results, 'token')
            self.assertTrue(entered.wait(10))
            policy = self.worker(toggle, results, 'policy')
            self.assertTrue(attempted.wait(10))
            release.set()
            upload.join(20)
            policy.join(20)
        self.assertFalse(upload.is_alive())
        self.assertFalse(policy.is_alive())
        self.assertEqual(results['token'].status_code, 200, results)
        self.assertTrue(results['policy'].enabled, results)
        self.assertIsNone(Profile.objects.get(user=self.user).api_token)
        token = results['token'].json()['data']['token']
        response = Client().get(reverse('user_edit_profile'), HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(response.status_code, 401)

    def test_waiting_stale_credential_request_sees_new_policy_under_lock(self):
        self.client.force_login(self.user, backend=BACKEND)
        request = RequestFactory().post(reverse('generate_api_token'))
        request.user, request.profile, request.session = self.user, self.user.profile, self.client.session
        started = Event()
        results = {}

        def generate():
            started.set()
            return generate_api_token(request)

        with transaction.atomic():
            User.objects.select_for_update().get(pk=self.user.pk)
            worker = self.worker(generate, results, 'response')
            self.assertTrue(started.wait(10))
            set_login_policy(self.actor, self.user.pk, True, 0)
        worker.join(20)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(results['response'], PermissionDenied)
        self.assertIsNone(Profile.objects.get(user=self.user).api_token)
