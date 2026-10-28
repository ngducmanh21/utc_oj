import json
from threading import Barrier, Thread
from unittest.mock import patch

import pyotp
from django.contrib import auth
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.db import DatabaseError, close_old_connections, connection
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from judge.models import ManagedAccountEvent, Profile
from judge.tests.test_managed_login import ManagedAccountData
from judge.utils.managed_login import MAX_SESSION_EPOCH, set_login_policy


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', AUTH_PASSWORD_VALIDATORS=[])
class ManagedLoginAdminTests(ManagedAccountData, TestCase):
    def setUp(self):
        self.actor = self.create_actor()
        self.user = self.create_user('contestant')
        self.client.force_login(self.actor)
        self.url = reverse('admin:auth_user_change', args=[self.user.pk])
        self.password_url = reverse('admin:auth_user_password_change', args=[self.user.pk])

    def data(self, **overrides):
        return {
            'username': self.user.username, 'first_name': '', 'last_name': '', 'email': '',
            'is_active': 'on', 'date_joined_0': '2026-10-24', 'date_joined_1': '10:00:00',
            'login_policy_epoch': Profile.objects.get(user=self.user).account_session_epoch,
            '_save': 'Save', **overrides,
        }

    def password_post(self, client=None):
        return (client or self.client).post(self.password_url, {
            'password1': 'Reissued-password-2026', 'password2': 'Reissued-password-2026',
        })

    def test_change_form_shows_controls_and_lists_filter_by_policy(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="rotate_password_on_login"')
        self.assertContains(response, 'name="login_policy_epoch"')
        self.assertNotContains(response, 'name="login_policy_reason"')
        self.assertContains(response, 'Last automatic password rotation')
        set_login_policy(self.actor, self.user.pk, True, 0, 'Exam')
        url = reverse('admin:auth_user_changelist')
        response = self.client.get(url, {'login_policy': 'on'})
        self.assertEqual([user.pk for user in response.context['cl'].result_list], [self.user.pk])
        response = self.client.get(url, {'login_policy': 'off'})
        self.assertNotIn(self.user.pk, [user.pk for user in response.context['cl'].result_list])

    def test_form_enable_disable_uses_service_and_does_not_change_password(self):
        password_hash = self.user.password
        self.user.profile.generate_api_token()
        response = self.client.post(self.url, self.data(rotate_password_on_login='on'))
        self.assertEqual(response.status_code, 302)
        profile = Profile.objects.get(user=self.user)
        self.assertTrue(profile.rotate_password_on_login)
        self.assertEqual(profile.account_session_epoch, 1)
        self.assertIsNone(profile.api_token)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, password_hash)
        response = self.client.post(self.url, self.data(), follow=True)
        self.assertContains(response, 'The old password is not restored.')
        self.assertContains(response, self.password_url)
        profile.refresh_from_db()
        self.assertFalse(profile.rotate_password_on_login)
        self.assertEqual(profile.account_session_epoch, 2)
        self.assertEqual(ManagedAccountEvent.objects.count(), 2)

    def test_toggle_without_reason_saves_and_keeps_audit(self):
        response = self.client.post(self.url, self.data(
            rotate_password_on_login='on', first_name='Saved name',
        ))
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Saved name')
        event = ManagedAccountEvent.objects.get()
        self.assertEqual(event.reason, '')
        self.assertEqual(event.actor_id, self.actor.pk)
        self.assertTrue(self.user.profile.rotate_password_on_login)

    def test_stale_admin_form_cannot_overwrite_new_policy(self):
        data = self.data(first_name='Stale name')
        set_login_policy(self.actor, self.user.pk, True, 0, 'Exam')
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Reload before saving')
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, '')
        self.assertTrue(self.user.profile.rotate_password_on_login)
        self.assertEqual(self.user.profile.account_session_epoch, 1)

    def test_missing_toggle_permission_hides_control_and_rejects_forged_post(self):
        self.actor.user_permissions.remove(Permission.objects.get(codename='manage_login_password_rotation'))
        response = self.client.get(self.url)
        self.assertNotContains(response, 'name="rotate_password_on_login"')
        self.assertEqual(self.client.post(self.url, self.data(rotate_password_on_login='on')).status_code, 403)
        self.assertFalse(ManagedAccountEvent.objects.exists())
        data = self.data(first_name='Allowed edit')
        data.pop('login_policy_epoch')
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Allowed edit')

    def test_staff_or_superuser_cannot_enable_policy(self):
        for field in ('is_staff', 'is_superuser'):
            with self.subTest(field=field):
                User.objects.filter(pk=self.user.pk).update(**{field: True})
                self.assertNotContains(self.client.get(self.url), 'name="rotate_password_on_login"')
                self.assertEqual(self.client.post(self.url, self.data(rotate_password_on_login='on')).status_code, 403)
                User.objects.filter(pk=self.user.pk).update(**{field: False})

    def test_cannot_enable_policy_and_promote_or_disable_user_together(self):
        self.actor.is_superuser = True
        self.actor.save(update_fields=['is_superuser'])
        for field, value in (('is_staff', 'on'), ('is_superuser', 'on'), ('is_active', '')):
            with self.subTest(field=field):
                response = self.client.post(self.url, self.data(rotate_password_on_login='on', **{field: value}))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['adminform'].form.errors)
                self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_on_account_must_be_disabled_before_promotion(self):
        self.actor.is_superuser = True
        self.actor.save(update_fields=['is_superuser'])
        set_login_policy(self.actor, self.user.pk, True, 0, 'Exam')
        response = self.client.post(self.url, self.data(rotate_password_on_login='on', is_staff='on'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Disable login password rotation before promoting')
        response = self.client.post(self.url, self.data(is_staff='on'))
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertFalse(self.user.profile.rotate_password_on_login)

    def test_regular_edit_does_not_rewrite_policy_or_password(self):
        set_login_policy(self.actor, self.user.pk, True, 0, 'Exam')
        password_hash = self.user.password
        response = self.client.post(self.url, self.data(
            rotate_password_on_login='on', first_name='New name',
        ))
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'New name')
        self.assertEqual(self.user.password, password_hash)
        self.assertEqual(self.user.profile.account_session_epoch, 1)
        self.assertEqual(ManagedAccountEvent.objects.count(), 1)

    def test_policy_audit_failure_rolls_back_user_and_profile_changes(self):
        with patch.object(ManagedAccountEvent.objects, 'create', side_effect=DatabaseError('Test failure')):
            response = self.client.post(self.url, self.data(rotate_password_on_login='on', first_name='Lost edit'))
        self.assertEqual(response.status_code, 503)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, '')
        self.assertFalse(self.user.profile.rotate_password_on_login)

    def test_admin_password_change_revokes_sessions_and_tokens_without_toggle_permission(self):
        set_login_policy(self.actor, self.user.pk, True, 0, 'Exam')
        contestant = Client()
        with patch('judge.views.user.PwnedPasswordsValidator.validate'):
            contestant.post(reverse('auth_login'), {
                'username': self.user.username, 'password': 'Initial-password-2026',
            })
        self.assertEqual(contestant.get(reverse('user_edit_profile')).status_code, 200)
        Profile.objects.filter(user=self.user).update(api_token='a' * 64)
        self.actor.user_permissions.remove(Permission.objects.get(codename='manage_login_password_rotation'))
        self.assertEqual(self.password_post().status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Reissued-password-2026'))
        profile = self.user.profile
        self.assertTrue(profile.rotate_password_on_login)
        self.assertEqual(profile.account_session_epoch, 3)
        self.assertIsNone(profile.api_token)
        self.assertIsNotNone(profile.password_rotated_at)
        contestant.get(reverse('user_edit_profile'))
        self.assertNotIn(auth.SESSION_KEY, contestant.session)
        event = ManagedAccountEvent.objects.get(event=ManagedAccountEvent.Event.CREDENTIAL_REISSUED)
        self.assertEqual(event.actor_id, self.actor.pk)
        for data in (str(ManagedAccountEvent.objects.values().all()), str(LogEntry.objects.values().all())):
            self.assertNotIn('Reissued-password-2026', data)
        with patch('judge.views.user.PwnedPasswordsValidator.validate'):
            response = contestant.post(reverse('auth_login'), {
                'username': self.user.username, 'password': 'Reissued-password-2026',
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(contestant.get(reverse('user_edit_profile')).status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password('Reissued-password-2026'))

    def test_password_change_off_uses_standard_behavior(self):
        self.assertEqual(self.client.get(self.password_url).status_code, 200)
        self.assertEqual(self.password_post().status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Reissued-password-2026'))
        self.assertEqual(self.user.profile.account_session_epoch, 0)
        self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_invalid_password_form_or_epoch_exhaustion_does_not_save(self):
        set_login_policy(self.actor, self.user.pk, True, 0, 'Exam')
        response = self.client.post(self.password_url, {'password1': 'one', 'password2': 'two'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('password2', response.context['form'].errors)
        Profile.objects.filter(user=self.user).update(account_session_epoch=MAX_SESSION_EPOCH)
        response = self.password_post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].non_field_errors())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Initial-password-2026'))

    def test_password_audit_failure_rolls_back_everything(self):
        set_login_policy(self.actor, self.user.pk, True, 0, 'Exam')
        Profile.objects.filter(user=self.user).update(api_token='a' * 64)
        with patch.object(ManagedAccountEvent.objects, 'create', side_effect=DatabaseError('Test failure')):
            self.assertEqual(self.password_post().status_code, 503)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Initial-password-2026'))
        self.assertEqual(self.user.profile.account_session_epoch, 1)
        self.assertEqual(self.user.profile.api_token, 'a' * 64)
        self.assertFalse(LogEntry.objects.exists())

    def test_password_change_requires_change_user_permission_and_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.actor)
        response = self.password_post(csrf_client)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.password_url)
        self.actor.user_permissions.remove(Permission.objects.get(
            codename='change_user', content_type__app_label='auth',
        ))
        self.assertEqual(self.password_post().status_code, 403)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Initial-password-2026'))

    def test_impersonation_cannot_change_policy_or_password(self):
        session = self.client.session
        session['_impersonate'] = self.user.pk
        session.save()
        self.assertEqual(self.client.post(self.url, self.data(rotate_password_on_login='on')).status_code, 403)
        self.assertEqual(self.password_post().status_code, 403)
        self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_toggle_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        response = client.post(self.url, self.data(rotate_password_on_login='on'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.url)
        self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_view_only_admin_has_no_policy_controls(self):
        self.actor.user_permissions.clear()
        self.actor.user_permissions.add(Permission.objects.get(codename='view_user', content_type__app_label='auth'))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="rotate_password_on_login"')
        self.assertEqual(self.client.post(self.url, self.data()).status_code, 403)

    def test_own_admin_password_change_keeps_admin_session(self):
        self.password_url = reverse('admin:auth_user_password_change', args=[self.actor.pk])
        self.assertEqual(self.password_post().status_code, 302)
        self.actor.refresh_from_db()
        self.assertTrue(self.actor.check_password('Reissued-password-2026'))
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_admin_reset_invalidates_pending_two_factor_and_new_password_still_needs_factor(self):
        set_login_policy(self.actor, self.user.pk, True, 0, 'Exam')
        key = pyotp.random_base32()
        Profile.objects.filter(user=self.user).update(is_totp_enabled=True, totp_key=key)
        contestant = Client()
        with patch('judge.views.user.PwnedPasswordsValidator.validate'):
            contestant.post(reverse('auth_login'), {
                'username': self.user.username, 'password': 'Initial-password-2026',
            })
        self.assertIn('managed_login_pending', contestant.session)
        self.assertEqual(self.password_post().status_code, 302)
        response = contestant.post(reverse('login_2fa'), {'totp_or_scratch_code': pyotp.TOTP(key).now()})
        self.assertTrue(response.url.startswith(reverse('auth_login')))
        self.assertNotIn(auth.SESSION_KEY, contestant.session)
        with patch('judge.views.user.PwnedPasswordsValidator.validate'):
            contestant.post(reverse('auth_login'), {
                'username': self.user.username, 'password': 'Reissued-password-2026',
            })
        self.assertIn('managed_login_pending', contestant.session)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Reissued-password-2026'))
        response = contestant.post(reverse('login_2fa'), {'totp_or_scratch_code': pyotp.TOTP(key).now()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(contestant.get(reverse('user_edit_profile')).status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password('Reissued-password-2026'))


@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en')
class LoginPolicyAPITests(ManagedAccountData, TestCase):
    def setUp(self):
        self.actor = self.create_actor()
        self.user = self.create_user('contestant')
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.actor)
        self.csrf = get_token(RequestFactory().get('/'))
        self.client.cookies['csrftoken'] = self.csrf
        self.url = reverse('admin:auth_user_login_policy', args=[self.user.pk])

    def change(self, data=None, **extra):
        return self.client.patch(self.url, json.dumps(data if data is not None else {
            'enabled': True, 'expected_epoch': 0,
        }), content_type='application/json', HTTP_X_CSRFTOKEN=self.csrf, **extra)

    def test_get_and_patch_contract(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'user_id': self.user.pk, 'enabled': False, 'epoch': 0, 'rotated_at': None})
        self.assertIn('no-store', response['Cache-Control'])
        response = self.change()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['enabled'])
        self.assertEqual(response.json()['epoch'], 1)
        self.assertEqual(ManagedAccountEvent.objects.count(), 1)

    def test_stale_patch_is_409_without_mutation(self):
        self.change()
        response = self.change({'enabled': False, 'expected_epoch': 0, 'reason': 'Old form'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'stale_login_policy')
        self.assertEqual(ManagedAccountEvent.objects.count(), 1)

    def test_csrf_required_and_anonymous_is_json_401(self):
        response = self.client.patch(self.url, '{}', content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'csrf_failed')
        self.client.logout()
        for method in ('get', 'patch'):
            response = getattr(self.client, method)(self.url)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()['error']['code'], 'authentication_required')

    def test_bearer_authentication_is_rejected_with_json(self):
        token = self.actor.profile.generate_api_token()
        for value in ('Bearer ' + token, 'Bearer invalid'):
            response = self.client.get(self.url, HTTP_AUTHORIZATION=value)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()['error']['code'], 'session_required')

    def test_permissions_and_staff_required(self):
        for codename in ('change_user', 'manage_login_password_rotation'):
            permission = Permission.objects.get(codename=codename, content_type__app_label__in=('auth', 'judge'))
            self.actor.user_permissions.remove(permission)
            self.assertEqual(self.change().status_code, 403)
            self.actor.user_permissions.add(permission)
        User.objects.filter(pk=self.actor.pk).update(is_staff=False)
        self.assertEqual(self.change().status_code, 403)

    def test_pending_admin_two_factor_cannot_access_api(self):
        Profile.objects.filter(user=self.actor).update(is_totp_enabled=True)
        response = self.change()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'two_factor_required')
        session = self.client.session
        session['2fa_passed'] = True
        session.save()
        self.assertEqual(self.change().status_code, 200)

    def test_stale_epoch_returns_json_401(self):
        Profile.objects.filter(user=self.actor).update(account_session_epoch=1)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'session_expired')
        self.assertNotIn(auth.SESSION_KEY, self.client.session)

    def test_invalid_payloads_are_422(self):
        for changes in ({'enabled': 1}, {'expected_epoch': True}, {'expected_epoch': -1}, {'reason': None},
                        {'reason': 'x' * 256}, {'password': 'Never accept passwords here'}):
            response = self.change({'enabled': True, 'expected_epoch': 0, 'reason': 'Exam', **changes})
            self.assertEqual(response.status_code, 422, response.content)
        for value in ('[]', '{', 'null'):
            response = self.client.patch(self.url, value, content_type='application/json', HTTP_X_CSRFTOKEN=self.csrf)
            self.assertEqual(response.status_code, 422)
        self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_optional_reason_can_be_omitted_empty_or_supplied(self):
        for epoch, reason in enumerate((None, '', 'Optional note')):
            with self.subTest(reason=reason):
                data = {'enabled': epoch % 2 == 0, 'expected_epoch': epoch}
                if reason is not None:
                    data['reason'] = reason
                response = self.change(data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(ManagedAccountEvent.objects.first().reason, reason or '')

    def test_target_missing_protected_and_method_errors(self):
        response = self.client.get(reverse('admin:auth_user_login_policy', args=[99999999]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error']['code'], 'not_found')
        User.objects.filter(pk=self.user.pk).update(is_superuser=True)
        self.assertEqual(self.change().status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        response = self.client.post(self.url, HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response['Allow'], 'GET, PATCH')

    def test_database_error_returns_json_503(self):
        with patch('judge.views.login_policy.set_login_policy', side_effect=DatabaseError('Secret database details')):
            response = self.change()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'unavailable')
        self.assertNotIn('Secret database details', response.content.decode())

    def test_impersonation_is_json_403(self):
        session = self.client.session
        session['_impersonate'] = self.user.pk
        session.save()
        response = self.change()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'permission_denied')
        self.assertFalse(ManagedAccountEvent.objects.exists())

    def test_repeated_identical_policy_does_not_add_audit_or_revoke_again(self):
        self.change()
        response = self.change({'enabled': True, 'expected_epoch': 1, 'reason': 'No change'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['epoch'], 1)
        self.assertEqual(ManagedAccountEvent.objects.count(), 1)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17')
class ConcurrentPolicyAPITests(ManagedAccountData, TransactionTestCase):
    def test_concurrent_patches_have_one_winner(self):
        if not connection.features.has_select_for_update:
            self.skipTest('Requires SELECT FOR UPDATE support.')
        actor = self.create_actor()
        user = self.create_user('contestant')
        url = reverse('admin:auth_user_login_policy', args=[user.pk])
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(actor)
        barrier = Barrier(2)
        results = []

        def change(client):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = client.patch(url, json.dumps({'enabled': True, 'expected_epoch': 0, 'reason': 'Exam'}),
                                        content_type='application/json')
                results.append(response.status_code)
            except Exception as error:
                results.append(type(error).__name__)
            finally:
                close_old_connections()

        threads = [Thread(target=change, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, [200, 409])
        self.assertEqual(ManagedAccountEvent.objects.count(), 1)
        self.assertEqual(Profile.objects.get(user=user).account_session_epoch, 1)
