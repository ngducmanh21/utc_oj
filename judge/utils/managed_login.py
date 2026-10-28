"""Managed password login: serialize credential consumption with policy changes."""

import json
import logging
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from typing import Optional

from django.conf import settings
from django.contrib import auth
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from judge.models import ManagedAccountEvent, Profile
from judge.utils.user_permissions import can_administer_user

SESSION_EPOCH_KEY = 'account_session_epoch'
MAX_SESSION_EPOCH = (1 << 63) - 1
POLICY_PERMISSIONS = ('auth.change_user', 'judge.manage_login_password_rotation')
PENDING_LOGIN_KEY = 'managed_login_pending'
COMPLETED_LOGIN_KEY = 'managed_login_complete'
PENDING_LOGIN_MAX_AGE = 300
logger = logging.getLogger(__name__)


def is_impersonating(request):
    return (getattr(request.user, 'is_impersonate', False) or '_impersonate' in request.session)


def can_manage_login_policy(actor):
    return (actor.is_authenticated and actor.is_active and actor.is_staff and
            not getattr(actor, 'is_impersonate', False) and actor.has_perms(POLICY_PERMISSIONS))


@contextmanager
def locked_managed_account(actor, user_id, permissions=POLICY_PERMISSIONS):
    """Use the same User -> Profile lock order for admin, API and password changes."""
    if not getattr(actor, 'is_authenticated', False) or getattr(actor, 'is_impersonate', False):
        raise PermissionDenied()
    with transaction.atomic():
        users = {user.pk: user for user in User.objects.select_for_update()
                 .filter(pk__in={actor.pk, user_id}).order_by('pk')}
        current_actor = users.get(actor.pk)
        if (current_actor is None or not current_actor.is_active or not current_actor.is_staff or
                not current_actor.has_perms(permissions)):
            raise PermissionDenied()
        target = users.get(user_id)
        if target is None:
            raise User.DoesNotExist()
        profile = Profile.objects.select_for_update().get(user=target)
        yield current_actor, target, profile


def get_login_policy(actor, user_id):
    with locked_managed_account(actor, user_id) as (current_actor, target, profile):
        if not can_administer_user(current_actor, target):
            raise PermissionDenied()
        if target.is_staff or target.is_superuser:
            raise PermissionDenied(_('Login password rotation cannot be managed for staff or superusers.'))
        return LoginPolicyState(target.pk, profile.rotate_password_on_login,
                                profile.account_session_epoch, profile.password_rotated_at)


def on_admin_password_changed(actor, user):
    """Call inside the transaction that saves the standard admin password form."""
    with locked_managed_account(actor, user.pk, ('auth.change_user',)) as (actor, target, profile):
        if not can_administer_user(actor, target):
            raise PermissionDenied()
        if not profile.rotate_password_on_login:
            return
        previous_epoch = profile.account_session_epoch
        if previous_epoch == MAX_SESSION_EPOCH:
            raise ValidationError(_('The session epoch cannot be increased.'), code='epoch_exhausted')
        profile.account_session_epoch += 1
        profile.api_token = None
        profile.save(update_fields=['account_session_epoch', 'api_token'])
        ManagedAccountEvent.objects.create(
            target=target, target_username=target.username, actor=actor,
            event=ManagedAccountEvent.Event.CREDENTIAL_REISSUED,
            previous_enabled=True, enabled=True, previous_epoch=previous_epoch, epoch=profile.account_session_epoch,
        )


class LoginPolicyConflict(ValidationError):
    def __init__(self):
        super().__init__(_('The login policy has changed. Reload before saving.'), code='stale_login_policy')


class ManagedLoginRejected(ValidationError):
    def __init__(self):
        super().__init__(
            _('This sign-in is no longer valid. Sign in again. If your issued password no longer works, '
              'ask the invigilator for a new one.'), code='invalid_managed_login',
        )


@dataclass(frozen=True)
class LoginPolicyState:
    user_id: int
    enabled: bool
    epoch: int
    rotated_at: Optional[datetime]


def session_epoch_matches(session, profile):
    """Existing sessions without a stamp remain valid only while the epoch is zero."""
    epoch = session.get(SESSION_EPOCH_KEY, 0)
    return type(epoch) is int and 0 <= epoch <= MAX_SESSION_EPOCH and epoch == profile.account_session_epoch


def stamp_session_epoch(request, user):
    # Read the persisted value rather than a possibly stale user.profile cache.
    epoch = Profile.objects.values_list('account_session_epoch', flat=True).get(user_id=user.pk)
    request.session[SESSION_EPOCH_KEY] = epoch


def discard_login_session(request):
    """Fail closed even when the session database is unavailable during cleanup."""
    try:
        auth.logout(request)
    except Exception:
        logger.warning('Could not delete failed login session; discarding its cookie.')
        request.session = import_module(settings.SESSION_ENGINE).SessionStore()
    request.user = request._cached_user = AnonymousUser()
    request.profile = None


