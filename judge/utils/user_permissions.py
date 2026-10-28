"""Account administration limits, in addition to Django's model permissions."""

from itertools import chain

from django.core.exceptions import PermissionDenied

PRIVILEGED_USER_FIELDS = ('is_staff', 'is_superuser', 'groups', 'user_permissions')


def admin_actor(request):
    return getattr(request, '_account_admin_actor', request.user)


def can_administer_user(actor, target):
    if not actor.is_authenticated or not actor.is_active or not actor.is_staff:
        return False
    if getattr(actor, 'is_impersonate', False):
        return False
    if actor.is_superuser:
        return True
    if target.is_superuser or (target.is_staff and target.pk != actor.pk):
        return False
    # Count assigned rights even while inactive: get_all_permissions() would
    # return an empty set, allowing a password reset followed by reactivation.
    direct = target.user_permissions.values_list('content_type__app_label', 'codename')
    groups = target.groups.values_list('permissions__content_type__app_label', 'permissions__codename')
    assigned = {'%s.%s' % (app_label, codename) for app_label, codename in chain(direct, groups) if codename}
    return assigned <= actor.get_all_permissions()


def check_user_admin_write(request, actor, target=None):
    if getattr(request.user, 'is_impersonate', False) or '_impersonate' in request.session:
        raise PermissionDenied()
    if target is not None and not can_administer_user(actor, target):
        raise PermissionDenied()
    if not actor.is_superuser and any(field in request.POST for field in PRIVILEGED_USER_FIELDS):
        raise PermissionDenied()


def can_administer_group(actor, group):
    if not actor.is_authenticated or not actor.is_active or not actor.is_staff:
        return False
    if actor.is_superuser:
        return True
    permissions = {'%s.%s' % (app_label, codename) for app_label, codename in
                   group.permissions.values_list('content_type__app_label', 'codename')}
    return permissions <= actor.get_all_permissions()
