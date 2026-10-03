from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.core import mail
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.translation import override
from registration.models import RegistrationProfile
from social_core.backends.base import BaseAuth
from social_core.exceptions import AuthException
from social_core.pipeline.utils import partial_load
from social_core.utils import PARTIAL_TOKEN_SESSION_NAME
from social_django.models import UserSocialAuth
from social_django.utils import load_strategy

from judge.models import Language, Profile
from judge.social_auth import SocialAuthExceptionMiddleware, verify_registration_email
from judge.utils.registration import get_registration_email_domains, validate_registration_email
from judge.views.register import CustomRegistrationForm, RegistrationView


@override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=['lms.utc.edu.vn', 'gmail.com'])
class RegistrationEmailValidatorTests(SimpleTestCase):
    def test_accepts_school_domain_and_normalizes_domain(self):
        for email in ('Student@lms.utc.edu.vn', 'Student@LMS.UTC.EDU.VN', ' Student@lms.utc.edu.vn '):
            with self.subTest(email=email):
                self.assertEqual(validate_registration_email(email), 'Student@lms.utc.edu.vn')

    def test_accepts_gmail_with_uppercase_and_whitespace(self):
        self.assertEqual(validate_registration_email(' Student@GMAIL.COM '), 'Student@gmail.com')

    def test_whitelist_is_read_after_import_and_normalized(self):
        for domains, accepted, rejected in ((['gmail.com'], 'student@gmail.com', 'student@lms.utc.edu.vn'),
                                            (['example.edu'], 'student@example.edu', 'student@gmail.com'),
                                            (['lms.utc.edu.vn'], 'student@lms.utc.edu.vn', 'student@gmail.com')):
            with self.subTest(domains=domains), override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=domains):
                self.assertEqual(validate_registration_email(accepted), accepted)
                with self.assertRaises(ValidationError):
                    validate_registration_email(rejected)
        with override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=[' GMAIL.COM ', 'gmail.com', 'LMS.UTC.EDU.VN']):
            self.assertEqual(get_registration_email_domains(), ['gmail.com', 'lms.utc.edu.vn'])

    @override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=[])
    def test_empty_whitelist_closes_new_registration(self):
        self.assertEqual(get_registration_email_domains(), [])
        with self.assertRaises(ValidationError) as error:
            validate_registration_email('student@gmail.com')
        self.assertEqual(error.exception.code, 'registration_closed')
        self.assertFalse(any(check.id == 'judge.E001' for check in run_checks()))

    def test_invalid_configuration_is_reported_and_never_allows_registration(self):
        for value in ('gmail.com', None, [None], [1], [''], ['@gmail.com'], ['https://gmail.com'],
                      ['*.gmail.com'], ['gmail.com.'], ['bad domain.com'], ['localhost']):
            with self.subTest(value=value), override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=value):
                self.assertTrue(any(check.id == 'judge.E001' for check in run_checks()))
                with self.assertRaises(ImproperlyConfigured):
                    validate_registration_email('student@gmail.com')

    def test_rejects_other_domains_and_suffix_lookalikes(self):
        for domain in ('outlook.com', 'gmail.com.evil.com', 'sub.gmail.com', 'fakegmail.com', 'gmail.com.',
                       'utc.edu.vn', 'sub.lms.utc.edu.vn', 'lms.utc.edu.vn.evil.com',
                       'fakelms.utc.edu.vn', 'lms.utc.edu.vn.'):
            with self.subTest(domain=domain), self.assertRaises(ValidationError):
                validate_registration_email('student@' + domain)

    def test_rejects_empty_and_malformed_email(self):
        for email in ('', None, 'student', '@lms.utc.edu.vn', 'student@@lms.utc.edu.vn',
                      'student name@lms.utc.edu.vn', 'a@lms.utc.edu.vn,b@lms.utc.edu.vn'):
            with self.subTest(email=email), self.assertRaises(ValidationError):
                validate_registration_email(email)

    def test_domain_message_is_translated(self):
        with override('vi'):
            with self.assertRaises(ValidationError) as error:
                validate_registration_email('student@outlook.com')
            self.assertEqual(error.exception.messages, ['Chỉ chấp nhận email có đuôi @lms.utc.edu.vn, @gmail.com.'])


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', AUTH_PASSWORD_VALIDATORS=[], REGISTRATION_OPEN=True,
                   OAUTH_ONLY=False, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   DMOJ_REGISTRATION_EMAIL_DOMAINS=['lms.utc.edu.vn', 'gmail.com'])