@sensitive_variables()
def _factor_fingerprint(profile, lock=False):
    credentials = profile.webauthn_credentials.order_by('pk')
    if lock:
        credentials = credentials.select_for_update()
    # Include factor configuration, but exclude counters consumed by verification.
    # Store only an HMAC in the pending session, never keys or scratch codes.
    state = [profile.is_totp_enabled, profile.totp_key, profile.scratch_codes,
             profile.is_webauthn_enabled,
             list(credentials.values_list('pk', 'cred_id', 'public_key'))]
    return salted_hmac('judge.managed_login.factors', json.dumps(state), algorithm='sha256').hexdigest()


def _check_account(user, profile):
    if (not user.is_active or profile.is_banned or
            (profile.rotate_password_on_login and (user.is_staff or user.is_superuser))):
        raise ManagedLoginRejected()


@sensitive_variables()
def validate_pending_login(request, user, profile, lock=False):
    """Validate a pending password proof; the 2fa_passed flag is never proof."""
    proof = request.session.get(PENDING_LOGIN_KEY)
    _check_account(user, profile)
    if not isinstance(proof, dict) or not profile.rotate_password_on_login:
        raise ManagedLoginRejected()
    issued_at = proof.get('issued_at')
    if (type(issued_at) is not int or not 0 <= time.time() - issued_at <= PENDING_LOGIN_MAX_AGE or
            type(proof.get('user_id')) is not int or proof['user_id'] != user.pk or
            str(user.pk) != request.session.get(auth.SESSION_KEY) or
            proof.get('backend') not in settings.AUTHENTICATION_BACKENDS or
            proof.get('backend') != request.session.get(auth.BACKEND_SESSION_KEY) or
            type(proof.get('epoch')) is not int or proof['epoch'] != profile.account_session_epoch or
            not session_epoch_matches(request.session, profile) or
            not isinstance(proof.get('auth_hash'), str) or
            not constant_time_compare(proof['auth_hash'], user.get_session_auth_hash()) or
            not constant_time_compare(proof['auth_hash'], request.session.get(auth.HASH_SESSION_KEY, '')) or
            not (profile.is_totp_enabled or profile.is_webauthn_enabled) or
            not isinstance(proof.get('factors'), str) or
            not constant_time_compare(proof['factors'], _factor_fingerprint(profile, lock=lock))):
        raise ManagedLoginRejected()
    return proof


@sensitive_variables()
def _rotate_password(request, user, profile):
    if profile.account_session_epoch == MAX_SESSION_EPOCH:
        raise ManagedLoginRejected()
    previous_epoch = profile.account_session_epoch
    user.set_password(secrets.token_urlsafe(48))
    user.save(update_fields=['password'])
    profile.account_session_epoch += 1
    profile.password_rotated_at = timezone.now()
    profile.api_token = None
    profile.save(update_fields=['account_session_epoch', 'password_rotated_at', 'api_token'])
    ManagedAccountEvent.objects.create(
        target=user, target_username=user.username, event=ManagedAccountEvent.Event.PASSWORD_ROTATED,
        previous_enabled=True, enabled=True, previous_epoch=previous_epoch, epoch=profile.account_session_epoch,
    )
    auth.update_session_auth_hash(request, user)
    request.session[SESSION_EPOCH_KEY] = profile.account_session_epoch
    request.session[COMPLETED_LOGIN_KEY] = profile.account_session_epoch
    request.session['2fa_passed'] = True
    request.session.pop('password_pwned', None)
    request.session.pop(PENDING_LOGIN_KEY, None)
    request.session.pop('webauthn_assert', None)


@sensitive_variables()
def begin_password_login(request, authenticated_user, submitted_password, password_pwned=False):
    """Recheck credentials under lock, then finalize or issue a five-minute 2FA proof."""
    try:
        with transaction.atomic():
            if request.method != 'POST' or is_impersonating(request):
                raise ManagedLoginRejected()
            user = User.objects.select_for_update().get(pk=authenticated_user.pk)
            profile = Profile.objects.select_for_update().get(user=user)
            _check_account(user, profile)
            if not check_password(submitted_password, user.password):
                raise ManagedLoginRejected()
            backend = authenticated_user.backend
            if backend not in settings.AUTHENTICATION_BACKENDS:
                raise ManagedLoginRejected()

            auth.login(request, user, backend=backend)
            request.profile = profile
            for key in (PENDING_LOGIN_KEY, COMPLETED_LOGIN_KEY, '2fa_passed', 'webauthn_assert'):
                request.session.pop(key, None)
            request.session['password_pwned'] = password_pwned
            pending = False
            if profile.rotate_password_on_login:
                request.session.pop('password_pwned', None)
                if profile.is_totp_enabled or profile.is_webauthn_enabled:
                    pending = True
                    request.session[PENDING_LOGIN_KEY] = {
                        'user_id': user.pk, 'backend': backend, 'auth_hash': user.get_session_auth_hash(),
                        'epoch': profile.account_session_epoch, 'issued_at': int(time.time()),
                        'factors': _factor_fingerprint(profile, lock=True),
                    }
                else:
                    _rotate_password(request, user, profile)
            # Catch session persistence failures before committing credential changes.
            request.session.save()
        return pending
    except (User.DoesNotExist, Profile.DoesNotExist):
        discard_login_session(request)
        raise ManagedLoginRejected() from None
    except Exception:
        discard_login_session(request)
        raise


