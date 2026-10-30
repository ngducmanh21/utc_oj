"""M3 browser acceptance with an actual asynchronous Celery validation worker.

The memory broker is isolated to this process. Redis/process-loss recovery and
account execution are the M4 acceptance gate, not asserted by this suite.
"""

import json
from threading import Event
from unittest.mock import patch
from urllib.parse import urlsplit

from celery.contrib.testing.worker import start_worker
from django.core.cache import cache
from django.test import override_settings, tag
from django.urls import reverse

from dmoj.celery import app
from judge.models import UserImportBatch, UserImportOperation
from judge.tests.test_user_import_csv import csv_bytes
from judge.tests.test_user_import_models import IMPORT_SETTINGS
from judge.tests.test_user_import_preview import PreviewData
from judge.tests.test_user_import_service import NEW_PASSWORD, PASSWORD
from tests.browser.support import HTTPSBrowserTestCase


@tag('browser', 'user-import')
@override_settings(**IMPORT_SETTINGS, LANGUAGE_CODE='en', AUTH_PASSWORD_VALIDATORS=[],
                   SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True,
                   CELERY_BROKER_URL='memory://', CELERY_RESULT_BACKEND='cache+memory://',
                   CELERY_TASK_ALWAYS_EAGER=False, CELERY_TASK_DEFAULT_QUEUE='user-import-m3-browser')
