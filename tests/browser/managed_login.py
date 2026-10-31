"""M5 browser acceptance tests. Run this label explicitly; see the local runbook."""

import json
from importlib import import_module
from unittest.mock import patch
from urllib.parse import urlsplit

import pyotp
from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError
from django.test import override_settings, tag
from django.urls import reverse
from social_django.models import UserSocialAuth

from judge.models import Language, ManagedAccountEvent, Problem, ProblemGroup, Profile, WebAuthnCredential
from judge.social_auth import GitHubSecureEmailOAuth2
from judge.tests.test_managed_login import ManagedAccountData
from judge.tests.test_managed_password_login import PASSWORD, SCRATCH_CODE, TOTP_KEY
from judge.utils.managed_login import PENDING_LOGIN_KEY
from tests.browser.oauth import OAuthProvider
from tests.browser.support import HTTPSBrowserTestCase


@tag('browser', 'managed-login')
@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', WEBAUTHN_RP_ID='localhost',
                   AUTH_PASSWORD_VALIDATORS=[], SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.PBKDF2PasswordHasher'])
class ManagedLoginBrowserTests(ManagedAccountData, HTTPSBrowserTestCase):
    def setUp(self):
        cache.clear()
        self.actor = self.create_actor()
        self.user = self.create_user('m5_contestant', email='m5@example.com')
        self.a, self.b, self.c = self.new_page(), self.new_page(), self.new_page()
        # Stub the external compromised-password lookup. Password hashing,
        # middleware, CSRF, TOTP and WebAuthn verification remain real.
        validator = patch('judge.views.user.PwnedPasswordsValidator.validate')
        validator.start()
        self.addCleanup(validator.stop)
        self.login(self.a, self.actor.username, PASSWORD, reverse('admin:auth_user_changelist'))
        self.assert_path(self.a, reverse('admin:auth_user_changelist'))

    def go(self, page, path):
        return self.ui(lambda: page.goto(self.live_server_url + path)).status

    def path(self, page):
        return urlsplit(self.ui(lambda: page.url)).path

    def assert_path(self, page, path):
        self.assertEqual(self.path(page), path)

    def login(self, page, username=None, password=PASSWORD, next_url=None):
        self.go(page, reverse('auth_login') + '?next=' + (next_url or reverse('user_edit_profile')))
        self.ui(lambda: page.locator('[name=username]').fill(username or self.user.username))
        self.ui(lambda: page.locator('[name=password]').fill(password))

        def submit():
            with page.expect_response(lambda response: urlsplit(response.url).path == reverse('auth_login') and
                                      response.request.method == 'POST') as posted:
                page.locator('form.form-area button[type=submit]').click()
            return posted.value.status

        status = self.ui(submit)
        self.ui(lambda: page.wait_for_load_state())
        return status

    def toggle(self, enabled):
        self.go(self.a, reverse('admin:auth_user_change', args=[self.user.pk]))
        self.ui(lambda: self.a.locator('[name=rotate_password_on_login]').set_checked(enabled))
        self.ui(lambda: self.a.locator('[name=_save]').click())
        self.assert_path(self.a, reverse('admin:auth_user_changelist'))

    def reissue(self, password):
        self.go(self.a, reverse('admin:auth_user_password_change', args=[self.user.pk]))
        self.ui(lambda: self.a.locator('[name=password1]').fill(password))
        self.ui(lambda: self.a.locator('[name=password2]').fill(password))
        self.ui(lambda: self.a.locator('input[type=submit]').click())
        self.assert_path(self.a, reverse('admin:auth_user_change', args=[self.user.pk]))

    def factor(self, page, code=None):
        self.ui(lambda: page.locator('[name=totp_or_scratch_code]').fill(code or pyotp.TOTP(TOTP_KEY).now()))
        self.ui(lambda: page.locator('[id="2fa-form"] button[type=submit]').click())

    def rotations(self):
        return ManagedAccountEvent.objects.filter(target=self.user, event='password_rotated').count()

    def enable_totp(self):
        Profile.objects.filter(user=self.user).update(
            is_totp_enabled=True, totp_key=TOTP_KEY, scratch_codes=json.dumps([SCRATCH_CODE]),
        )

    def browser_request(self, page, path, method='POST', data=None, csrf=True):
        return self.ui(lambda: page.evaluate("""async ({path, method, data, csrf}) => {
            const headers = {};
            if (csrf) headers['X-CSRFToken'] = document.querySelector('[name=csrfmiddlewaretoken]').value;
            if (method === 'PATCH') headers['Content-Type'] = 'application/json';
            const response = await fetch(path, {method, credentials: 'same-origin', headers,
                body: method === 'GET' ? undefined : method === 'PATCH' ? JSON.stringify(data) :
                    new URLSearchParams(data || {})});
            return {status: response.status, path: new URL(response.url).pathname, body: await response.text()};
        }""", {'path': path, 'method': method, 'data': data, 'csrf': csrf}))

    def test_one_use_password_session_tabs_logout_and_proctor_reissue(self):
        self.toggle(True)
        self.login(self.b)
        self.assert_path(self.b, reverse('user_edit_profile'))
        self.assertEqual(self.rotations(), 1)
        cookies = self.ui(lambda: self.b.context.cookies())
        self.assertTrue(next(c for c in cookies if c['name'] == settings.SESSION_COOKIE_NAME)['secure'])
        self.ui(lambda: self.b.reload())
        tab = self.ui(lambda: self.b.context.new_page())
        self.assertEqual(self.go(tab, reverse('user_edit_profile')), 200)
        group = ProblemGroup.objects.create(name='m5', full_name='M5')
        problem = Problem.objects.create(code='m5_sample', name='M5 sample', group=group,
                                         is_public=True, points=100, time_limit=1, memory_limit=65536)
        problem.allowed_languages.add(Language.objects.get(key='CPP17'))
        self.assertEqual(self.go(self.b, reverse('problem_submit', args=[problem.code])), 200)
        self.assert_path(self.b, reverse('problem_submit', args=[problem.code]))
        self.login(self.c)
        self.assert_path(self.c, reverse('auth_login'))
        self.assertEqual(self.rotations(), 1)
        # Recover an already consumed password through the actual Admin form.
        self.reissue('M5-reissued-password-2026')
        self.go(self.b, reverse('user_edit_profile'))
        self.assert_path(self.b, reverse('auth_login'))
        self.login(self.b, password='M5-reissued-password-2026')
        self.assert_path(self.b, reverse('user_edit_profile'))
        self.assertEqual(self.rotations(), 2)
        self.capture(self.b, 'managed-profile')
        result = self.browser_request(self.b, reverse('auth_logout'))
        self.assertEqual(result['status'], 200)
        self.login(self.b, password='M5-reissued-password-2026')
        self.assert_path(self.b, reverse('auth_login'))
        self.toggle(False)
        self.login(self.c, password='M5-reissued-password-2026')
        self.assert_path(self.c, reverse('auth_login'))
        self.reissue('M5-normal-password-2026')
        for page in (self.b, self.c):
            self.login(page, password='M5-normal-password-2026')
            self.assert_path(page, reverse('user_edit_profile'))
        self.assertEqual(self.rotations(), 2)

    def oauth_provider(self):
        callback = self.live_server_url + reverse('social:complete', args=['github-secure'])
        provider = OAuthProvider(callback, self.user.email).start()
        self.addCleanup(provider.close)
        config = override_settings(
            AUTHENTICATION_BACKENDS=(
                'tests.browser.oauth.FixtureGitHubOAuth2', 'django.contrib.auth.backends.ModelBackend',
            ),
            M5_OAUTH_PROVIDER_URL=provider.url,
            SOCIAL_AUTH_GITHUB_SECURE_KEY='m5-client', SOCIAL_AUTH_GITHUB_SECURE_SECRET='m5-test-secret',
        )
        config.enable()
        self.addCleanup(config.disable)
        # social-auth caches backend classes by name independently of settings.
        # A rendered login page may already have cached the production backend.
        self.enterContext(patch('social_core.backends.utils.BACKENDSCACHE', {}))
        UserSocialAuth.objects.create(user=self.user, provider='github-secure', uid='424242')
        return provider

    def begin_oauth(self, page):
        self.assertEqual(self.go(page, reverse('social:begin', args=['github-secure']) +
                                 '?next=' + reverse('user_edit_profile')), 200)
        self.assertEqual(urlsplit(self.ui(lambda: page.url)).netloc, urlsplit(settings.M5_OAUTH_PROVIDER_URL).netloc)
        self.assert_path(page, '/authorize')

    def approve_oauth(self, page):
        self.ui(lambda: page.get_by_role('button', name='Approve test identity').click())

    def test_oauth_http_round_trip_off_on_off_preserves_link(self):
        # Exercise the fixture after another request has populated the cache.
        self.enterContext(patch('social_core.backends.utils.BACKENDSCACHE', {
            'github-secure': GitHubSecureEmailOAuth2,
        }))
        provider = self.oauth_provider()
        self.begin_oauth(self.b)
        self.approve_oauth(self.b)
        self.assert_path(self.b, reverse('user_edit_profile'))
        self.assertEqual(provider.token_requests, 1)
        self.assertEqual(provider.user_requests, 2)
        self.toggle(True)
        self.begin_oauth(self.c)
        self.approve_oauth(self.c)
        self.assert_path(self.c, reverse('social_auth_error'))
        self.go(self.c, reverse('user_edit_profile'))
        self.assert_path(self.c, reverse('auth_login'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        self.assertEqual(self.rotations(), 0)
        self.assertEqual(UserSocialAuth.objects.filter(user=self.user).count(), 1)
        self.toggle(False)
        self.begin_oauth(self.c)
        self.approve_oauth(self.c)
        self.assert_path(self.c, reverse('user_edit_profile'))
        self.assertEqual(provider.token_requests, 3)

    def test_oauth_in_flight_callback_rechecks_policy(self):
        provider = self.oauth_provider()
        self.begin_oauth(self.c)
        self.toggle(True)
        self.approve_oauth(self.c)
        self.assert_path(self.c, reverse('social_auth_error'))
        self.assertEqual(provider.token_requests, 1)
        self.go(self.c, reverse('user_edit_profile'))
        self.assert_path(self.c, reverse('auth_login'))
        self.assertEqual(self.rotations(), 0)

    def test_oauth_state_mismatch_is_rejected_before_token_exchange(self):
        provider = self.oauth_provider()
        self.begin_oauth(self.c)
        code = self.ui(lambda: self.c.locator('[name=code]').input_value())
        callback = reverse('social:complete', args=['github-secure']) + '?code=' + code + '&state=wrong'
        self.go(self.c, callback)
        self.assert_path(self.c, reverse('social_auth_error'))
        self.assertEqual(provider.token_requests, 0)
        self.go(self.c, reverse('user_edit_profile'))
        self.assert_path(self.c, reverse('auth_login'))

    def test_oauth_association_and_disconnect_are_guarded_for_managed_session(self):
        self.oauth_provider()
        self.toggle(True)
        self.login(self.b)
        begin = reverse('social:begin', args=['github-secure'])
        disconnect = reverse('social:disconnect', args=['github-secure'])
        self.assertEqual(self.browser_request(self.b, begin, 'GET')['status'], 403)
        self.assertEqual(self.browser_request(self.b, disconnect)['status'], 403)
        self.assertTrue(UserSocialAuth.objects.filter(user=self.user).exists())
        self.toggle(False)
        self.reissue('M5-disconnect-2026')
        self.login(self.b, password='M5-disconnect-2026')
        self.assertEqual(self.browser_request(self.b, disconnect)['status'], 200)
        self.assertFalse(UserSocialAuth.objects.filter(user=self.user).exists())
        self.assertEqual(self.rotations(), 1)

    def test_stale_admin_form_and_policy_api_csrf(self):
        change = reverse('admin:auth_user_change', args=[self.user.pk])
        self.go(self.a, change)
        stale = self.ui(lambda: self.a.context.new_page())
        self.go(stale, change)
        self.toggle(True)
        self.ui(lambda: stale.locator('[name=first_name]').fill('Must not save'))
        self.ui(lambda: stale.locator('[name=_save]').click())
        self.assert_path(stale, change)
        self.assertIn('Reload before saving', self.ui(lambda: stale.locator('body').inner_text()))
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, '')
        self.assertTrue(self.user.profile.rotate_password_on_login)
        api = reverse('admin:auth_user_login_policy', args=[self.user.pk])
        payload = {'enabled': False, 'expected_epoch': 1}
        self.assertEqual(self.browser_request(stale, api, 'PATCH', payload, csrf=False)['status'], 403)
        self.assertEqual(self.browser_request(stale, api, 'PATCH', {**payload, 'expected_epoch': 0})['status'], 409)
        self.assertEqual(self.browser_request(stale, api, 'PATCH', {**payload, 'enabled': 1})['status'], 422)
        self.assertEqual(self.browser_request(stale, api, 'PATCH', payload)['status'], 200)
        self.assertEqual(ManagedAccountEvent.objects.filter(target=self.user).count(), 2)

    def test_two_pending_totp_sessions_only_first_completes(self):
        self.enable_totp()
        self.toggle(True)
        for page in (self.b, self.c):
            self.login(page)
            self.assert_path(page, reverse('login_2fa'))
        self.factor(self.b, 'not-a-code')
        self.assert_path(self.b, reverse('login_2fa'))
        self.assertEqual(self.rotations(), 0)
        self.go(self.b, reverse('user_edit_profile'))
        self.assert_path(self.b, reverse('login_2fa'))
        self.factor(self.b)
        self.assert_path(self.b, reverse('user_edit_profile'))
        self.factor(self.c)
        self.assert_path(self.c, reverse('auth_login'))
        self.assertEqual(self.rotations(), 1)

    def test_pending_expiry_factor_change_and_admin_reissue(self):
        self.enable_totp()
        self.toggle(True)
        self.login(self.b)
        cookie = self.ui(lambda: next(c['value'] for c in self.b.context.cookies()
                                      if c['name'] == settings.SESSION_COOKIE_NAME))
        session = import_module(settings.SESSION_ENGINE).SessionStore(session_key=cookie)
        session[PENDING_LOGIN_KEY]['issued_at'] -= 301
        session.save()
        self.factor(self.b)
        self.assert_path(self.b, reverse('auth_login'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        self.login(self.b)
        Profile.objects.filter(user=self.user).update(totp_key=pyotp.random_base32())
        self.go(self.b, reverse('user_edit_profile'))
        self.assert_path(self.b, reverse('auth_login'))
        self.enable_totp()
        self.login(self.b)
        self.reissue('M5-pending-reset-2026')
        self.factor(self.b)
        self.assert_path(self.b, reverse('auth_login'))
        self.login(self.b, password='M5-pending-reset-2026')
        self.factor(self.b, SCRATCH_CODE)
        self.assert_path(self.b, reverse('user_edit_profile'))
        self.assertEqual(self.rotations(), 1)
        self.assertEqual(json.loads(Profile.objects.get(user=self.user).scratch_codes), [])

    def test_managed_profile_and_direct_credential_posts_are_guarded(self):
        self.toggle(True)
        self.login(self.b)
        self.assertEqual(self.ui(lambda: self.b.locator('#new-webauthn').count()), 0)
        self.ui(lambda: self.b.locator('[name=first_name]').fill('M5 student'))
        self.ui(lambda: self.b.locator('#edit-form button[type=submit], #edit-form input[type=submit]').click())
        self.ui(lambda: self.b.wait_for_load_state())
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'M5 student')
        self.assertTrue(self.user.profile.rotate_password_on_login)
        endpoints = ('password_change', 'generate_api_token', 'generate_scratch_codes', 'enable_2fa', 'webauthn_attest')
        for name in endpoints:
            with self.subTest(endpoint=name):
                self.assertEqual(self.browser_request(self.b, reverse(name))['status'], 403)
        self.assertEqual(self.rotations(), 1)
        # A normal Staff account cannot promote itself through a forged POST.
        self.go(self.a, reverse('admin:auth_user_change', args=[self.actor.pk]))
        self.assertEqual(self.ui(lambda: self.a.locator('[name=is_superuser]').count()), 0)
        result = self.browser_request(self.a, self.path(self.a), data={'is_superuser': 'on', 'first_name': 'Forged'})
        self.assertEqual(result['status'], 403)
        self.actor.refresh_from_db()
        self.assertFalse(self.actor.is_superuser)
        self.assertEqual(self.actor.first_name, '')

    def test_lost_browser_cookie_after_rotation_requires_proctor_reissue(self):
        self.toggle(True)
        self.login(self.b)
        self.assertEqual(self.rotations(), 1)
        self.ui(lambda: self.b.context.clear_cookies())
        self.go(self.b, reverse('user_edit_profile'))
        self.assert_path(self.b, reverse('auth_login'))
        self.login(self.b)
        self.assert_path(self.b, reverse('auth_login'))
        self.assertEqual(self.rotations(), 1)
        self.reissue('M5-lost-cookie-recovery-2026')
        self.login(self.b, password='M5-lost-cookie-recovery-2026')
        self.assert_path(self.b, reverse('user_edit_profile'))
        self.assertEqual(self.rotations(), 2)

    def test_audit_failure_returns_503_without_consuming_password_or_leaving_session(self):
        self.toggle(True)
        with patch.object(ManagedAccountEvent.objects, 'create', side_effect=DatabaseError('M5 injected failure')):
            self.assertEqual(self.login(self.b), 503)
        self.assertIn('Unable to sign in', self.ui(lambda: self.b.locator('body').inner_text()))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        self.assertEqual(self.rotations(), 0)
        self.go(self.b, reverse('user_edit_profile'))
        self.assert_path(self.b, reverse('auth_login'))
        self.login(self.b)
        self.assert_path(self.b, reverse('user_edit_profile'))
        self.assertEqual(self.rotations(), 1)

    def test_real_webauthn_registration_bad_signature_then_managed_login(self):
        self.login(self.b)

        def add_authenticator():
            cdp = self.b.context.new_cdp_session(self.b)
            cdp.send('WebAuthn.enable')
            authenticator = cdp.send('WebAuthn.addVirtualAuthenticator', {'options': {
                'protocol': 'ctap2', 'transport': 'usb', 'hasResidentKey': False,
                'hasUserVerification': False, 'isUserVerified': False, 'automaticPresenceSimulation': True,
            }})['authenticatorId']
            return cdp, authenticator
        cdp, authenticator = self.ui(add_authenticator)
        self.ui(lambda: self.b.locator('#new-webauthn-name').fill('M5 virtual security key'))

        def register():
            with self.b.expect_response(lambda response: response.url.endswith(reverse('webauthn_attest')) and
                                        response.request.method == 'POST') as posted:
                self.b.locator('#new-webauthn').click()
            return posted.value.status
        self.assertEqual(self.ui(register), 200)
        credential = WebAuthnCredential.objects.get(user=self.user.profile)
        self.assertTrue(credential.public_key)
        registered_counter = credential.counter
        self.assertGreaterEqual(registered_counter, 0)
        self.toggle(True)
        self.login(self.b)
        self.assert_path(self.b, reverse('login_2fa'))
        self.ui(lambda: cdp.send('WebAuthn.setResponseOverrideBits', {
            'authenticatorId': authenticator, 'isBogusSignature': True,
        }))
        self.ui(lambda: self.b.locator('#use-webauthn').click())
        self.ui(lambda: self.b.locator('#form-errors').wait_for())
        self.assert_path(self.b, reverse('login_2fa'))
        self.assertEqual(self.rotations(), 0)
        credential.refresh_from_db()
        self.assertEqual(credential.counter, registered_counter)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        self.ui(lambda: cdp.send('WebAuthn.setResponseOverrideBits', {
            'authenticatorId': authenticator, 'isBogusSignature': False,
        }))
        self.ui(lambda: self.b.locator('#use-webauthn').click())
        self.ui(lambda: self.b.wait_for_url(self.live_server_url + reverse('user_edit_profile')))
        credential.refresh_from_db()
        self.assertGreater(credential.counter, registered_counter)
        self.assertEqual(self.rotations(), 1)
        self.login(self.c)
        self.assert_path(self.c, reverse('auth_login'))
