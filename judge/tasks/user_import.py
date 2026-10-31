"""Validation/execution carry IDs only; results stay behind the owner API."""

import logging
import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from threading import Event

from celery import shared_task
from celery.exceptions import Retry
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, OperationalError, close_old_connections, connections, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from judge.models import UserImportBatch
from judge.utils.user_import import ImportIssue, UserImportError, require_user_import_enabled
from judge.utils.user_import_execution import LOCK_BUSY, claim_import_execution, database_error_code, \
    execution_failure, reconcile_import_execution, repair_import_cache
from judge.utils.user_import_progress import write_import_progress
from judge.utils.user_import_service import ImportBatchConflict, capture_import_snapshot, execute_import_batch, \
    finalize_import_preview
from judge.utils.user_import_storage import prepare_import_password
from judge.utils.user_import_validation import claim_import_validation, finish_import_validation, \
    invalid_row_candidates, locked_validation, release_import_validation, resolve_validation_rows

__all__ = ('validate_user_import', 'execute_user_import', 'repair_user_import_cache', 'maintain_user_imports')
logger = logging.getLogger(__name__)


def dispatch_import_validation(batch_id, version):
    """Reserve one delivery; a failed broker publish leaves the same batch recoverable."""
    task_id = str(uuid.uuid4())
    with transaction.atomic():
        batch = UserImportBatch.objects.select_for_update().get(pk=batch_id)
        if (batch.version != version or batch.status != UserImportBatch.Status.VALIDATING or
                batch.task_id or batch.lease_expires_at):
            return True
        batch.task_id = task_id
        batch.save(update_fields=['task_id', 'updated_at'])
    try:
        validate_user_import.apply_async(args=[str(batch_id), version], task_id=task_id, retry=False)
        return True
    except Exception:
        # Do not log exception strings/tracebacks from broker libraries or callbacks.
        logger.warning('User import validation dispatch unavailable for batch %s', batch_id)
        UserImportBatch.objects.filter(pk=batch_id, version=version, status=UserImportBatch.Status.VALIDATING,
                                       task_id=task_id, attempt_token__isnull=True).update(task_id='')
        return False


def dispatch_import_execution(batch_id, version):
    require_user_import_enabled()
    task_id = str(uuid.uuid4())
    try:
        with transaction.atomic():
            batch = UserImportBatch.objects.select_for_update(nowait=True).get(pk=batch_id)
            if batch.version != version or batch.status != UserImportBatch.Status.QUEUED or batch.task_id:
                return True
            countdown = max(0, (batch.lease_expires_at - timezone.now()).total_seconds()) \
                if batch.lease_expires_at else 0
            batch.task_id = task_id
            batch.save(update_fields=['task_id', 'updated_at'])
        try:
            execute_user_import.apply_async(args=[str(batch_id), version], task_id=task_id,
                                            countdown=countdown, retry=False)
            return True
        except Exception:
            logger.warning('User import execution dispatch unavailable for batch %s', batch_id)
            UserImportBatch.objects.filter(pk=batch_id, version=version, status='QUEUED', task_id=task_id,
                                           attempt_token__isnull=True).update(task_id='')
            return False
    except OperationalError as error:
        if database_error_code(error) in LOCK_BUSY:
            return True
        raise


@shared_task(bind=True, ignore_result=True, acks_late=True, reject_on_worker_lost=True)
@sensitive_variables()
def execute_user_import(self, batch_id, version):
    token = None
    outcome = None
    try:
        if connections['default'].in_atomic_block:
            raise RuntimeError('Execution tasks require an outermost account transaction.')
        claimed = claim_import_execution(batch_id, version)
        if claimed is None:
            if UserImportBatch.objects.filter(pk=batch_id, version=version, status='COMPLETED').exists():
                repair_import_cache(batch_id, version)
            return
        batch, actor = claimed
        token = batch.attempt_token
        write_import_progress(batch_id, token, 0, batch.total_rows)
        try:
            execute_import_batch(actor, batch_id, version, token, progress=lambda done, total:
                                 write_import_progress(batch_id, token, done, total))
            outcome = 'completed'
        except Exception as error:
            failure = execution_failure(error)
            # Do not infer rollback from the exception, including a COMMIT error.
            # A new primary connection must acquire the batch lock and read it.
            connections['default'].close()
            outcome = reconcile_import_execution(batch_id, version, token=token, failure=failure)
        if outcome == 'queued':
            dispatch_import_execution(batch_id, version)
        elif outcome == 'completed':
            repair_import_cache(batch_id, version)
    except UserImportError as error:
        if error.code != 'disabled':
            logger.warning('User import execution interrupted for batch %s', batch_id)
    except Exception:
        # Claim/repair/reconciliation can themselves lose their DB connection.
        # Leave the durable marker for recovery; never log row/exception content.
        logger.warning('User import execution outcome pending for batch %s', batch_id)