class UserImportBrowserTests(PreviewData, HTTPSBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        previous = {name: getattr(app.conf, name) for name in
                    ('broker_url', 'result_backend', 'task_always_eager', 'task_default_queue')}
        app.conf.update(broker_url='memory://', result_backend='cache+memory://', task_always_eager=False,
                        task_default_queue='user-import-m3-browser')
        cls.addClassCleanup(lambda: app.conf.update(**previous))
        cls.worker = start_worker(app, concurrency=1, pool='solo', perform_ping_check=False,
                                  shutdown_timeout=10, loglevel='ERROR')
        cls.worker.__enter__()
        cls.addClassCleanup(lambda: cls.worker.__exit__(None, None, None))

    def setUp(self):
        super().setUp()
        cache.clear()
        self.page = self.new_page()
        validator = patch('judge.views.user.PwnedPasswordsValidator.validate')
        validator.start()
        self.addCleanup(validator.stop)
        login_url = self.live_server_url + reverse('auth_login') + '?next=' + reverse('user_import')
        self.ui(lambda: self.page.goto(login_url))
        self.ui(lambda: self.page.locator('[name=username]').fill(self.admin.username))
        self.ui(lambda: self.page.locator('[name=password]').fill(PASSWORD))
        self.ui(lambda: self.page.locator('form.form-area button[type=submit]').click())
        self.ui(lambda: self.page.locator('#import-upload-form').wait_for())

    def upload(self, data=None):
        data = self.new_csv() if data is None else data
        self.ui(lambda: self.page.locator('#import-csv-file').set_input_files({
            'name': 'accounts.csv', 'mimeType': 'text/csv', 'buffer': data,
        }))

        def submit():
            with self.page.expect_response(lambda response: urlsplit(response.url).path ==
                                           reverse('user_import_post_file')) as received:
                self.page.locator('#import-upload').click()
            return received.value.status

        return self.ui(submit)

    def ready(self, state='PREVIEW_READY'):
        self.ui(lambda: self.page.locator('#import-status[data-state="' + state + '"]').wait_for())

    def storage(self):
        return self.ui(lambda: self.page.evaluate("""() => Object.fromEntries(
            Object.entries(sessionStorage).filter(([key]) => key.startsWith('utcoj.user-import.')))
        """))

    def test_actual_worker_upload_preview_escaping_and_disabled_confirmation(self):
        before = self.account_state()
        data = csv_bytes([
            ['existing', '', 'Updated name', '', '', 'org_b', 'false'],
            ['browser_new', NEW_PASSWORD, '<script>window.csvXss=1</script>', 'UTC', '', 'org_a', 'true'],
        ])
        self.assertEqual(self.upload(data), 202)
        self.ready()
        self.assertEqual(self.account_state(), before)
        self.assertEqual(self.ui(lambda: self.page.locator('#import-rows tr').count()), 2)
        rendered = self.ui(lambda: self.page.locator('#import-rows').inner_text())
        self.assertIn('<script>window.csvXss=1</script>', rendered)
        self.assertEqual(self.ui(lambda: self.page.locator('#import-rows script').count()), 0)
        self.assertIsNone(self.ui(lambda: self.page.evaluate('window.csvXss')))
        self.assertNotIn(NEW_PASSWORD, self.ui(lambda: self.page.content()))
        self.assertTrue(self.ui(lambda: self.page.locator('#import-submit').is_disabled()))
        self.ui(lambda: self.page.locator('#import-confirmed').check())
        self.assertTrue(self.ui(lambda: self.page.locator('#import-submit').is_disabled()))
        saved = self.storage()
        for value in saved.values():
            self.assertEqual(set(json.loads(value)), {'key', 'batch_id'})
            self.assertNotIn(NEW_PASSWORD, value)
        batch = UserImportBatch.objects.get()
        self.assertFalse(batch.rows.exclude(password_ciphertext='').exists())
        self.assertTrue(batch.rows.filter(prepared_password_expires_at__isnull=False).exists())
        self.capture(self.page, 'preview-desktop')

    def test_lost_upload_response_refresh_recovers_same_key_and_batch(self):
        entered, release = Event(), Event()

        def pause(password, user):
            entered.set()
            if not release.wait(timeout=20):
                raise RuntimeError('Validation test release timed out')

        def lose_response(route):
            route.fetch()
            route.abort()

        self.ui(lambda: self.page.route('**/users/import_users/post_file/', lose_response))
        with patch('judge.utils.user_import_storage.validate_password', side_effect=pause):
            try:
                self.ui(lambda: self.page.locator('#import-csv-file').set_input_files({
                    'name': 'accounts.csv', 'mimeType': 'text/csv', 'buffer': self.new_csv(),
                }))
                self.ui(lambda: self.page.evaluate("""() => {
                    const form = document.getElementById('import-upload-form');
                    form.requestSubmit(); form.requestSubmit();
                }"""))
                self.ui(lambda: self.page.locator('#import-error').filter(has_text='Connection interrupted').wait_for())
                self.assertTrue(entered.wait(timeout=5))
                old = self.storage()
                self.assertIn('operation_key=', self.ui(lambda: self.page.url))
                self.assertEqual(UserImportBatch.objects.count(), 1)
                self.assertEqual(UserImportOperation.objects.count(), 1)
                self.ui(lambda: self.page.evaluate('sessionStorage.clear()'))
                self.ui(lambda: self.page.reload())
                self.ready('VALIDATING')
                batch = UserImportBatch.objects.get()
                self.assertEqual(batch.applied_rows, 0)
                self.assertEqual(json.loads(next(iter(old.values())))['key'], str(batch.idempotency_key))
            finally:
                release.set()
        self.ready()
        self.assertEqual(UserImportBatch.objects.count(), 1)
        self.assertEqual(UserImportOperation.objects.count(), 1)
        self.capture(self.page, 'recovered-preview')

    def test_multiple_errors_then_corrected_upload_uses_new_key_without_creating_accounts(self):
        before = self.account_state()
        data = csv_bytes([
            ['good_before_error', NEW_PASSWORD, '', '', '', '', 'true'],
            ['missing_password', '', '', '', '', '', 'false'],
            ['missing_org', NEW_PASSWORD, '', '', '', 'does-not-exist', 'true'],
        ])
        self.assertEqual(self.upload(data), 202)
        self.ready('INVALID')
        self.assertGreaterEqual(self.ui(lambda: self.page.locator('#import-errors li').count()), 2)
        old_key = UserImportBatch.objects.get().idempotency_key
        self.capture(self.page, 'invalid-csv')
        self.assertEqual(self.upload(), 202)
        self.ready()
        self.assertEqual(UserImportBatch.objects.count(), 2)
        self.assertNotEqual(UserImportBatch.objects.latest('created_at').idempotency_key, old_key)
        self.assertEqual(self.account_state(), before)

    def test_same_csv_new_upload_has_new_key_and_refresh_does_not_upload_again(self):
        self.assertEqual(self.upload(), 202)
        self.ready()
        first = UserImportBatch.objects.get()
        self.ui(lambda: self.page.reload())
        self.ready()
        self.assertEqual(UserImportBatch.objects.count(), 1)
        self.assertEqual(self.upload(), 202)
        self.ready()
        second = UserImportBatch.objects.latest('created_at')
        self.assertNotEqual(first.pk, second.pk)
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        self.assertEqual((first.applied_rows, second.applied_rows), (0, 0))

    def test_mobile_tabs_stay_in_page_on_all_user_list_pages(self):
        self.ui(lambda: self.page.set_viewport_size({'width': 390, 'height': 844}))
        for name in ('user_list', 'contributors_list', 'organization_list', 'user_import'):
            self.ui(lambda: self.page.goto(self.live_server_url + reverse(name)))
            tab = self.page.locator('.tabs a[href="' + reverse('user_import') + '"]')
            self.assertEqual(self.ui(lambda: tab.count()), 1)
            widths = self.ui(lambda: self.page.evaluate("""() => {
                const tabs = document.querySelector('.tabs');
                return {page: document.documentElement.scrollWidth, viewport: window.innerWidth,
                    tabs: tabs.getBoundingClientRect().width,
                    list: tabs.querySelector('ul').getBoundingClientRect().width};
            }"""))
            self.assertLessEqual(widths['list'], widths['tabs'] + 1)
            self.capture(self.page, 'mobile-' + name)
            self.assertLessEqual(widths['page'], widths['viewport'] + 1, name)
        self.assertEqual(self.upload(), 202)
        self.ready()
        width = self.ui(lambda: self.page.evaluate('document.documentElement.scrollWidth'))
        self.assertLessEqual(width, 391)
        self.capture(self.page, 'mobile-preview')

    def test_browser_post_without_csrf_is_json_403(self):
        response = self.ui(lambda: self.page.evaluate("""async () => {
            const response = await fetch('/users/import_users/submit/', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: '{}'});
            return {status: response.status, body: await response.json()};
        }"""))
        self.assertEqual(response['status'], 403)
        self.assertEqual(response['body']['error']['code'], 'csrf_failed')

    def test_broker_publish_failure_retries_same_file_with_same_key(self):
        with patch('judge.tasks.user_import.validate_user_import.apply_async', side_effect=ConnectionError('Offline')):
            self.assertEqual(self.upload(), 503)
        first = UserImportBatch.objects.get()
        self.assertTrue(self.ui(lambda: self.page.locator('#import-upload').is_enabled()))
        self.assertEqual(self.upload(), 200)
        self.ready()
        second = UserImportBatch.objects.get()
        self.assertEqual((first.pk, first.idempotency_key), (second.pk, second.idempotency_key))
