import logging
import re
from operator import itemgetter
from urllib.parse import quote

from django import forms
from django.contrib.auth import REDIRECT_FIELD_NAME, password_validation
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from requests import HTTPError
from reversion import revisions
from social_core.actions import do_complete
from social_core.backends.github import GithubOAuth2
from social_core.exceptions import AuthException, InvalidEmail, SocialAuthBaseException
from social_core.pipeline.partial import partial
from social_django import views as social_views
from social_django.middleware import SocialAuthExceptionMiddleware as OldSocialAuthExceptionMiddleware
from social_django.utils import psa

from judge.forms import ProfileForm
from judge.models import Language, Profile
from judge.utils.credential_policy import IMPERSONATED_CREDENTIAL_MESSAGE, credential_management_required, \
    is_impersonating, locked_credential_account
from judge.utils.registration import validate_registration_email

logger = logging.getLogger('judge.social_auth')


@never_cache
@credential_management_required
def auth(request, backend):
    return social_views.auth(request, backend)


@never_cache
@csrf_exempt
@transaction.atomic
@credential_management_required
@psa(f'{social_views.NAMESPACE}:complete')
def complete(request, backend, *args, **kwargs):
    # Pipeline guards keep User -> Profile locks until association and session
    # creation finish, including callbacks resumed from a partial pipeline.
    return do_complete(request.backend, _social_login, user=request.user,
                       redirect_name=REDIRECT_FIELD_NAME, request=request, *args, **kwargs)


def _social_login(backend, user, social_user):
    # Also guard the final login callback if a backend supplies its own pipeline.
    result = verify_login_policy(backend, user=user)
    return social_views._do_login(backend, result['user'], social_user)


@never_cache
@credential_management_required
def disconnect(request, backend, association_id=None):
    return social_views.disconnect(request, backend, association_id)


def verify_login_policy(backend, user=None, *args, **kwargs):
    request = backend.strategy.request
    if is_impersonating(request):
        raise AuthException(backend, str(IMPERSONATED_CREDENTIAL_MESSAGE))
    if user is not None:
        try:
            with locked_credential_account(user.pk) as (user, profile):
                return {'user': user}
        except PermissionDenied as exc:
            raise AuthException(backend, str(exc)) from exc


class GitHubSecureEmailOAuth2(GithubOAuth2):
    name = 'github-secure'

    def user_data(self, access_token, *args, **kwargs):
        data = self._user_data(access_token)
        try:
            emails = self._user_data(access_token, '/emails')
        except (HTTPError, ValueError, TypeError):
            emails = []

        emails = [(e.get('email'), e.get('primary'), 0) for e in emails if isinstance(e, dict) and e.get('verified')]
        emails.sort(key=itemgetter(1), reverse=True)
        emails = list(map(itemgetter(0), emails))

        if emails:
            data['email'] = emails[0]
        else:
            data['email'] = None

        return data


def slugify_username(username, renotword=re.compile(r'[^\w]')):
    return renotword.sub('', username.replace('-', '_'))


def verify_email(backend, details, *args, **kwargs):
    if not details['email']:
        raise InvalidEmail(backend)


def verify_registration_email(backend, details, user=None, *args, **kwargs):
    if user is None:
        try:
            details['email'] = validate_registration_email(details.get('email'))
        except forms.ValidationError as exc:
            raise AuthException(backend, exc.messages[0]) from exc


class SocialPostAuthForm(forms.Form):
    username = forms.RegexField(regex=re.compile(r'^\w+$', re.ASCII), max_length=30, label='Username',
                                error_messages={'invalid': 'A username must contain letters, numbers, or underscores.'})
    password = forms.CharField(label='Password', strip=False, widget=forms.PasswordInput(),
                               help_text=password_validation.password_validators_help_text_html(),
                               validators=[password_validation.validate_password])
    password_confirm = forms.CharField(label='Retype password', widget=forms.PasswordInput(), strip=False)

    def clean_username(self):
        if User.objects.filter(username=self.cleaned_data['username']).exists():
            raise forms.ValidationError('Sorry, the username is taken.')
        return self.cleaned_data['username']

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        password_confirm = cleaned_data.get('password_confirm')
        if password and password_confirm and password != password_confirm:
            self.add_error('password_confirm', "Passwords didn't match")


@partial
def get_username_password(backend, user, details, username=None, *args, **kwargs):
    # Resuming this partial step skips the earlier pipeline validator.
    result = verify_login_policy(backend, user=user)
    if result:
        user = result['user']
    verify_registration_email(backend, details, user=user)
    if not user:
        request = backend.strategy.request
        if request.POST:
            form = SocialPostAuthForm(request.POST)
            if form.is_valid():
                return {'username': form.cleaned_data['username'],
                        'password': form.cleaned_data['password']}
        else:
            form = SocialPostAuthForm(initial={'username': username})
        return render(request, 'registration/username_select.html', {
            'title': 'Set up your account', 'form': form,
        })


def add_password(user, backend, password=None, *args, **kwargs):
    result = verify_login_policy(backend, user=user)
    user = result['user']
    if password:
        user.set_password(password)
        user.save(update_fields=['password'])
    return {'user': user}


@partial
def make_profile(backend, user, response, is_new=False, *args, **kwargs):
    result = verify_login_policy(backend, user=user)
    user = result['user']
    if is_new:
        if not hasattr(user, 'profile'):
            profile = Profile(user=user)
            profile.language = Language.get_default_language()
            logger.info('Info from %s: %s', backend.name, response)
            profile.save()
            form = ProfileForm(instance=profile, user=user)
        else:
            data = backend.strategy.request_data()
            logger.info(data)
            form = ProfileForm(data, instance=user.profile, user=user)
            if form.is_valid():
                with revisions.create_revision(atomic=True):
                    form.save()
                    revisions.set_user(user)
                    revisions.set_comment('Updated on registration')
                    return
        return render(backend.strategy.request, 'registration/profile_creation.html', {
            'title': 'Create your profile', 'form': form,
        })


class SocialAuthExceptionMiddleware(OldSocialAuthExceptionMiddleware):
    def process_exception(self, request, exception):
        if isinstance(exception, SocialAuthBaseException):
            return HttpResponseRedirect('%s?message=%s' % (reverse('social_auth_error'),
                                                           quote(self.get_message(request, exception))))
