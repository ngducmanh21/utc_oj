"""Redis + prefork worker acceptance on a disposable MySQL DB.

Run with dmoj.user_import_integration_test_settings. Fault gates live only in
the worker harness; production code has no test switches or secret payloads.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import redis
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TransactionTestCase, override_settings, tag
from django.urls import reverse

from dmoj.celery import app
from judge.models import UserImportBatch, UserImportOperation
from judge.tasks.user_import import dispatch_import_execution, execute_user_import
from judge.tests.test_managed_password_login import BACKEND
from judge.tests.test_user_import_csv import csv_bytes
from judge.tests.test_user_import_execution import ExecutionData
from judge.tests.test_user_import_service import NEW_PASSWORD
from judge.utils.user_import_maintenance import recover_import_batches


@tag('integration', 'user-import')
class UserImportWorkerTests(ExecutionData, TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        if (connection.vendor != 'mysql' or
                connection.settings_dict['NAME'] != 'test_utcoj_user_import_integration'):
            raise RuntimeError('Run import worker tests with dmoj.user_import_integration_test_settings.')
        super().setUpClass()
        executable = shutil.which('redis-server')
        if executable is None:
            raise RuntimeError('The M4 acceptance gate requires redis-server and a real worker.')
        cls.temporary = TemporaryDirectory(prefix='utcoj-import-m4-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.executable = executable
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            cls.port = probe.getsockname()[1]
        cls.broker = 'redis://127.0.0.1:%s/0' % cls.port
        cls.queue = 'utcoj-import-integration-' + uuid.uuid4().hex
        cls.redis = redis.Redis.from_url(cls.broker, socket_timeout=0.5, socket_connect_timeout=0.5)
        cls.start_redis()
        cls.addClassCleanup(cls.stop_redis)
        cls.environment = patch.dict(os.environ, {
            'CELERY_BROKER_URL': cls.broker, 'CELERY_BROKER_READ_URL': cls.broker,
            'CELERY_BROKER_WRITE_URL': cls.broker, 'CELERY_RESULT_BACKEND': cls.broker[:-1] + '2',
        })
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        caches = dict(settings.CACHES)
        caches['import_progress'] = {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache', 'LOCATION': cls.broker[:-1] + '1',
            'OPTIONS': {'socket_timeout': 0.5, 'socket_connect_timeout': 0.5},
        }
        cls.options = override_settings(
            CELERY_BROKER_URL=cls.broker, CELERY_RESULT_BACKEND=cls.broker[:-1] + '2',
            CELERY_TASK_ALWAYS_EAGER=False, CELERY_BROKER_CONNECTION_TIMEOUT=0.5,
            CELERY_TASK_DEFAULT_QUEUE=cls.queue,
            CELERY_BROKER_TRANSPORT_OPTIONS={'visibility_timeout': 2, 'socket_timeout': 0.5,
                                             'socket_connect_timeout': 0.5},
            CACHES=caches, DMOJ_USER_IMPORT_PROGRESS_CACHE='import_progress',
        )
        cls.options.enable()
        cls.addClassCleanup(cls.options.disable)
        print('M4 acceptance: MySQL %s, disposable loopback Redis, real prefork worker; no eager execution.' %
              (connection.mysql_version,))

    @classmethod
    def start_redis(cls):
        cls.redis_process = subprocess.Popen([
            cls.executable, '--bind', '127.0.0.1', '--port', str(cls.port), '--save', '',
            '--appendonly', 'no', '--protected-mode', 'yes', '--dir', cls.temporary.name,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if cls.redis.ping():
                    return
            except redis.RedisError:
                time.sleep(0.05)
        raise RuntimeError('The private Redis test server did not start.')

    @classmethod
    def stop_redis(cls):
        if cls.redis_process.poll() is None:
            cls.redis_process.terminate()
            try:
                cls.redis_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                cls.redis_process.kill()
                cls.redis_process.wait(timeout=3)

    def setUp(self):
        super().setUp()
        self.workers = []
        self.addCleanup(self.stop_workers)
        self.clear_fault()
        self.assertIs(execute_user_import.app, app)
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.admin, backend=BACKEND)
        self.assertEqual(self.client.get(reverse('user_import')).status_code, 200)
        self.csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value

    def control_key(self, name):
        return self.queue + ':' + name

    def wait(self, predicate, *, timeout=25, description='integration condition'):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail('Timed out waiting for ' + description)

    def wait_state(self, batch, status, timeout=25):
        def ready():
            batch.refresh_from_db()
            return batch.status == status
        self.wait(ready, timeout=timeout, description=status)

    def configure_fault(self, batch, kind, row=60):
        self.redis.set(self.control_key('fault'), json.dumps({'batch': str(batch.pk), 'kind': kind, 'row': row}))

    def clear_fault(self):
        self.redis.delete(self.control_key('fault'))

    def start_worker(self):
        self.redis.delete(self.control_key('ready'))
        directory = Path(os.environ.get('UTCOJ_IMPORT_ARTIFACTS', self.temporary.name))
        directory.mkdir(parents=True, exist_ok=True)
        logfile = (directory / (self._testMethodName + '-worker-%s.log' % len(self.workers))).open('w')
        self.addCleanup(logfile.close)
        environment = dict(os.environ)
        environment.update({
            'UTCOJ_IMPORT_WORKER_TEST': '1', 'UTCOJ_IMPORT_WORKER_DB': connection.settings_dict['NAME'],
            'UTCOJ_IMPORT_WORKER_BROKER': self.broker, 'UTCOJ_IMPORT_WORKER_QUEUE': self.queue,
        })
        environment.pop('TEST_BROKER', None)
        worker = subprocess.Popen([sys.executable, '-m', 'tests.integration.user_import_worker'],
                                  env=environment, cwd=settings.BASE_DIR, stdout=logfile, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        self.workers.append(worker)
        self.wait(lambda: self.redis.get(self.control_key('ready')), timeout=15, description='prefork worker ready')
        self.assertIsNone(worker.poll(), 'Worker exited during startup')
        return worker

    def kill_worker(self, worker):
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=5)

    def stop_workers(self):
        for worker in self.workers:
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                try:
                    worker.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.kill_worker(worker)

    def post_confirm(self, batch):
        return self.client.post(reverse('user_import_submit'), json.dumps({
            'batch_id': str(batch.pk), 'version': batch.version, 'confirmed': True,
        }), content_type='application/json', HTTP_X_CSRFTOKEN=self.csrf,
            HTTP_IDEMPOTENCY_KEY=str(batch.idempotency_key))

    def paused(self, batch):
        value = self.wait(lambda: self.redis.get(self.control_key('paused:' + str(batch.pk))),
                          description='worker fault gate')
        return json.loads(value)

    def release(self, batch):
        self.redis.set(self.control_key('release:' + str(batch.pk)), '1')

    def hundred(self):
        rows = [[f'process_new_{i}', NEW_PASSWORD, 'Process account', '', '', 'org_a', 'true'] for i in range(100)]
        rows[1] = ['existing', NEW_PASSWORD, 'Changed by batch', '', '', 'org_b', 'true']
        return self.queued(rows)

    def test_upload_validation_confirm_completion_duplicate_delivery_and_secret_free_broker(self):
        self.start_worker()
        before = self.account_state()
        key = uuid.uuid4()
        response = self.client.post(reverse('user_import_post_file'), {
            'csv_file': SimpleUploadedFile('accounts.csv', self.new_csv()), 'muted': 'true',
        }, HTTP_X_CSRFTOKEN=self.csrf, HTTP_IDEMPOTENCY_KEY=str(key))
        self.assertEqual(response.status_code, 202)
        batch = UserImportBatch.objects.get(pk=response.json()['batch']['id'])
        self.wait_state(batch, 'PREVIEW_READY')
        self.assertEqual(self.account_state(), before)
        prepared = batch.rows.get().prepared_password_ciphertext
        self.assertTrue(prepared)
        self.assertIn(self.post_confirm(batch).status_code, (200, 202))
        self.assertIn(self.post_confirm(batch).status_code, (200, 202))
        self.wait_state(batch, 'COMPLETED')
        self.assertEqual((batch.applied_rows, batch.execution_attempts), (1, 1))
        after = self.account_state()
        execute_user_import.apply_async(args=[str(batch.pk), 1], queue=self.queue, retry=False)
        self.assertEqual(self.post_confirm(batch).status_code, 200)
        time.sleep(0.25)
        self.assertEqual(self.account_state(), after)
        self.assertEqual(UserImportOperation.objects.filter(batch=batch).count(), 2)
        for database in (0, 1, 2):
            client = redis.Redis.from_url(self.broker[:-1] + str(database))
            for redis_key in client.scan_iter():
                data = client.dump(redis_key) or b''
                self.assertNotIn(NEW_PASSWORD.encode(), data)
                self.assertNotIn(prepared.encode(), data)

    def test_sigkill_mid_transaction_recovers_entire_hundred_row_batch(self):
        batch = self.hundred()
        before = self.account_state()
        self.configure_fault(batch, 'row_pause')
        worker = self.start_worker()
        dispatch_import_execution(batch.pk, 1)
        info = self.paused(batch)
        self.assertEqual(info['phase'], 'execution')
        self.assertEqual(info['row'], 60)
        started = time.monotonic()
        response = self.client.get(reverse('user_import_status', args=[batch.pk]))
        self.assertLess(time.monotonic() - started, 2)
        payload = response.json()['batch']
        self.assertEqual((payload['status'], payload['applied_rows']), ('RUNNING', 0))
        self.assertEqual(payload['progress'], {'processed': 60, 'total': 100, 'committed': False})
        time.sleep(2.1)
        self.assertGreaterEqual(recover_import_batches(batch_id=batch.pk)['pending'], 1)
        self.assertEqual(self.account_state(), before)
        self.kill_worker(worker)
        self.clear_fault()
        self.start_worker()
        self.wait(lambda: recover_import_batches(batch_id=batch.pk)['dispatched'], description='rollback recovery')
        self.wait_state(batch, 'COMPLETED')
        self.assertEqual((batch.applied_rows, batch.created_users, batch.updated_users, batch.execution_attempts),
                         (100, 99, 1, 2))
        self.assertEqual(batch.rows.filter(status='SUCCESS').count(), 100)

    def test_real_mysql_connection_loss_rolls_back_then_retries(self):
        batch = self.hundred()
        self.configure_fault(batch, 'row_pause')
        self.start_worker()
        dispatch_import_execution(batch.pk, 1)
        info = self.paused(batch)
        self.assertEqual(info['database'], 'test_utcoj_user_import_integration')
        connection_id = info['connection_id']
        self.assertIs(type(connection_id), int)
        with connection.cursor() as cursor:
            cursor.execute('SELECT DB FROM information_schema.PROCESSLIST WHERE ID=%s', [connection_id])
            self.assertEqual(cursor.fetchone(), ('test_utcoj_user_import_integration',))
            # Only the verified, test-owned worker connection is terminated.
            cursor.execute('KILL CONNECTION %s', [connection_id])
        self.clear_fault()
        self.release(batch)
        self.wait_state(batch, 'COMPLETED')
        self.assertEqual((batch.applied_rows, batch.execution_attempts), (100, 2))

    def test_process_exit_after_commit_redelivery_does_not_reissue_credentials(self):
        batch = self.queued()
        self.configure_fault(batch, 'after_commit_exit')
        self.start_worker()
        dispatch_import_execution(batch.pk, 1)
        self.wait(lambda: self.redis.get(self.control_key('committed:' + str(batch.pk))))
        self.wait_state(batch, 'COMPLETED')
        before = self.account_state()
        self.clear_fault()
        execute_user_import.apply_async(args=[str(batch.pk), 1], queue=self.queue, retry=False)
        recover_import_batches()
        time.sleep(0.5)
        batch.refresh_from_db()
        self.assertEqual((batch.applied_rows, batch.execution_attempts), (1, 1))
        self.assertEqual(self.account_state(), before)

    def test_lost_commit_ack_in_process_reads_marker_and_keeps_one_attempt(self):
        batch = self.queued()
        self.configure_fault(batch, 'lost_ack')
        self.start_worker()
        dispatch_import_execution(batch.pk, 1)
        self.wait(lambda: self.redis.get(self.control_key('commit_fault:' + str(batch.pk))))
        self.wait_state(batch, 'COMPLETED')
        self.wait(lambda: UserImportBatch.objects.filter(pk=batch.pk, errors=[]).exists())
        self.assertEqual((batch.applied_rows, batch.execution_attempts), (1, 1))

    def test_last_attempt_unknown_rollback_waits_for_reconciliation_before_failed(self):
        batch = self.queued()
        UserImportBatch.objects.filter(pk=batch.pk).update(execution_attempts=2)
        before = self.account_state()
        self.configure_fault(batch, 'unknown_rollback')
        self.start_worker()
        dispatch_import_execution(batch.pk, 1)
        self.wait(lambda: self.redis.get(self.control_key('outcome_unknown:' + str(batch.pk))))
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.execution_attempts, batch.applied_rows), ('RUNNING', 3, 0))
        self.assertEqual(self.account_state(), before)
        self.clear_fault()
        time.sleep(2.1)
        recover_import_batches(batch_id=batch.pk)
        self.wait_state(batch, 'FAILED')
        self.assertEqual(batch.execution_attempts, 3)

    def test_last_attempt_committed_with_unknown_ack_is_completed_not_failed(self):
        batch = self.queued()
        UserImportBatch.objects.filter(pk=batch.pk).update(execution_attempts=2)
        self.configure_fault(batch, 'lost_ack_unknown')
        self.start_worker()
        dispatch_import_execution(batch.pk, 1)
        self.wait(lambda: self.redis.get(self.control_key('outcome_unknown:' + str(batch.pk))))
        self.wait_state(batch, 'COMPLETED')
        self.assertEqual((batch.execution_attempts, batch.applied_rows), (3, 1))
        before = self.account_state()
        self.clear_fault()
        recover_import_batches(batch_id=batch.pk)
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.execution_attempts, batch.errors), ('COMPLETED', 3, []))
        self.assertEqual(self.account_state(), before)

    def test_attempt_budget_survives_worker_restart_and_repeated_confirmations(self):
        batch = self.hundred()
        self.configure_fault(batch, 'row_pause')
        worker = self.start_worker()
        dispatch_import_execution(batch.pk, 1)
        self.paused(batch)
        time.sleep(2.1)
        self.kill_worker(worker)
        self.configure_fault(batch, 'deadlock_always')
        self.start_worker()
        self.wait(lambda: recover_import_batches(batch_id=batch.pk)['dispatched'], description='restart recovery')
        for _ in range(3):
            self.assertIn(self.post_confirm(batch).status_code, (200, 202))
        self.wait_state(batch, 'FAILED', timeout=30)
        self.assertEqual((batch.applied_rows, batch.execution_attempts), (0, 3))
        self.assertFalse(batch.rows.exclude(prepared_password_ciphertext='').exists())

    def test_validation_process_loss_recovers_prepared_password_without_rehashing(self):
        batch = self.reserve(csv_bytes([
            ['validated_first', NEW_PASSWORD, '', '', '', '', 'true'],
            ['validated_second', NEW_PASSWORD, '', '', '', '', 'false'],
        ]))
        self.configure_fault(batch, 'validation_pause')
        worker = self.start_worker()
        recover_import_batches(batch_id=batch.pk)
        self.paused(batch)
        prepared = batch.rows.get(row_number=1).prepared_password_ciphertext
        self.assertTrue(prepared)
        time.sleep(2.1)
        self.kill_worker(worker)
        self.clear_fault()
        self.start_worker()
        recover_import_batches(batch_id=batch.pk)
        self.wait_state(batch, 'PREVIEW_READY')
        self.assertEqual(batch.validation_attempts, 2)
        self.assertEqual(batch.rows.get(row_number=1).prepared_password_ciphertext, prepared)
        self.assertEqual(batch.applied_rows, 0)

    def test_actual_broker_unavailable_keeps_queued_and_recovery_dispatches_later(self):
        batch = self.validate(self.reserve())
        self.stop_redis()
        try:
            response = self.post_confirm(batch)
            self.assertEqual(response.status_code, 503)
            batch.refresh_from_db()
            self.assertEqual((batch.status, batch.execution_attempts), ('QUEUED', 0))
        finally:
            self.start_redis()
        self.start_worker()
        recover_import_batches(batch_id=batch.pk)
        self.wait_state(batch, 'COMPLETED')
        self.assertEqual(batch.execution_attempts, 1)
