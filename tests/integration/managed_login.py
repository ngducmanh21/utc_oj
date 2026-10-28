"""M5 rollback and SQL backup/restore rehearsal on a disposable MySQL database."""

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.contrib import auth
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import connection, connections
from django.test import Client, TransactionTestCase, override_settings, tag
from django.urls import reverse

from judge.models import ManagedAccountEvent, Profile
from judge.tests.test_managed_login import ManagedAccountData
from judge.tests.test_managed_password_login import PASSWORD
from judge.utils.managed_login import set_login_policy


@tag('managed-login', 'operations')
@override_settings(DEFAULT_USER_LANGUAGE='CPP17', LANGUAGE_CODE='en', AUTH_PASSWORD_VALIDATORS=[])
class ManagedLoginOperationsTests(ManagedAccountData, TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'mysql' or connection.settings_dict['NAME'] != 'test_utcoj_managed_login':
            raise RuntimeError('The destructive SQL rehearsal requires dmoj.managed_login_test_settings.')
        if settings.CACHES['default']['BACKEND'] != 'django.core.cache.backends.locmem.LocMemCache':
            raise RuntimeError('The rehearsal requires an isolated local-memory cache.')
        cache.clear()
        self.actor = self.create_actor()
        self.user = self.create_user('m5_operations')
        self.admin = Client()
        self.admin.force_login(self.actor)
        validator = patch('judge.views.user.PwnedPasswordsValidator.validate')
        validator.start()
        self.addCleanup(validator.stop)

    def login(self, client, user=None, password=PASSWORD):
        return client.post(reverse('auth_login'), {
            'username': (user or self.user).username, 'password': password,
            'next': reverse('user_edit_profile'),
        })

    def reissue_then_disable(self):
        response = self.admin.post(reverse('admin:auth_user_password_change', args=[self.user.pk]), {
            'password1': 'M5-controlled-rollback-2026', 'password2': 'M5-controlled-rollback-2026',
        })
        self.assertEqual(response.status_code, 302)
        profile = Profile.objects.get(user=self.user)
        response = self.admin.patch(reverse('admin:auth_user_login_policy', args=[self.user.pk]), {
            'enabled': False, 'expected_epoch': profile.account_session_epoch,
        }, content_type='application/json')
        self.assertEqual(response.status_code, 200)

    def assert_controlled_recovery(self):
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile.rotate_password_on_login)
        self.assertFalse(self.user.check_password(PASSWORD))
        rotations = ManagedAccountEvent.objects.filter(event='password_rotated').count()
        for _ in range(2):
            normal = Client()
            self.assertEqual(self.login(normal, password='M5-controlled-rollback-2026').status_code, 302)
            self.assertEqual(normal.get(reverse('user_edit_profile')).status_code, 200)
        self.assertEqual(ManagedAccountEvent.objects.filter(event='password_rotated').count(), rotations)

    def test_code_rollback_retains_schema_audit_and_invalidates_old_session_hash(self):
        set_login_policy(self.actor, self.user.pk, True, 0)
        contestant = Client()
        self.assertEqual(self.login(contestant).status_code, 302)
        session_hash = contestant.session[auth.HASH_SESSION_KEY]
        self.reissue_then_disable()
        self.user.refresh_from_db()
        # Old code does not enforce epochs. The changed password hash must also
        # revoke the session through Django's ordinary authentication contract.
        self.assertNotEqual(session_hash, self.user.get_session_auth_hash())
        contestant.get(reverse('user_edit_profile'))
        self.assertNotIn(auth.SESSION_KEY, contestant.session)
        self.assertEqual(list(ManagedAccountEvent.objects.filter(target=self.user).order_by('pk')
                              .values_list('event', flat=True)),
                         ['policy_enabled', 'password_rotated', 'credential_reissued', 'policy_disabled'])
        self.assert_controlled_recovery()

    def test_already_off_account_is_reissued_without_retaining_a_new_api_token(self):
        set_login_policy(self.actor, self.user.pk, True, 0)
        contestant = Client()
        self.assertEqual(self.login(contestant).status_code, 302)
        profile = Profile.objects.get(user=self.user)
        set_login_policy(self.actor, self.user.pk, False, profile.account_session_epoch)
        profile.refresh_from_db()
        profile.generate_api_token()
        self.assertTrue(profile.api_token)
        # Re-enable before reissue so ordinary OFF password change cannot leave
        # an API token that was created after policy was disabled.
        response = self.admin.patch(reverse('admin:auth_user_login_policy', args=[self.user.pk]), {
            'enabled': True, 'expected_epoch': profile.account_session_epoch,
        }, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.reissue_then_disable()
        self.assertIsNone(Profile.objects.get(user=self.user).api_token)
        self.assert_controlled_recovery()

    def test_sql_backup_restore_requires_session_revocation_and_password_reissue(self):
        dump_client = shutil.which('mysqldump') or shutil.which('mariadb-dump')
        sql_client = shutil.which('mysql') or shutil.which('mariadb')
        if not dump_client or not sql_client:
            self.fail('Install the MySQL/MariaDB client tools before running the operations rehearsal.')
        bystander = self.create_user('m5_restore_bystander')
        old_session = Client()
        self.assertEqual(self.login(old_session, bystander).status_code, 302)
        set_login_policy(self.actor, self.user.pk, True, 0)
        snapshot_events = list(ManagedAccountEvent.objects.values_list('pk', flat=True))
        database = connection.settings_dict
        with TemporaryDirectory(prefix='utcoj-m5-backup-') as directory:
            options = Path(directory) / 'client.cnf'

            def quote(value):
                return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'

            options.write_text('[client]\n' + ''.join(
                f'{key}={quote(database.get(setting) or fallback)}\n' for key, setting, fallback in (
                    ('host', 'HOST', 'localhost'), ('port', 'PORT', 3306),
                    ('user', 'USER', ''), ('password', 'PASSWORD', ''),
                )
            ))
            options.chmod(0o600)
            dump = Path(directory) / 'snapshot.sql'
            version = subprocess.run([dump_client, '--version'], capture_output=True, check=True, text=True).stdout
            command = [dump_client, f'--defaults-extra-file={options}', '--single-transaction',
                       '--skip-lock-tables', '--no-tablespaces', '--skip-comments']
            if 'mariadb' not in version.lower():
                command += ['--set-gtid-purged=OFF', '--column-statistics=0']
            with dump.open('wb') as output:
                dump.chmod(0o600)
                result = subprocess.run(command + [database['NAME']], stdout=output, stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(result.returncode, 0, 'SQL dump failed; diagnostics withheld to protect credentials.')
            self.assertGreater(dump.stat().st_size, 0)
            contestant = Client()
            self.assertEqual(self.login(contestant).status_code, 302)
            self.user.refresh_from_db()
            self.assertFalse(self.user.check_password(PASSWORD))
            connections.close_all()
            with dump.open('rb') as source:
                result = subprocess.run([sql_client, f'--defaults-extra-file={options}', database['NAME']],
                                        stdin=source, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(result.returncode, 0, 'SQL restore failed; diagnostics withheld to protect credentials.')
        cache.clear()
        self.user.refresh_from_db()
        # A snapshot can revive an issued password and old authenticated sessions.
        # This is why the runbook requires maintenance and the following cleanup.
        self.assertTrue(self.user.check_password(PASSWORD))
        self.assertEqual(old_session.get(reverse('user_edit_profile')).status_code, 200)
        self.assertTrue(ManagedAccountEvent.objects.filter(pk__in=snapshot_events).exists())
        Session.objects.all().delete()
        cache.clear()
        self.actor = User.objects.get(pk=self.actor.pk)
        self.admin.force_login(self.actor)
        self.reissue_then_disable()
        old_session.get(reverse('user_edit_profile'))
        self.assertNotIn(auth.SESSION_KEY, old_session.session)
        self.assert_controlled_recovery()
