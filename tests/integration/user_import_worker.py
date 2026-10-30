"""Prefork worker with test-only fault gates, never imported by application code."""

import json
import os
import time
from unittest.mock import patch

import django
import redis
from celery.signals import worker_ready
from django.conf import settings
from django.db import OperationalError, connection

os.environ['DJANGO_SETTINGS_MODULE'] = 'dmoj.user_import_worker_test_settings'
django.setup()

from dmoj.celery import app  # noqa: E402
from judge.tasks import user_import as tasks  # noqa: E402
from judge.utils import user_import_execution as execution, user_import_service as service  # noqa: E402
from judge.utils.user_import_progress import write_import_progress  # noqa: E402

control = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=1, socket_connect_timeout=1)
namespace = settings.CELERY_TASK_DEFAULT_QUEUE
real_row = service._apply_row
real_execute = tasks.execute_import_batch
real_prepare = tasks.prepare_import_password
real_reconcile = tasks.reconcile_import_execution
real_cache = execution.invalidate_import_caches


def key(name):
    return namespace + ':' + name


def fault(batch_id):
    value = json.loads(control.get(key('fault')) or '{}')
    return value if value.get('batch') == str(batch_id) else {}


def once(batch_id, kind):
    return control.set(key('once:' + str(batch_id) + ':' + kind), '1', nx=True)


def gate(batch, phase, row_number):
    connection.ensure_connection()
    control.set(key('paused:' + str(batch.pk)), json.dumps({
        'database': connection.settings_dict['NAME'], 'connection_id': connection.connection.thread_id(),
        'pid': os.getpid(), 'phase': phase, 'row': row_number,
    }))
    if phase == 'execution':
        write_import_progress(batch.pk, batch.attempt_token, row_number, batch.total_rows)
    deadline = time.monotonic() + 30
    while not control.get(key('release:' + str(batch.pk))):
        if time.monotonic() >= deadline:
            raise RuntimeError('The integration fault gate timed out.')
        time.sleep(0.05)


def apply_row(actor, batch, row, *args):
    result = real_row(actor, batch, row, *args)
    config = fault(batch.pk)
    kind = config.get('kind')
    if row.row_number == config.get('row', 60):
        if kind == 'row_pause' and once(batch.pk, kind):
            gate(batch, 'execution', row.row_number)
        elif kind == 'deadlock_always':
            raise OperationalError(1213, 'Integration rollback injection')
    return result


def prepare(row, user, **kwargs):
    result = real_prepare(row, user, **kwargs)
    config = fault(row.batch_id)
    if config.get('kind') == 'validation_pause' and row.row_number == 1 and once(row.batch_id, 'validation_pause'):
        gate(row.batch, 'validation', row.row_number)
    return result


def execute(actor, batch_id, version, token, **kwargs):
    config = fault(batch_id)
    kind = config.get('kind')
    if kind in ('lost_ack', 'lost_ack_unknown', 'unknown_rollback') and once(batch_id, kind):
        commit = connection.commit

        def lose_response():
            if kind != 'unknown_rollback':
                commit()
            control.set(key('commit_fault:' + str(batch_id)), kind)
            raise OperationalError(2013, 'Integration COMMIT response injection')

        with patch.object(connection, 'commit', side_effect=lose_response):
            return real_execute(actor, batch_id, version, token, **kwargs)
    result = real_execute(actor, batch_id, version, token, **kwargs)
    if kind == 'after_commit_exit' and once(batch_id, kind):
        control.set(key('committed:' + str(batch_id)), '1')
        os._exit(71)
    return result


def reconcile(batch_id, version, **kwargs):
    if fault(batch_id).get('kind') in ('lost_ack_unknown', 'unknown_rollback'):
        control.set(key('outcome_unknown:' + str(batch_id)), '1')
        return 'unknown'
    return real_reconcile(batch_id, version, **kwargs)


def invalidate_cache(batch_id):
    if fault(batch_id).get('kind') == 'cache_failure':
        raise RuntimeError('Integration cache failure injection')
    return real_cache(batch_id)


service._apply_row = apply_row
tasks.prepare_import_password = prepare
tasks.execute_import_batch = execute
tasks.reconcile_import_execution = reconcile
execution.invalidate_import_caches = invalidate_cache


@worker_ready.connect(weak=False)
def ready(**kwargs):
    control.set(key('ready'), '1')


if __name__ == '__main__':
    app.worker_main(['worker', '--pool=prefork', '--concurrency=1', '--loglevel=WARNING',
                     '--without-gossip', '--without-mingle', '--without-heartbeat', '--queues=' + namespace])