@sensitive_variables()
def complete_managed_login(request, pending_proof, second_factor):
    """Validate and consume the factor and password together, using fresh locked rows."""
    try:
        with transaction.atomic():
            if request.method != 'POST' or is_impersonating(request):
                raise ManagedLoginRejected()
            user = User.objects.select_for_update().get(pk=request.user.pk)
            profile = Profile.objects.select_for_update().get(user=user)
            proof = validate_pending_login(request, user, profile, lock=True)
            if pending_proof != proof:
                raise ManagedLoginRejected()
            second_factor.profile = profile
            # Force validation here even if a caller evaluated form.errors earlier.
            second_factor.full_clean()
            if second_factor.errors:
                return False
            request.user = user
            request.profile = profile
            _rotate_password(request, user, profile)
            request.session.save()
        return True
    except (User.DoesNotExist, Profile.DoesNotExist):
        discard_login_session(request)
        raise ManagedLoginRejected() from None
    except Exception:
        discard_login_session(request)
        raise


@transaction.atomic
def set_login_policy(actor, user_id, enabled, expected_epoch, reason=''):
    """Change policy and revoke old sessions/tokens without changing the password."""
    if not getattr(actor, 'is_authenticated', False) or getattr(actor, 'is_impersonate', False):
        raise PermissionDenied()
    if type(user_id) is not int or user_id <= 0:
        raise ValidationError(_('Invalid user ID.'), code='invalid_user_id')
    if type(enabled) is not bool:
        raise ValidationError(_('The login policy must be enabled or disabled.'), code='invalid_policy')
    if type(expected_epoch) is not int or not 0 <= expected_epoch <= MAX_SESSION_EPOCH:
        raise ValidationError(_('Invalid session epoch.'), code='invalid_epoch')
    if not isinstance(reason, str) or len(reason.strip()) > 255:
        raise ValidationError(_('The optional reason must be text of at most 255 characters.'), code='invalid_reason')

    # Lock every User first, in primary-key order, then the target Profile.
    users = {user.pk: user for user in User.objects.select_for_update()
             .filter(pk__in={actor.pk, user_id}).order_by('pk')}
    current_actor = users.get(actor.pk)
    if (current_actor is None or not current_actor.is_active or not current_actor.is_staff or
            not current_actor.has_perms(POLICY_PERMISSIONS)):
        raise PermissionDenied()
    target = users.get(user_id)
    if target is None:
        raise User.DoesNotExist()
    if not can_administer_user(current_actor, target):
        raise PermissionDenied()
    if target.is_staff or target.is_superuser:
        raise PermissionDenied(_('Login password rotation cannot be managed for staff or superusers.'))
    profile = Profile.objects.select_for_update().get(user=target)
    if not target.is_active or profile.is_banned:
        raise ValidationError(_('The account is disabled or banned.'), code='inactive_account')
    if profile.account_session_epoch != expected_epoch:
        raise LoginPolicyConflict()

    if profile.rotate_password_on_login != enabled:
        if profile.account_session_epoch == MAX_SESSION_EPOCH:
            raise ValidationError(_('The session epoch cannot be increased.'), code='epoch_exhausted')
        previous_enabled = profile.rotate_password_on_login
        profile.rotate_password_on_login = enabled
        profile.account_session_epoch += 1
        profile.api_token = None
        profile.save(update_fields=['rotate_password_on_login', 'account_session_epoch', 'api_token'])
        ManagedAccountEvent.objects.create(
            target=target, target_username=target.username, actor=current_actor,
            event=(ManagedAccountEvent.Event.POLICY_ENABLED if enabled else ManagedAccountEvent.Event.POLICY_DISABLED),
            reason=reason.strip(), previous_enabled=previous_enabled, enabled=enabled,
            previous_epoch=expected_epoch, epoch=profile.account_session_epoch,
        )

    return LoginPolicyState(user_id=target.pk, enabled=profile.rotate_password_on_login,
                            epoch=profile.account_session_epoch, rotated_at=profile.password_rotated_at)
