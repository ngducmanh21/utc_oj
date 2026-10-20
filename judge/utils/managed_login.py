"""Policy and session primitives; password/2FA finalization is a separate milestone."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from judge.models import ManagedAccountEvent, Profile

SESSION_EPOCH_KEY = 'account_session_epoch'
MAX_SESSION_EPOCH = (1 << 63) - 1
POLICY_PERMISSIONS = ('auth.change_user', 'judge.manage_login_password_rotation')


class LoginPolicyConflict(ValidationError):
    def __init__(self):
        super().__init__(_('The login policy has changed. Reload before saving.'), code='stale_login_policy')


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


@transaction.atomic
def set_login_policy(actor, user_id, enabled, expected_epoch, reason):
    """Change policy and revoke old sessions/tokens without changing the password."""
    if not getattr(actor, 'is_authenticated', False) or getattr(actor, 'is_impersonate', False):
        raise PermissionDenied()
    if type(user_id) is not int or user_id <= 0:
        raise ValidationError(_('Invalid user ID.'), code='invalid_user_id')
    if type(enabled) is not bool:
        raise ValidationError(_('The login policy must be enabled or disabled.'), code='invalid_policy')
    if type(expected_epoch) is not int or not 0 <= expected_epoch <= MAX_SESSION_EPOCH:
        raise ValidationError(_('Invalid session epoch.'), code='invalid_epoch')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 255:
        raise ValidationError(_('A reason between 1 and 255 characters is required.'), code='invalid_reason')

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
