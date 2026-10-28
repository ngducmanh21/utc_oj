"""HTTPS and browser lifecycle for the opt-in M5 integration suite."""

import os
import ssl
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.servers.basehttp import ThreadedWSGIServer
from django.db import connection
from django.test.testcases import LiveServerThread
from django.utils.functional import classproperty
from playwright.sync_api import sync_playwright


class TLSWSGIServer(ThreadedWSGIServer):
    def handle_error(self, request, client_address):
        error = sys.exception()
        if isinstance(error, ssl.SSLError) and error.reason == 'UNEXPECTED_EOF_WHILE_READING':
            # Chrome can close speculative TLS connections during navigation.
            return
        super().handle_error(request, client_address)


class TLSLiveServerThread(LiveServerThread):
    server_class = TLSWSGIServer

    def _create_server(self, connections_override=None):
        server = super()._create_server(connections_override)
        server.socket = self.tls_context.wrap_socket(server.socket, server_side=True)
        server.base_environ['HTTPS'] = 'on'
        return server


class HTTPSBrowserTestCase(StaticLiveServerTestCase):
    """Use real secure cookies without changing the app's HTTPS WebAuthn origin.

    Playwright stays on one worker thread. ORM assertions stay on Django's test
    thread, without DJANGO_ALLOW_ASYNC_UNSAFE or shared database connections.
    """

    server_thread_class = TLSLiveServerThread

    @classproperty
    def live_server_url(cls):
        return f'https://{cls.host}:{cls.server_thread.port}'

    @classmethod
    def setUpClass(cls):
        if connection.vendor != 'mysql' or not connection.features.has_select_for_update:
            raise RuntimeError('M5 needs a MySQL/MariaDB test database with row locking.')
        if connection.settings_dict['NAME'] != 'test_utcoj_managed_login':
            raise RuntimeError('Run M5 with --settings=dmoj.managed_login_test_settings on its dedicated test DB.')
        print(f'M5 HTTPS browser: MySQL/MariaDB {connection.mysql_version}; isolated DB and secure cookies.')
        temp = TemporaryDirectory(prefix='utcoj-m5-tls-')
        cls.addClassCleanup(temp.cleanup)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
                .sign(key, hashes.SHA256()))
        key_path, cert_path = Path(temp.name) / 'key.pem', Path(temp.name) / 'cert.pem'
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        key_path.chmod(0o600)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        cls.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.tls_context.load_cert_chain(cert_path, key_path)
        super().setUpClass()
        cls.browser_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='m5-browser')
        cls.addClassCleanup(cls.browser_worker.shutdown)

        def start():
            cls.playwright = sync_playwright().start()
            cls.addClassCleanup(lambda: cls.ui(cls.playwright.stop))
            cls.browser = cls.playwright.chromium.launch(channel=os.environ.get('UTCOJ_BROWSER_CHANNEL', 'chrome'))
            cls.addClassCleanup(lambda: cls.ui(cls.browser.close))
        cls.ui(start)

    @classmethod
    def _create_server_thread(cls, connections_override):
        thread = super()._create_server_thread(connections_override)
        thread.tls_context = cls.tls_context
        return thread

    @classmethod
    def ui(cls, action):
        return cls.browser_worker.submit(action).result(timeout=60)

    def new_page(self):
        def create():
            context = self.browser.new_context(ignore_https_errors=True, locale='en-US')
            context.set_default_timeout(10000)
            # Never send test traffic to production, CDNs, analytics or providers.
            context.route('**/*', lambda route: route.continue_() if
                          urlsplit(route.request.url).hostname in ('localhost', '127.0.0.1') else route.abort())
            return context.new_page()
        page = self.ui(create)
        self.addCleanup(lambda: self.ui(page.context.close))
        return page

    def capture(self, page, name):
        folder = os.environ.get('UTCOJ_BROWSER_ARTIFACTS')
        if folder:
            target = Path(folder)
            target.mkdir(parents=True, exist_ok=True)
            self.ui(lambda: page.screenshot(path=str(target / f'{self._testMethodName}-{name}.png'), full_page=True))
