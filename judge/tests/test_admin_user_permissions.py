from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from reversion import revisions
from reversion.models import Version

from judge.models import Profile
from judge.tests.test_managed_login import ManagedAccountData


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', AUTH_PASSWORD_VALIDATORS=[])
class AdminUserPermissionTests(ManagedAccountData, TestCase):
    def setUp(self):
        self.actor = self.create_actor()
        self.user = self.create_user('contestant')
        self.client.force_login(self.actor)

    def grant(self, *permissions):
        for name in permissions:
            app_label, codename = name.split('.', 1)
            self.actor.user_permissions.add(Permission.objects.get(
                content_type__app_label=app_label, codename=codename,
            ))

    def data(self, user=None, **overrides):
        user = user or self.user
        data = {
            'username': user.username, 'first_name': 'Updated', 'last_name': '', 'email': '',
            'is_active': 'on', 'date_joined_0': '2026-10-24', 'date_joined_1': '10:00:00', '_save': 'Save',
        }
        if not user.is_staff and not user.is_superuser:
            data['login_policy_epoch'] = user.profile.account_session_epoch
        return {**data, **overrides}

    def url(self, user=None, action='change'):
        return reverse('admin:auth_user_' + action, args=[(user or self.user).pk])

    def test_non_superuser_permission_fields_are_readonly(self):
        for target in (self.actor, self.user):
            response = self.client.get(self.url(target))
            self.assertEqual(response.status_code, 200)
            for name in ('is_staff', 'is_superuser', 'groups', 'user_permissions'):
                self.assertNotContains(response, 'name="%s"' % name)
            self.assertContains(response, 'name="first_name"')

    def test_forged_self_promotion_and_permission_assignment_are_rejected(self):
        permission = Permission.objects.get(codename='delete_user', content_type__app_label='auth')
        group = Group.objects.create(name='Powerful group')
        group.permissions.add(permission)
        for field, value in (('is_superuser', 'on'), ('is_staff', ''),
                             ('user_permissions', [permission.pk]), ('groups', [group.pk])):
            with self.subTest(field=field):
                response = self.client.post(self.url(self.actor), self.data(self.actor, **{field: value}))
                self.assertEqual(response.status_code, 403)
                actor = User.objects.get(pk=self.actor.pk)
                self.assertFalse(actor.is_superuser)
                self.assertTrue(actor.is_staff)
                self.assertFalse(actor.has_perm('auth.delete_user'))

    def test_forged_privileged_fields_on_another_user_are_rejected(self):
        for field in ('is_superuser', 'is_staff', 'groups', 'user_permissions'):
            with self.subTest(field=field):
                self.assertEqual(self.client.post(self.url(), self.data(**{field: '1'})).status_code, 403)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertEqual(self.user.first_name, '')

    def test_basic_edits_work_without_erasing_existing_membership(self):
        group = Group.objects.create(name='Invigilators')
        self.actor.groups.add(group)
        self.assertEqual(self.client.post(self.url(self.actor), self.data(self.actor)).status_code, 302)
        self.actor.refresh_from_db()
        self.assertEqual(self.actor.first_name, 'Updated')
        self.assertTrue(self.actor.groups.filter(pk=group.pk).exists())
        self.assertTrue(self.actor.user_permissions.filter(codename='change_user').exists())
        self.assertEqual(self.client.post(self.url(), self.data()).status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Updated')

    def test_other_staff_and_superuser_are_readonly_and_password_reset_is_denied(self):
        targets = [self.create_user('other_staff', is_staff=True),
                   self.create_user('root_admin', is_staff=True, is_superuser=True)]
        for target in targets:
            with self.subTest(target=target.username):
                response = self.client.get(self.url(target))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'name="first_name"')
                self.assertEqual(self.client.post(self.url(target), self.data(target)).status_code, 403)
                password_url = reverse('admin:auth_user_password_change', args=[target.pk])
                self.assertEqual(self.client.get(password_url).status_code, 403)
                response = self.client.post(password_url, {'password1': 'New-password-2026',
                                                           'password2': 'New-password-2026'})
                self.assertEqual(response.status_code, 403)
                target.refresh_from_db()
                self.assertTrue(target.check_password('Initial-password-2026'))

    def test_non_staff_with_higher_permissions_is_protected_including_rotation_api(self):
        permission = Permission.objects.get(codename='delete_user', content_type__app_label='auth')
        self.user.user_permissions.add(permission)
        self.assertEqual(self.client.post(self.url(), self.data()).status_code, 403)
        password_url = reverse('admin:auth_user_password_change', args=[self.user.pk])
        self.assertEqual(self.client.post(password_url, {
            'password1': 'New-password-2026', 'password2': 'New-password-2026',
        }).status_code, 403)
        url = reverse('admin:auth_user_login_policy', args=[self.user.pk])
        response = self.client.patch(url, '{"enabled":true,"expected_epoch":0}', content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'permission_denied')

    def test_group_permissions_also_count_when_protecting_a_target(self):
        group = Group.objects.create(name='Privileged users')
        group.permissions.add(Permission.objects.get(codename='delete_user', content_type__app_label='auth'))
        self.user.groups.add(group)
        self.assertEqual(self.client.post(self.url(), self.data()).status_code, 403)
        self.grant('auth.delete_user')
        self.assertEqual(self.client.post(self.url(), self.data()).status_code, 302)

    def test_inactive_higher_privilege_account_cannot_be_reset_then_reactivated(self):
        permission = Permission.objects.get(codename='delete_user', content_type__app_label='auth')
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        password_url = reverse('admin:auth_user_password_change', args=[self.user.pk])
        for source in ('direct', 'group'):
            with self.subTest(source=source):
                self.user.user_permissions.clear()
                if source == 'direct':
                    self.user.user_permissions.add(permission)
                else:
                    group = Group.objects.create(name='Inactive privileged group')
                    group.permissions.add(permission)
                    self.user.groups.add(group)
                self.assertEqual(self.client.post(password_url, {
                    'password1': 'New-password-2026', 'password2': 'New-password-2026',
                }).status_code, 403)
                self.assertEqual(self.client.post(self.url(), self.data()).status_code, 403)
                self.user.refresh_from_db()
                self.assertFalse(self.user.is_active)
                self.assertTrue(self.user.check_password('Initial-password-2026'))

    def test_user_creation_cannot_smuggle_privileged_fields(self):
        self.grant('auth.add_user')
        url = reverse('admin:auth_user_add')
        data = {'username': 'new_contestant', 'password1': 'New-password-2026', 'password2': 'New-password-2026'}
        for field in ('is_staff', 'is_superuser', 'groups', 'user_permissions'):
            self.assertEqual(self.client.post(url, {**data, field: '1'}).status_code, 403)
            self.assertFalse(User.objects.filter(username='new_contestant').exists())
        self.assertEqual(self.client.post(url, data).status_code, 302)
        user = User.objects.get(username='new_contestant')
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(Profile.objects.filter(user=user).exists())

    def test_delete_and_bulk_delete_cannot_remove_protected_accounts(self):
        self.grant('auth.delete_user', 'judge.delete_profile')
        root = self.create_user('root_admin', is_staff=True, is_superuser=True)
        self.assertEqual(self.client.post(self.url(root, 'delete'), {'post': 'yes'}).status_code, 403)
        request = RequestFactory().post('/')
        request.user = User.objects.get(pk=self.actor.pk)
        request.session = {}
        queryset = User.objects.filter(pk__in=[self.user.pk, root.pk])
        with self.assertRaises(PermissionDenied):
            admin.site._registry[User].delete_queryset(request, queryset)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(User.objects.filter(pk=root.pk).exists())

    def test_profile_cannot_bypass_protected_user_or_expose_totp_key(self):
        self.grant('judge.view_profile', 'judge.change_profile', 'judge.delete_profile', 'judge.totp')
        root = self.create_user('root_admin', is_staff=True, is_superuser=True)
        Profile.objects.filter(user=root).update(is_totp_enabled=True, totp_key='JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP')
        profile = Profile.objects.get(user=root)
        url = reverse('admin:judge_profile_change', args=[profile.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {'is_totp_enabled': '', 'ip_auth': '127.0.0.1'}).status_code, 403)
        url = reverse('admin:judge_profile_delete', args=[profile.pk])
        self.assertEqual(self.client.post(url, {'post': 'yes'}).status_code, 403)
        profile.refresh_from_db()
        self.assertTrue(profile.is_totp_enabled)
        self.assertIsNone(profile.ip_auth)

    def test_profile_revision_restore_requires_superuser(self):
        self.grant('judge.change_profile')
        with revisions.create_revision():
            self.user.profile.save()
        version = Version.objects.get_for_object(self.user.profile).first()
        url = reverse('admin:judge_profile_revision', args=[self.user.profile.pk, version.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {}).status_code, 403)

    def test_permission_revocation_is_rechecked_inside_write_lock(self):
        self.actor.is_superuser = True
        self.actor.save(update_fields=['is_superuser'])
        from judge.admin.managed_login import locked_managed_account

        def revoke_then_lock(*args, **kwargs):
            User.objects.filter(pk=self.actor.pk).update(is_superuser=False)
            return locked_managed_account(*args, **kwargs)

        with patch('judge.admin.managed_login.locked_managed_account', side_effect=revoke_then_lock):
            response = self.client.post(self.url(), self.data(is_superuser='on'))
        self.assertEqual(response.status_code, 403)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)

    def test_superuser_keeps_privilege_management(self):
        self.actor.is_superuser = True
        self.actor.save(update_fields=['is_superuser'])
        group = Group.objects.create(name='Staff group')
        permission = Permission.objects.get(codename='delete_user', content_type__app_label='auth')
        response = self.client.post(self.url(), self.data(
            is_staff='on', is_superuser='on', groups=[group.pk], user_permissions=[permission.pk],
        ))
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertTrue(self.user.is_superuser)
        self.assertTrue(self.user.groups.filter(pk=group.pk).exists())
        self.assertTrue(self.user.user_permissions.filter(pk=permission.pk).exists())


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en')
class GroupPermissionDelegationTests(ManagedAccountData, TestCase):
    def setUp(self):
        self.actor = self.create_actor()
        self.actor.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='auth', codename__in=['add_group', 'change_group', 'delete_group'],
        ))
        self.group = Group.objects.create(name='Invigilators')
        self.actor.groups.add(self.group)
        self.client.force_login(self.actor)
        self.url = reverse('admin:auth_group_change', args=[self.group.pk])
        self.held = Permission.objects.get(codename='change_user', content_type__app_label='auth')
        self.unheld = Permission.objects.get(codename='delete_user', content_type__app_label='auth')

    def test_group_editor_cannot_grant_unheld_permissions_to_own_group(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        choices = response.context['adminform'].form.fields['permissions'].queryset
        self.assertTrue(choices.filter(pk=self.held.pk).exists())
        self.assertFalse(choices.filter(pk=self.unheld.pk).exists())
        response = self.client.post(self.url, {'name': self.group.name, 'permissions': [self.unheld.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('permissions', response.context['adminform'].form.errors)
        self.assertFalse(self.group.permissions.exists())
        self.assertFalse(User.objects.get(pk=self.actor.pk).has_perm('auth.delete_user'))

    def test_group_editor_can_delegate_held_permissions(self):
        response = self.client.post(self.url, {'name': 'Renamed', 'permissions': [self.held.pk]})
        self.assertEqual(response.status_code, 302)
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, 'Renamed')
        self.assertTrue(self.group.permissions.filter(pk=self.held.pk).exists())

    def test_group_creation_rejects_unheld_permission(self):
        url = reverse('admin:auth_group_add')
        response = self.client.post(url, {'name': 'Forged group', 'permissions': [self.unheld.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Group.objects.filter(name='Forged group').exists())
        response = self.client.post(url, {'name': 'Allowed group', 'permissions': [self.held.pk]})
        self.assertEqual(response.status_code, 302)

    def test_more_privileged_group_cannot_be_changed_or_deleted(self):
        group = Group.objects.create(name='Higher privileges')
        group.permissions.add(self.unheld)
        url = reverse('admin:auth_group_change', args=[group.pk])
        self.assertEqual(self.client.post(url, {'name': 'Stolen', 'permissions': []}).status_code, 403)
        url = reverse('admin:auth_group_delete', args=[group.pk])
        self.assertEqual(self.client.post(url, {'post': 'yes'}).status_code, 403)
        self.assertTrue(group.permissions.filter(pk=self.unheld.pk).exists())

    def test_superuser_can_grant_any_group_permission(self):
        self.actor.is_superuser = True
        self.actor.save(update_fields=['is_superuser'])
        response = self.client.post(self.url, {'name': self.group.name, 'permissions': [self.unheld.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.group.permissions.filter(pk=self.unheld.pk).exists())
