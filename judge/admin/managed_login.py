from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import UserChangeForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from judge.utils.managed_login import LoginPolicyConflict, MAX_SESSION_EPOCH, can_manage_login_policy, \
    locked_managed_account, on_admin_password_changed, set_login_policy
from judge.utils.user_permissions import admin_actor, can_administer_user, check_user_admin_write
from judge.views.login_policy import login_policy

POLICY_FIELDS = ('rotate_password_on_login', 'login_policy_epoch')


class LoginPolicyUserForm(UserChangeForm):
    rotate_password_on_login = forms.BooleanField(
        required=False, label=_('Rotate password after login'),
        help_text=_('The issued password works until the next completed login. The contestant keeps that session; '
                    'if it is lost, an invigilator must set a new password.'),
    )
    login_policy_epoch = forms.IntegerField(min_value=0, max_value=MAX_SESSION_EPOCH, widget=forms.HiddenInput)
    policy_controls = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.policy_controls:
            profile = self.instance.profile
            self.initial['rotate_password_on_login'] = profile.rotate_password_on_login
            self.initial['login_policy_epoch'] = profile.account_session_epoch
        else:
            for field in POLICY_FIELDS:
                self.fields.pop(field, None)

    def clean(self):
        data = super().clean()
        if not self.policy_controls and any(field in self.data for field in POLICY_FIELDS):
            raise PermissionDenied()
        profile = self.instance.profile
        enabled = data.get('rotate_password_on_login', profile.rotate_password_on_login)
        if self.policy_controls and data.get('login_policy_epoch') != profile.account_session_epoch:
            raise LoginPolicyConflict()
        if enabled and (data.get('is_staff', self.instance.is_staff) or
                        data.get('is_superuser', self.instance.is_superuser)):
            raise ValidationError(_('Disable login password rotation before promoting this account.'))
        if enabled != profile.rotate_password_on_login:
            if enabled and not data.get('is_active', self.instance.is_active):
                raise ValidationError(_('The account is disabled or banned.'))
        return data


class LoginPolicyFilter(admin.SimpleListFilter):
    title = _('Rotate password after login')
    parameter_name = 'login_policy'

    def lookups(self, request, model_admin):
        return [('on', _('Enabled')), ('off', _('Disabled'))]

    def queryset(self, request, queryset):
        if self.value() in ('on', 'off'):
            return queryset.filter(profile__rotate_password_on_login=self.value() == 'on')
        return queryset


