from django.contrib.auth.admin import GroupAdmin as OldGroupAdmin
from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404

from judge.utils.user_permissions import admin_actor, can_administer_group


class GroupAdmin(OldGroupAdmin):
    """Group editors may delegate only permissions they already hold."""

    def has_change_permission(self, request, obj=None):
        actor = admin_actor(request)
        return actor.has_perm('auth.change_group') and (obj is None or can_administer_group(actor, obj))

    def has_delete_permission(self, request, obj=None):
        actor = admin_actor(request)
        return actor.has_perm('auth.delete_group') and (obj is None or can_administer_group(actor, obj))

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        actor = admin_actor(request)
        if db_field.name == 'permissions' and not actor.is_superuser:
            held = actor.get_all_permissions()
            ids = [pk for pk, app_label, codename in Permission.objects.values_list(
                'pk', 'content_type__app_label', 'codename',
            ) if '%s.%s' % (app_label, codename) in held]
            kwargs['queryset'] = Permission.objects.filter(pk__in=ids)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method != 'POST':
            return super().changeform_view(request, object_id, form_url, extra_context)
        with transaction.atomic():
            actor = User.objects.select_for_update().get(pk=request.user.pk)
            request._account_admin_actor = actor
            if (not actor.is_active or not actor.is_staff or '_impersonate' in request.session or
                    not actor.has_perm('auth.add_group' if object_id is None else 'auth.change_group')):
                raise PermissionDenied()
            if object_id is not None:
                try:
                    group = Group.objects.select_for_update().get(pk=object_id)
                except (ValueError, Group.DoesNotExist):
                    raise Http404() from None
                if not can_administer_group(actor, group):
                    raise PermissionDenied()
            return super().changeform_view(request, object_id, form_url, extra_context)

    def delete_queryset(self, request, queryset):
        with transaction.atomic():
            actor = User.objects.select_for_update().get(pk=request.user.pk)
            if not actor.has_perm('auth.delete_group') or '_impersonate' in request.session:
                raise PermissionDenied()
            groups = list(Group.objects.select_for_update().filter(pk__in=queryset.values('pk')).order_by('pk'))
            if any(not can_administer_group(actor, group) for group in groups):
                raise PermissionDenied()
            super().delete_queryset(request, queryset)

    def delete_model(self, request, obj):
        self.delete_queryset(request, Group.objects.filter(pk=obj.pk))
