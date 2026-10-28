"""Guards for credentials outside the managed password login flow."""

from contextlib import contextmanager
from functools import wraps

from django.contrib import auth
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext_lazy as _

from judge.models import Profile
from judge.utils.managed_login import is_impersonating, session_epoch_matches

MANAGED_CREDENTIAL_MESSAGE = _(
    'Sign-in settings are managed by the invigilator. Contact the invigilator if you need a new password.',
)
IMPERSONATED_CREDENTIAL_MESSAGE = _('Sign-in settings cannot be changed while impersonating another account.')


@contextmanager
def locked_credential_account(user_id):
    """Serialize alternate credential writes with login, reset and policy changes."""
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_id).first()
        if user is None:
            raise PermissionDenied()
        profile = Profile.objects.select_for_update().filter(user=user).first()
        if not user.is_active:
            raise PermissionDenied()
        if profile is not None and profile.rotate_password_on_login:
            raise PermissionDenied(MANAGED_CREDENTIAL_MESSAGE)
        if profile is not None:
            user.profile = profile
            profile.user = user
        yield user, profile


def credential_management_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if is_impersonating(request):
            raise PermissionDenied(IMPERSONATED_CREDENTIAL_MESSAGE)
        if not request.user.is_authenticated:
            return view(request, *args, **kwargs)
        with locked_credential_account(request.user.pk) as (user, profile):
            # AuthenticationMiddleware ran before these locks. An administrator
            # may have reset the password or toggled policy in the meantime.
            if getattr(request, '_api_authenticated', False):
                if (profile is None or not profile.api_token or
                        not constant_time_compare(profile.api_token, getattr(request, '_api_token_digest', ''))):
                    raise PermissionDenied()
            else:
                session_hash = request.session.get(auth.HASH_SESSION_KEY, '')
                if (not constant_time_compare(session_hash, user.get_session_auth_hash()) or
                        (profile is not None and not session_epoch_matches(request.session, profile))):
                    raise PermissionDenied(_('Your sign-in has changed. Sign in again before changing credentials.'))
            request.user = request._cached_user = user
            request.profile = profile
            return view(request, *args, **kwargs)
    return wrapped
