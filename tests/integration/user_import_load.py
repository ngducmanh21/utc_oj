"""Bounded M5 load with real PBKDF2, validators, Redis and prefork execution.

Use dmoj.user_import_load_test_settings. No request reaches the HIBP service:
the local range fixture exercises the real HTTP parser and timeout fallback.
UTCOJ_IMPORT_FULL_TIMEOUT=1 expands the timeout case from 32 to 2,000 rows.
"""

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TransactionTestCase, tag
from django.urls import reverse

from judge.models import UserImportBatch
from judge.tests.test_user_import_csv import csv_bytes
from tests.integration.user_import import UserImportHarness


class RangeFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.server.delay:
            time.sleep(self.server.delay)
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(('0' * 35 + ':0\r\n').encode())
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when the real two-second timeout expires.

    def log_message(self, *args):
        pass  # Even the SHA1 range prefix is unnecessary in test artifacts.


@tag('integration', 'user-import', 'load')
class UserImportLoadTests(UserImportHarness, TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if settings.PASSWORD_HASHERS != ['django.contrib.auth.hashers.PBKDF2PasswordHasher']:
            raise RuntimeError('Use dmoj.user_import_load_test_settings; MD5 is not a load acceptance gate.')
        super().setUpClass()
        cls.hibp = ThreadingHTTPServer(('127.0.0.1', 0), RangeFixture)
        cls.hibp.daemon_threads = True
        cls.hibp.delay = 0
        thread = threading.Thread(target=cls.hibp.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(cls.hibp.server_close)
        cls.addClassCleanup(cls.hibp.shutdown)
        environment = patch.dict(os.environ, {
            'UTCOJ_IMPORT_LOAD_TEST': '1',
            'UTCOJ_IMPORT_TEST_HIBP': 'http://127.0.0.1:%s/range/{}' % cls.hibp.server_port,
        })
        environment.start()
        cls.addClassCleanup(environment.stop)

    def upload(self, data):
        started = time.monotonic()
        response = self.client.post(reverse('user_import_post_file'), {
            'csv_file': SimpleUploadedFile('load.csv', data), 'muted': 'true',
        }, HTTP_X_CSRFTOKEN=self.csrf, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        return response, time.monotonic() - started

    def poll(self, batch, state, timeout):
        deadline = time.monotonic() + timeout
        latencies = []
        peak_rss = 0
        while time.monotonic() < deadline:
            started = time.monotonic()
            response = self.client.get(reverse('user_import_status', args=[batch.pk]))
            latencies.append(time.monotonic() - started)
            self.assertEqual(response.status_code, 200)
            batch.refresh_from_db()
            if batch.status == state:
                return max(latencies), peak_rss
            self.assertIn(batch.status, ('VALIDATING', 'QUEUED', 'RUNNING'))
            self.assertEqual(batch.applied_rows, 0)
            visible = User.objects.filter(username__startswith='load_user_').count()
            self.assertIn(visible, (0, batch.total_rows))
            if visible:
                batch.refresh_from_db()
                self.assertEqual(batch.status, 'COMPLETED')
            for worker in self.workers:
                children = Path('/proc/%s/task/%s/children' % (worker.pid, worker.pid))
                if children.exists():
                    for pid in children.read_text().split():
                        status = Path('/proc/%s/status' % pid)
                        if status.exists():
                            for line in status.read_text().splitlines():
                                if line.startswith('VmRSS:'):
                                    peak_rss = max(peak_rss, int(line.split()[1]))
            time.sleep(0.5)
        self.fail('Load validation/execution exceeded its acceptance deadline')

    def run_load(self, count, exact_size=False):
        self.start_worker()
        rows = [[f'load_user_{i}', 'S9!independent-secret-' + uuid.uuid4().hex, '', '', '', '', 'false']
                for i in range(count)]
        if exact_size:
            remaining = settings.DMOJ_USER_IMPORT_MAX_BYTES - len(csv_bytes(rows))
            for i, row in enumerate(rows):
                row[1] += 'x' * (remaining // count + (i < remaining % count))
        data = csv_bytes(rows)
        if exact_size:
            self.assertEqual(len(data), 5 * 1024 * 1024)
        response, upload_seconds = self.upload(data)
        self.assertEqual(response.status_code, 202)
        self.assertLess(upload_seconds, 30)
        batch = UserImportBatch.objects.get(pk=response.json()['batch']['id'])
        validation_started = time.monotonic()
        poll_validation, rss_validation = self.poll(batch, 'PREVIEW_READY', 850)
        validation_seconds = time.monotonic() - validation_started
        self.assertLess((batch.updated_at - batch.created_at).total_seconds(), 900)
        self.assertFalse(batch.rows.exclude(password_ciphertext='').exists())
        self.assertEqual(User.objects.filter(username__startswith='load_user_').count(), 0)
        started = time.monotonic()
        self.assertIn(self.post_confirm(batch).status_code, (200, 202))
        poll_execution, rss_execution = self.poll(batch, 'COMPLETED', 180)
        execution_seconds = time.monotonic() - started
        self.assertEqual((batch.applied_rows, batch.created_users, batch.execution_attempts), (count, count, 1))
        self.assertEqual(User.objects.filter(username__startswith='load_user_').count(), count)
        self.assertFalse(batch.rows.exclude(password_ciphertext='', prepared_password_ciphertext='').exists())
        with connection.cursor() as cursor:
            cursor.execute('SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES '
                           'WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = %s', ['BASE TABLE'])
            self.assertTrue(all(engine == 'InnoDB' for name, engine in cursor.fetchall()))
        result = {'rows': count, 'bytes': len(data), 'hibp_timeout': bool(self.hibp.delay),
                  'upload_seconds': round(upload_seconds, 3), 'validation_seconds': round(validation_seconds, 3),
                  'execution_seconds': round(execution_seconds, 3),
                  'account_transaction_seconds': round(float(self.redis.get(
                      self.control_key('execution_seconds:' + str(batch.pk))) or 0), 3),
                  'max_poll_seconds': round(max(poll_validation, poll_execution), 3),
                  'peak_worker_rss_kib': max(rss_validation, rss_execution), 'database': connection.mysql_version}
        self.assertLess(result['max_poll_seconds'], 5)
        self.assertLess(result['peak_worker_rss_kib'], 512 * 1024)
        print('M5 load ' + json.dumps(result, sort_keys=True))
        directory = os.environ.get('UTCOJ_IMPORT_ARTIFACTS')
        if directory:
            Path(directory, self._testMethodName + '.json').write_text(json.dumps(result, indent=2) + '\n')

    def test_two_thousand_rows_exact_five_mib(self):
        self.hibp.delay = 0
        self.run_load(2000, exact_size=True)

    def test_real_two_second_hibp_timeout_and_common_password_fallback(self):
        self.hibp.delay = 2.25
        self.run_load(2000 if os.environ.get('UTCOJ_IMPORT_FULL_TIMEOUT') == '1' else 32)

    def test_size_and_row_overflow_do_not_create_batches_or_accounts(self):
        before = self.account_state()
        batches = UserImportBatch.objects.count()
        response, unused = self.upload(b'x' * (settings.DMOJ_USER_IMPORT_MAX_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        rows = [[f'load_user_{i}', 'NeverPersisted-S9!', '', '', '', '', 'false'] for i in range(2001)]
        response, unused = self.upload(csv_bytes(rows))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(UserImportBatch.objects.count(), batches)
        self.assertEqual(self.account_state(), before)