class RegistrationEndpointTests(TestCase):
    fixtures = ['language_small']

    def setUp(self):
        self.language = Language.objects.get(key='CPP17')
        self.data = {'username': 'new_student', 'email': 'student@lms.utc.edu.vn',
                     'password1': 'Student-password-2026', 'password2': 'Student-password-2026',
                     'full_name': 'Student', 'timezone': settings.DEFAULT_USER_TIME_ZONE,
                     'language': self.language.pk}
        self.url = reverse('registration_register')

    def counts(self):
        return User.objects.count(), Profile.objects.count(), RegistrationProfile.objects.count()

    def test_get_renders_domain_hint_and_frontend_configuration(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'data-registration-email-domains="lms.utc.edu.vn,gmail.com"')
        self.assertContains(response, 'registration-email.js')
        self.assertContains(response, 'student@lms.utc.edu.vn')
        self.assertNotContains(response, 'please choose a popular email provider')
        self.assertNotContains(response, 'Sign up with Gmail')

    def test_frontend_configuration_follows_whitelist_override(self):
        for domains in (['gmail.com'], ['example.edu'], []):
            with self.subTest(domains=domains), override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=domains):
                response = self.client.get(self.url)
                self.assertContains(response, 'data-registration-email-domains="%s"' % ','.join(domains))
                if domains:
                    self.assertContains(response, 'student@' + domains[0])
                else:
                    self.assertContains(response, 'type="submit" disabled')

    def test_gmail_post_is_accepted_only_when_whitelisted(self):
        payload = {**self.data, 'email': ' Student@GMAIL.COM '}
        with override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=['lms.utc.edu.vn']):
            response = self.client.post(self.url, payload)
            self.assertEqual(response.status_code, 200)
            self.assertIn('email', response.context['form'].errors)
            self.assertFalse(User.objects.filter(username=payload['username']).exists())
        with self.captureOnCommitCallbacks(execute=True), patch.object(RegistrationView, 'SEND_ACTIVATION_EMAIL', True):
            response = self.client.post(self.url, payload)
        self.assertRedirects(response, reverse('registration_complete'))
        self.assertEqual(User.objects.get(username=payload['username']).email, 'Student@gmail.com')
        self.assertEqual(mail.outbox[0].to, ['Student@gmail.com'])

    @override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=[])
    def test_empty_whitelist_rejects_direct_post(self):
        before = self.counts()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('email', response.context['form'].errors)
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(mail.outbox), 0)

    def test_direct_post_rejects_domain_without_creating_accounts_or_sending_email(self):
        before = self.counts()
        for email in ('student@outlook.com', 'student@utc.edu.vn', 'student@sub.lms.utc.edu.vn',
                      'student@lms.utc.edu.vn.evil.com', 'student@fakelms.utc.edu.vn', 'student@@lms.utc.edu.vn'):
            with self.subTest(email=email), self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.url, {**self.data, 'email': email})
                self.assertEqual(response.status_code, 200)
                self.assertIn('email', response.context['form'].errors)
                self.assertEqual(self.counts(), before)
        self.assertEqual(len(mail.outbox), 0)

    def test_valid_post_sends_activation_and_can_activate(self):
        with patch.object(RegistrationView, 'SEND_ACTIVATION_EMAIL', True), \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, {**self.data, 'email': ' Student@LMS.UTC.EDU.VN '})
        self.assertRedirects(response, reverse('registration_complete'))
        user = User.objects.get(username=self.data['username'])
        self.assertEqual(user.email, 'Student@lms.utc.edu.vn')
        self.assertFalse(user.is_active)
        self.assertTrue(Profile.objects.filter(user=user).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        registration = RegistrationProfile.objects.get(user=user)
        response = self.client.get(reverse('registration_activate', args=[registration.activation_key]))
        self.assertRedirects(response, reverse('registration_activation_complete'))
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_duplicate_email_is_still_rejected(self):
        User.objects.create_user('existing_student', email=self.data['email'])
        form = CustomRegistrationForm(self.data)
        self.assertFalse(form.is_valid())
        self.assertIn('already taken', str(form.errors['email']))

    def test_backend_domain_error_in_vietnamese(self):
        response = self.client.post(self.url, {**self.data, 'email': 'student@outlook.com'},
                                    HTTP_ACCEPT_LANGUAGE='vi')
        self.assertContains(response, 'Chỉ chấp nhận email có đuôi @lms.utc.edu.vn, @gmail.com.')

    def test_existing_account_outside_domain_can_login_and_reset_password(self):
        user = User.objects.create_user('old_account', email='old@outlook.com', password=self.data['password1'])
        Profile.objects.create(user=user, language=self.language)
        self.assertTrue(self.client.login(username=user.username, password=self.data['password1']))
        self.client.logout()
        response = self.client.post(reverse('password_reset'), {'email': user.email})
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])


