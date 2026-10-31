"""Exercise the shipped Nginx location and Django's memory-only upload handler."""

import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.test import LiveServerTestCase, override_settings, tag
from django.urls import reverse

from judge.models import UserImportBatch
from judge.tests.test_managed_password_login import BACKEND
from judge.tests.test_user_import_csv import csv_bytes
from judge.tests.test_user_import_models import IMPORT_SETTINGS
from judge.tests.test_user_import_preview import PreviewData


@tag('integration', 'user-import', 'proxy')
@override_settings(**IMPORT_SETTINGS, AUTH_PASSWORD_VALIDATORS=[], SESSION_COOKIE_SECURE=False,
                   CSRF_COOKIE_SECURE=False, LANGUAGE_CODE='en')
class UserImportProxyTests(PreviewData, LiveServerTestCase):
    def test_maximum_csv_and_overflow_leave_no_proxy_or_django_temp_files_or_credentials_in_logs(self):
        nginx = os.environ.get('UTCOJ_TEST_NGINX') or shutil.which('nginx')
        if not nginx:
            self.fail('Install Nginx or set UTCOJ_TEST_NGINX for this explicit proxy acceptance gate.')
        with TemporaryDirectory(prefix='utcoj-import-proxy-') as directory:
            root = Path(directory)
            for name in ('body', 'proxy', 'django', 'logs', 'fastcgi', 'uwsgi', 'scgi'):
                (root / name).mkdir()
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            location = Path(settings.BASE_DIR, 'deploy/user-import/nginx.conf').read_text()
            location = location.replace('http://site:8000', self.live_server_url)
            config = ('daemon off; master_process off;\npid ' + str(root / 'nginx.pid') + ';\n'
                      'error_log ' + str(root / 'error.log') + ' warn;\nevents {}\nhttp {\n'
                      'client_body_temp_path ' + str(root / 'body') + ';\n'
                      'proxy_temp_path ' + str(root / 'proxy') + ';\n'
                      'fastcgi_temp_path ' + str(root / 'fastcgi') + ';\n'
                      'uwsgi_temp_path ' + str(root / 'uwsgi') + ';\n'
                      'scgi_temp_path ' + str(root / 'scgi') + ';\n'
                      'access_log ' + str(root / 'access.log') + ';\nserver {\n'
                      'listen 127.0.0.1:' + str(port) + ';\n' + location + '\n}\n}\n')
            config_path = root / 'nginx.conf'
            config_path.write_text(config)
            checked = subprocess.run([nginx, '-t', '-p', directory, '-c', str(config_path)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            process = subprocess.Popen([nginx, '-p', directory, '-c', str(config_path)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                            break
                    except OSError:
                        if time.monotonic() > deadline:
                            self.fail('Private Nginx did not start')
                        time.sleep(0.05)
                session = requests.Session()
                self.client.force_login(self.admin, backend=BACKEND)
                session.cookies.update({cookie: value.value for cookie, value in self.client.cookies.items()})
                base = 'http://127.0.0.1:' + str(port)
                page = session.get(base + reverse('user_import'), timeout=5)
                self.assertEqual(page.status_code, 200)
                headers = {'X-CSRFToken': session.cookies[settings.CSRF_COOKIE_NAME]}
                marker = 'Proxy-S9!never-log-this-' + uuid.uuid4().hex
                rows = [[f'proxy_{i}', marker, '', '', '', '', 'false'] for i in range(2000)]
                remaining = 5 * 1024 * 1024 - len(csv_bytes(rows))
                for i, row in enumerate(rows):
                    row[1] += 'x' * (remaining // 2000 + (i < remaining % 2000))
                data = csv_bytes(rows)
                self.assertEqual(len(data), 5 * 1024 * 1024)
                with override_settings(FILE_UPLOAD_TEMP_DIR=str(root / 'django')), \
                        patch('judge.tasks.user_import.validate_user_import.apply_async'):
                    for body, expected in ((data, 202), (data + b'x', 413), (b'x' * (6 * 1024 * 1024 + 1), 413)):
                        headers['Idempotency-Key'] = str(uuid.uuid4())
                        response = session.post(base + reverse('user_import_post_file'), headers=headers,
                                                files={'csv_file': ('accounts.csv', body, 'text/csv')}, timeout=30)
                        self.assertEqual(response.status_code, expected)
                        self.assertIn('application/json', response.headers['Content-Type'])
                        self.assertIn('no-store', response.headers['Cache-Control'])
                        self.assertNotIn(marker, response.text)
                self.assertEqual(UserImportBatch.objects.count(), 1)
                self.assertEqual(User.objects.filter(username__startswith='proxy_').count(), 0)
                for name in ('body', 'proxy', 'django'):
                    self.assertEqual(list((root / name).iterdir()), [])
                for name in ('error.log', 'access.log'):
                    logfile = root / name
                    if logfile.exists():
                        self.assertNotIn(marker, logfile.read_text())
                print('M5 Nginx: exact 5 MiB accepted, app/proxy overflow JSON 413, temp directories empty.')
            finally:
                process.terminate()
                process.wait(timeout=5)