@shared_task(ignore_result=True)
def repair_user_import_cache(batch_id, version):
    try:
        repair_import_cache(batch_id, version)
    except Exception:
        logger.warning('User import cache repair interrupted for batch %s', batch_id)


@shared_task(ignore_result=True)
def maintain_user_imports(cleanup=False):
    from judge.utils.user_import_maintenance import cleanup_import_batches, recover_import_batches
    try:
        if cleanup:
            cleanup_import_batches()
        else:
            recover_import_batches()
    except Exception:
        logger.warning('User import maintenance interrupted')


@shared_task(bind=True, ignore_result=True, acks_late=True, reject_on_worker_lost=True)
@sensitive_variables()
def validate_user_import(self, batch_id, version):
    try:
        _validate_user_import(self, batch_id, version)
    except Retry:
        raise
    except Exception:
        # State-recording itself can fail while the DB is unavailable. Leave
        # the current claim to recovery/expiry; never emit credential tracebacks.
        logger.warning('User import validation interrupted for batch %s', batch_id)


@sensitive_variables()
def _prepare_passwords(pairs, token, workers):
    """Bound network/hash concurrency; join every writer before closing a batch.

    The queue contains encrypted row objects and unsaved candidates only. Each
    thread owns its DB connections and decrypts at most one credential at a time.
    A fatal failure stops new work, then waits for in-flight writes to finish.
    """
    pending = Queue()
    stopped = Event()
    for row, candidate in pairs:
        if not row.errors:
            pending.put((row, candidate))

    def consume(threaded):
        issues = []
        try:
            if threaded:
                close_old_connections()
            while not stopped.is_set():
                try:
                    row, candidate = pending.get_nowait()
                except Empty:
                    break
                try:
                    prepare_import_password(row, candidate, validation_token=token)
                except UserImportError as error:
                    if error.code != 'invalid_password':
                        raise
                    issues.append(ImportIssue(error.code, error.message, row.row_number, row.line_number, 'password'))
        except Exception:
            stopped.set()
            raise
        finally:
            if threaded:
                connections.close_all()
        return issues

    if workers == 1:
        return consume(False)
    # Only `workers` futures exist, regardless of the CSV row count. Exiting the
    # context joins all threads even when future.result() raises an exception.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='user-import') as executor:
        futures = [executor.submit(consume, True) for unused in range(min(workers, pending.qsize()))]
        issues = [issue for future in futures for issue in future.result()]
    return sorted(issues, key=lambda issue: issue.row)


@sensitive_variables()
def _validate_user_import(task, batch_id, version):
    token = None
    try:
        config = require_user_import_enabled()
        batch = claim_import_validation(batch_id, version)
        if batch is None:
            return
        token = batch.attempt_token
        batch, actor, rows, issues = resolve_validation_rows(batch_id, version, token)
        if issues:
            pairs = invalid_row_candidates(rows)
        else:
            with transaction.atomic():
                locked_validation(batch_id, version, token)
                pairs = capture_import_snapshot(actor, batch_id, version)
        issues.extend(_prepare_passwords(pairs, token, config.validation_workers))
        if issues:
            finish_import_validation(batch_id, version, token, UserImportBatch.Status.INVALID, issues)
            return
        with transaction.atomic():
            locked_validation(batch_id, version, token)
            finalized = finalize_import_preview(actor, batch_id, version)
            finalized.attempt_token = None
            finalized.lease_expires_at = None
            finalized.save(update_fields=['attempt_token', 'lease_expires_at', 'updated_at'])
    except PermissionDenied:
        if token:
            finish_import_validation(batch_id, version, token, UserImportBatch.Status.FAILED, [ImportIssue(
                'permission_denied', _('The import owner no longer has permission to import users.'))])
    except ImportBatchConflict as error:
        if token:
            finish_import_validation(batch_id, version, token, UserImportBatch.Status.CONFLICT,
                                     error.issues or [ImportIssue(error.code, error.message)])
    except UserImportError as error:
        if token and error.code != 'validation_superseded':
            expired = error.code in ('expired_secret', 'expired_snapshot')
            finish_import_validation(batch_id, version, token,
                                     UserImportBatch.Status.EXPIRED if expired else UserImportBatch.Status.FAILED,
                                     [ImportIssue(error.code, error.message)])
    except OperationalError:
        if token and release_import_validation(batch_id, version, token):
            raise task.retry(countdown=2 ** batch.validation_attempts + random.uniform(0, 1),
                             max_retries=config.validation_attempts - 1) from None
    except DatabaseError:
        # DB may still be unavailable. Keep the claim for bounded recovery/expiry.
        logger.warning('User import validation database unavailable for batch %s', batch_id)
    except Exception:
        logger.warning('User import validation failed for batch %s', batch_id)
        if token:
            finish_import_validation(batch_id, version, token, UserImportBatch.Status.FAILED, [ImportIssue(
                'validation_failed', _('Validation could not finish. Upload the CSV again.'))])