class RegistrationTestBackend(BaseAuth):
    name = 'google-oauth2'
    ID_KEY = 'id'

    def get_user_details(self, response):
        return {'email': response.get('email'), 'username': 'oauth_student'}


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', AUTH_PASSWORD_VALIDATORS=[],
                   DMOJ_REGISTRATION_EMAIL_DOMAINS=['lms.utc.edu.vn', 'gmail.com'])
class SocialRegistrationTests(TestCase):
    fixtures = ['language_small']

    def setUp(self):
        self.request = RequestFactory().get('/complete/google-oauth2/')
        self.request.session = {}
        self.request.user = AnonymousUser()
        self.request.misc_config = {}
        self.request.LANGUAGE_CODE = 'en'
        self.request.in_contest = False
        self.strategy = load_strategy(self.request)
        self.backend = RegistrationTestBackend(self.strategy, redirect_uri='/complete/google-oauth2/')

    def run_pipeline(self, email, uid='new-provider-id', **kwargs):
        return self.backend.run_pipeline(settings.SOCIAL_AUTH_PIPELINE,
                                         response={'id': uid, 'email': email}, **kwargs)

    def test_new_outside_domain_is_rejected_before_account_or_partial_is_created(self):
        with self.assertRaises(AuthException):
            self.run_pipeline('student@outlook.com')
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Profile.objects.count(), 0)
        self.assertEqual(UserSocialAuth.objects.count(), 0)
        self.assertNotIn(PARTIAL_TOKEN_SESSION_NAME, self.request.session)

    def test_new_school_account_can_resume_and_finish_registration(self):
        response = self.run_pipeline(' Student@LMS.UTC.EDU.VN ')
        self.assertContains(response, 'oauth_student')
        self.assertEqual(User.objects.count(), 0)
        partial = partial_load(self.strategy, self.request.session[PARTIAL_TOKEN_SESSION_NAME])
        self.assertEqual(partial.kwargs['details']['email'], 'Student@lms.utc.edu.vn')
        self.request.method = 'POST'
        self.request.POST = {'username': 'oauth_student', 'password': 'Student-password-2026',
                             'password_confirm': 'Student-password-2026'}
        result = self.backend.run_pipeline(settings.SOCIAL_AUTH_PIPELINE, pipeline_index=partial.next_step,
                                           *partial.args, **partial.kwargs)
        self.assertEqual(result.status_code, 200)  # Profile setup follows user creation.
        user = User.objects.get(username='oauth_student')
        self.assertEqual(user.email, 'Student@lms.utc.edu.vn')
        self.assertTrue(user.check_password('Student-password-2026'))

    def test_new_gmail_account_uses_current_whitelist_and_resumes(self):
        with override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=['lms.utc.edu.vn']), self.assertRaises(AuthException):
            self.run_pipeline('student@gmail.com')
        self.run_pipeline(' Student@GMAIL.COM ')
        partial = partial_load(self.strategy, self.request.session[PARTIAL_TOKEN_SESSION_NAME])
        self.request.method = 'POST'
        self.request.POST = {'username': 'oauth_student', 'password': 'Student-password-2026',
                             'password_confirm': 'Student-password-2026'}
        with override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=['lms.utc.edu.vn']), self.assertRaises(AuthException):
            self.backend.run_pipeline(settings.SOCIAL_AUTH_PIPELINE, pipeline_index=partial.next_step,
                                      *partial.args, **partial.kwargs)
        self.assertFalse(User.objects.exists())
        self.backend.run_pipeline(settings.SOCIAL_AUTH_PIPELINE, pipeline_index=partial.next_step,
                                  *partial.args, **partial.kwargs)
        self.assertEqual(User.objects.get(username='oauth_student').email, 'Student@gmail.com')

    @override_settings(DMOJ_REGISTRATION_EMAIL_DOMAINS=[])
    def test_empty_whitelist_blocks_new_oauth_but_allows_existing_login(self):
        with self.assertRaises(AuthException):
            self.run_pipeline('student@gmail.com')
        user = User.objects.create_user('old_account', email='old@outlook.com')
        Profile.objects.create(user=user)
        UserSocialAuth.objects.create(user=user, provider=self.backend.name, uid='old-provider-id')
        result = self.run_pipeline(user.email, uid='old-provider-id')
        self.assertEqual(result['user'].pk, user.pk)
        self.assertEqual(User.objects.count(), 1)

    def test_resumed_partial_with_invalid_domain_cannot_create_account(self):
        self.run_pipeline('student@lms.utc.edu.vn')
        partial = partial_load(self.strategy, self.request.session[PARTIAL_TOKEN_SESSION_NAME])
        partial.kwargs['details']['email'] = 'student@outlook.com'
        self.request.method = 'POST'
        self.request.POST = {'username': 'oauth_student', 'password': 'Student-password-2026',
                             'password_confirm': 'Student-password-2026'}
        with self.assertRaises(AuthException):
            self.backend.run_pipeline(settings.SOCIAL_AUTH_PIPELINE, pipeline_index=partial.next_step,
                                      *partial.args, **partial.kwargs)
        self.assertEqual(User.objects.count(), 0)

    def test_existing_linked_outside_domain_account_can_login(self):
        user = User.objects.create_user('old_oauth', email='old@outlook.com')
        Profile.objects.create(user=user)
        UserSocialAuth.objects.create(user=user, provider=self.backend.name, uid='old-provider-id')
        result = self.run_pipeline(user.email, uid='old-provider-id')
        self.assertEqual(result['user'].pk, user.pk)
        self.assertFalse(result['is_new'])
        self.assertEqual(User.objects.count(), 1)

    def test_existing_account_can_still_associate_by_email(self):
        user = User.objects.create_user('old_account', email='old@outlook.com')
        Profile.objects.create(user=user)
        result = self.run_pipeline(user.email)
        self.assertEqual(result['user'].pk, user.pk)
        self.assertFalse(result['is_new'])
        self.assertEqual(User.objects.count(), 1)

    def test_missing_email_is_rejected(self):
        with self.assertRaises(AuthException):
            self.run_pipeline(None)
        self.assertFalse(User.objects.exists())

    def test_authenticated_existing_account_can_link_an_outside_domain_provider(self):
        user = User.objects.create_user('old_account', email='old@outlook.com')
        Profile.objects.create(user=user)
        result = self.run_pipeline('provider@hotmail.com', user=user)
        self.assertEqual(result['user'].pk, user.pk)
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(UserSocialAuth.objects.filter(user=user, uid='new-provider-id').exists())

    def test_domain_error_uses_social_error_page_in_vietnamese(self):
        with override('vi'), self.assertRaises(AuthException) as error:
            verify_registration_email(self.backend, {'email': 'student@outlook.com'})
        response = SocialAuthExceptionMiddleware(lambda request: None).process_exception(self.request, error.exception)
        self.assertEqual(response.status_code, 302)
        response = self.client.get(response.url)
        self.assertContains(response, 'Chỉ chấp nhận email có đuôi @lms.utc.edu.vn, @gmail.com.')