class ManagedLoginUserAdminMixin:
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('profile')

    def policy_controls(self, request, obj):
        return (obj is not None and obj.is_active and not obj.is_staff and not obj.is_superuser and
                '_impersonate' not in request.session and
                can_manage_login_policy(admin_actor(request)) and can_administer_user(admin_actor(request), obj))

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None:
            return fieldsets
        fields = (*POLICY_FIELDS, 'last_password_rotation') if self.policy_controls(request, obj) else (
            'login_policy_enabled', 'last_password_rotation',
        )
        return (*fieldsets, (_('Contest account'), {'fields': fields}))

    def get_readonly_fields(self, request, obj=None):
        return (*super().get_readonly_fields(request, obj), 'login_policy_enabled', 'last_password_rotation')

    def get_form(self, request, obj=None, **kwargs):
        if obj is not None:
            class RequestUserForm(LoginPolicyUserForm):
                policy_controls = self.policy_controls(request, obj)

            kwargs['form'] = RequestUserForm
        return super().get_form(request, obj, **kwargs)

    @admin.display(boolean=True, description=_('Rotate password after login'),
                   ordering='profile__rotate_password_on_login')
    def login_policy_enabled(self, obj):
        return obj.profile.rotate_password_on_login

    @admin.display(description=_('Last automatic password rotation'))
    def last_password_rotation(self, obj):
        return obj.profile.password_rotated_at

    def get_urls(self):
        # The API handles authentication itself to keep errors JSON instead of admin redirects.
        return [path('<int:user_id>/login-policy/', login_policy, name='auth_user_login_policy')] + super().get_urls()

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method != 'POST':
            return super().changeform_view(request, object_id, form_url, extra_context)
        if getattr(request.user, 'is_impersonate', False) or '_impersonate' in request.session:
            raise PermissionDenied()
        if object_id is None:
            if any(field in request.POST for field in POLICY_FIELDS):
                raise PermissionDenied()
            return super().changeform_view(request, object_id, form_url, extra_context)
        target = self.get_object(request, unquote(object_id))
        if target is None:
            raise Http404()
        try:
            with locked_managed_account(request.user, target.pk, ('auth.change_user',)) as (actor, target, profile):
                request._account_admin_actor = actor
                check_user_admin_write(request, actor, target)
                # Validate the form and save all User/policy changes while the same locks are held.
                return super().changeform_view(request, object_id, form_url, extra_context)
        except DatabaseError:
            return HttpResponse(gettext('Unable to save account changes right now. Please try again.'), status=503)

    def save_model(self, request, obj, form, change):
        if not change:
            super().save_model(request, obj, form, change)
            return
        if form.policy_controls:
            enabled = form.cleaned_data['rotate_password_on_login']
            if enabled != obj.profile.rotate_password_on_login:
                set_login_policy(request.user, obj.pk, enabled, form.cleaned_data['login_policy_epoch'])
                if not enabled:
                    self.message_user(request, format_html(
                        gettext('Automatic rotation is disabled. The old password is not restored. '
                                '<a href="{}">Set a new password</a> if needed.'),
                        reverse('admin:auth_user_password_change', args=[obj.pk]),
                    ), messages.WARNING)
        # UserChangeForm includes a read-only password hash. Do not write that cached hash back.
        fields = [field.name for field in obj._meta.concrete_fields
                  if not field.primary_key and field.name != 'password' and field.name in form.changed_data]
        obj.save(update_fields=fields)

    @method_decorator(sensitive_post_parameters())
    @sensitive_variables()
    def user_change_password(self, request, id, form_url=''):
        if getattr(request.user, 'is_impersonate', False) or '_impersonate' in request.session:
            raise PermissionDenied()
        user = self.get_object(request, unquote(id))
        if user is None:
            raise Http404()
        if not self.has_change_permission(request, user):
            raise PermissionDenied()
        if request.method != 'POST':
            return super().user_change_password(request, id, form_url)

        try:
            with locked_managed_account(request.user, user.pk, ('auth.change_user',)) as (actor, user, profile):
                request._account_admin_actor = actor
                if not can_administer_user(actor, user):
                    raise PermissionDenied()
                form = self.change_password_form(user, request.POST)
                if form.is_valid():
                    if profile.rotate_password_on_login and profile.account_session_epoch == MAX_SESSION_EPOCH:
                        form.add_error(None, _('The session epoch cannot be increased.'))
                    else:
                        form.save()
                        on_admin_password_changed(request.user, user)
                        self.log_change(request, user, self.construct_change_message(request, form, None))
                        update_session_auth_hash(request, user)
                        response = HttpResponseRedirect(reverse('admin:auth_user_change', args=[user.pk]))
            if not form.errors:
                self.message_user(request, gettext('Password changed successfully.'), messages.SUCCESS)
                return response
        except DatabaseError:
            return HttpResponse(gettext('Unable to save account changes right now. Please try again.'), status=503)

        # Keep the standard Django password form, validation and template for invalid POSTs.
        admin_form = admin.helpers.AdminForm(form, [(None, {'fields': list(form.base_fields)})], {})
        request.current_app = self.admin_site.name
        return TemplateResponse(request, self.change_user_password_template or 'admin/auth/user/change_password.html', {
            **self.admin_site.each_context(request),
            'title': gettext('Change password: %s') % user.get_username(), 'adminForm': admin_form,
            'form': form, 'form_url': form_url, 'opts': self.opts, 'original': user,
            'is_popup': '_popup' in request.POST or '_popup' in request.GET, 'is_popup_var': '_popup',
            'add': True, 'change': False, 'has_delete_permission': False, 'has_change_permission': True,
            'has_absolute_url': False, 'save_as': False, 'show_save': True,
        })
