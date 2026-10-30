"""Confirmation, execution claims and reconciliation against the primary DB.

A lock on the same batch row used by the account transaction is the evidence:
COMPLETED means committed; RUNNING under a new lock means that transaction is
gone. A timeout, lease or broker ACK alone never establishes either outcome.
"""

import logging
import random
import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, OperationalError, connections, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext_lazy as _

from judge.models import UserImportBatch, UserImportOperation, UserImportRow
from judge.utils.user_import import ImportIssue, UserImportError, get_or_create_import_operation, \
    import_request_digest, require_user_import_enabled
from judge.utils.user_import_config import get_user_import_config
from judge.utils.user_import_service import ImportBatchConflict, invalidate_import_caches, verify_import_submission
from judge.utils.user_import_validation import ACTIVE_IMPORTS, clear_import_secrets

logger = logging.getLogger(__name__)
LOCK_BUSY = (1205, 3572)
RETRYABLE_DATABASE_ERRORS = (1040, 1042, 1205, 1213, 2006, 2013, 2055)
TERMINAL_IMPORTS = (UserImportBatch.Status.COMPLETED, UserImportBatch.Status.INVALID,
                    UserImportBatch.Status.CONFLICT, UserImportBatch.Status.FAILED, UserImportBatch.Status.EXPIRED)


def database_error_code(error):
    return error.args[0] if error.args and type(error.args[0]) is int else None


def _actor(actor):
    if getattr(actor, 'is_impersonate', False):
        raise PermissionDenied()
    current = User.objects.filter(pk=actor.pk, is_active=True, is_staff=True, is_superuser=True).first()
    if current is None:
        raise PermissionDenied()
    return current


def reserve_import_submission(actor, batch_id, version, key):
    """Only PREVIEW_READY can transition; HTTP replay never resets attempts."""
    require_user_import_enabled()
    current_actor = _actor(actor)
    batch = UserImportBatch.objects.filter(pk=batch_id, actor_id=current_actor.pk).first()
    if batch is None:
        raise UserImportError('not_found', _('Import request not found.'))
    if batch.version != version or batch.idempotency_key != key:
        raise ImportBatchConflict('batch_conflict')
    digest = import_request_digest('submit', {'batch_id': str(batch.pk), 'version': version, 'confirmed': True})
    operation = batch.operations.filter(actor=current_actor, action='submit', idempotency_key=key).first()
    if operation:
        if not constant_time_compare(operation.request_digest, digest):
            raise ImportBatchConflict('idempotency_conflict')
        return batch, True
    if batch.status == UserImportBatch.Status.COMPLETED:
        return batch, True
    if batch.status == UserImportBatch.Status.INVALID or batch.errors:
        raise UserImportError('invalid_preview', _('Correct the CSV errors before importing.'))
    if batch.status != UserImportBatch.Status.PREVIEW_READY:
        raise ImportBatchConflict('preview_not_ready')
    try:
        with transaction.atomic():
            batch = UserImportBatch.objects.select_for_update(nowait=True).get(pk=batch.pk)
            # A concurrent identical submit may have committed while we read it.
            operation = batch.operations.filter(actor=current_actor, action='submit', idempotency_key=key).first()
            if operation:
                if not constant_time_compare(operation.request_digest, digest):
                    raise ImportBatchConflict('idempotency_conflict')
                return batch, True
            batch = verify_import_submission(current_actor, batch.pk, version)
            # verify_import_submission holds the actor lock. Upload admission uses
            # that same actor lock, so two different batches cannot both start.
            if UserImportBatch.objects.filter(actor=current_actor, status__in=ACTIVE_IMPORTS).exclude(
                    pk=batch.pk).exists():
                raise UserImportError('active_batch', _('Wait for your active import to finish validating or running.'))

            def accept():
                batch.status = UserImportBatch.Status.QUEUED
                batch.attempt_token = None
                batch.lease_expires_at = None
                batch.task_id = ''
                batch.save(update_fields=['status', 'attempt_token', 'lease_expires_at', 'task_id', 'updated_at'])
                return batch

            get_or_create_import_operation(current_actor, UserImportOperation.Action.SUBMIT, key, digest, accept)
            return batch, False
    except OperationalError as error:
        if database_error_code(error) in LOCK_BUSY:
            message = _('The import is being confirmed. Check its status and retry.')
            raise UserImportError('batch_busy', message) from None
        raise


def close_import_execution(batch, status, issues):
    """Caller holds the batch lock and has established no account commit exists."""
    if batch.status not in (UserImportBatch.Status.QUEUED, UserImportBatch.Status.RUNNING):
        return
    now = timezone.now()
    batch.status = status
    batch.errors = [issue.as_dict() for issue in issues]
    batch.finished_at = now
    batch.attempt_token = None
    batch.lease_expires_at = None
    batch.task_id = ''
    batch.save(update_fields=['status', 'errors', 'finished_at', 'attempt_token', 'lease_expires_at',
                              'task_id', 'updated_at'])
    clear_import_secrets(batch)
    batch.rows.update(status=UserImportRow.Status.NOT_APPLIED, finished_at=now)
    by_row = {}
    for issue in issues:
        if issue.row:
            by_row.setdefault(issue.row, []).append(issue.as_dict())
    for row in batch.rows.filter(row_number__in=by_row):
        row.errors = by_row[row.row_number]
        row.save(update_fields=['errors'])


def claim_import_execution(batch_id, version):
    config = require_user_import_enabled()
    try:
        with transaction.atomic():
            batch = UserImportBatch.objects.select_for_update(nowait=True).filter(pk=batch_id, version=version).first()
            if batch is None or batch.status != UserImportBatch.Status.QUEUED:
                return
            now = timezone.now()
            if batch.lease_expires_at and now < batch.lease_expires_at:
                return
            if now >= batch.secret_expires_at:
                close_import_execution(batch, UserImportBatch.Status.EXPIRED, [ImportIssue(
                    'expired_secret', _('Temporary credentials expired. Upload the CSV again.'))])
                return
            if batch.execution_attempts >= config.max_attempts:
                close_import_execution(batch, UserImportBatch.Status.FAILED, [ImportIssue(
                    'attempts_exhausted', _('Import attempts were exhausted. No accounts were changed.'))])
                return
            if not batch.operations.filter(action='submit', actor_id=batch.actor_id,
                                           idempotency_key=batch.idempotency_key).exists():
                close_import_execution(batch, UserImportBatch.Status.FAILED, [ImportIssue(
                    'confirmation_missing', _('This import has no valid confirmation. Upload the CSV again.'))])
                return
            actor = User.objects.select_related('profile').get(pk=batch.actor_id)
            profile = getattr(actor, 'profile', None)
            if (not actor.is_active or not actor.is_staff or not actor.is_superuser or
                    profile is None or profile.is_banned):
                close_import_execution(batch, UserImportBatch.Status.FAILED, [ImportIssue(
                    'permission_denied', _('The import owner no longer has permission to import users.'))])
                return
            batch.execution_attempts += 1
            batch.attempt_token = uuid.uuid4()
            batch.lease_expires_at = now + timedelta(seconds=config.execution_lease_seconds)
            # status is written with the claim, never first inside the account tx.
            batch.status = UserImportBatch.Status.RUNNING
            batch.save(update_fields=['status', 'execution_attempts', 'attempt_token',
                                      'lease_expires_at', 'updated_at'])
            return batch, actor
    except OperationalError as error:
        if database_error_code(error) in LOCK_BUSY:
            return
        raise


def execution_failure(error):
    """Convert exceptions to safe report data, excluding DB/validator messages."""
    if isinstance(error, ImportBatchConflict):
        expired = error.code in ('expired_secret', 'expired_snapshot')
        return (UserImportBatch.Status.EXPIRED if expired else UserImportBatch.Status.CONFLICT,
                error.issues or [ImportIssue(error.code, error.message)], False)
    if isinstance(error, PermissionDenied):
        return UserImportBatch.Status.FAILED, [ImportIssue(
            'permission_denied', _('The import owner no longer has permission to import users.'))], False
    if isinstance(error, UserImportError) and error.code in ('expired_secret', 'expired_snapshot'):
        return UserImportBatch.Status.EXPIRED, [ImportIssue(
            'expired_secret', _('Temporary credentials expired. Upload the CSV again.'))], False
    row, line = getattr(error, 'import_row', (0, 0))
    issue = ImportIssue('execution_failed', _('The import failed. No accounts were changed.'), row, line)
    retryable = isinstance(error, DatabaseError) and database_error_code(error) in RETRYABLE_DATABASE_ERRORS
    return UserImportBatch.Status.FAILED, [issue], retryable


def reconcile_import_execution(batch_id, version, *, token=None, failure=None):
    """Use a fresh primary connection after an executor exception.

    Recovery calls this without token and only fences expired claims. If the
    account transaction still owns the row, NOWAIT returns busy without touching
    the token, budget, secrets or marker. Database unavailability stays unknown.
    """
    if connections['default'].in_atomic_block:
        # Re-entrant locking inside an account transaction cannot establish the
        # outcome of that same transaction. Recovery must use its own boundary.
        return 'unknown'
    config = get_user_import_config()
    try:
        with transaction.atomic(using='default'):
            batch = (UserImportBatch.objects.using('default').select_for_update(nowait=True)
                     .filter(pk=batch_id, version=version).first())
            if batch is None:
                return 'missing'
            if batch.status == UserImportBatch.Status.COMPLETED:
                return 'completed'
            if batch.status != UserImportBatch.Status.RUNNING:
                return batch.status.lower()
            if token is not None and batch.attempt_token != token:
                return 'superseded'
            now = timezone.now()
            if (token is None and now < batch.secret_expires_at and batch.lease_expires_at and
                    now < batch.lease_expires_at):
                return 'active'
            # Holding this lock and seeing RUNNING proves no account commit was
            # made by the preceding transaction. Fence it before any new claim.
            if now >= batch.secret_expires_at:
                close_import_execution(batch, UserImportBatch.Status.EXPIRED, [ImportIssue(
                    'expired_secret', _('Temporary credentials expired. Upload the CSV again.'))])
                return 'expired'
            if failure is None:
                status, issues, retryable = UserImportBatch.Status.FAILED, [ImportIssue(
                    'worker_interrupted', _('The worker stopped. The whole import will be retried.'))], True
            else:
                status, issues, retryable = failure
            if retryable and batch.execution_attempts < config.max_attempts and config.enabled:
                batch.status = UserImportBatch.Status.QUEUED
                batch.attempt_token = None
                batch.task_id = ''
                delay = min(60, 2 ** batch.execution_attempts) + random.uniform(0, 1)
                batch.lease_expires_at = now + timedelta(seconds=delay)
                batch.errors = []
                batch.save(update_fields=['status', 'attempt_token', 'task_id', 'lease_expires_at',
                                          'errors', 'updated_at'])
                return 'queued'
            if retryable and not config.enabled and batch.execution_attempts < config.max_attempts:
                # Preserve a resolved rollback until the feature is resumed or
                # credentials expire. No execution is dispatched while disabled.
                batch.status = UserImportBatch.Status.QUEUED
                batch.attempt_token = None
                batch.lease_expires_at = None
                batch.task_id = ''
                batch.save(update_fields=['status', 'attempt_token', 'lease_expires_at', 'task_id', 'updated_at'])
                return 'queued'
            if retryable:
                issues = [ImportIssue('attempts_exhausted',
                                      _('Import attempts were exhausted. No accounts were changed.'))]
            close_import_execution(batch, status, issues)
            return status.lower()
    except OperationalError as error:
        if database_error_code(error) in LOCK_BUSY:
            return 'busy'
        logger.warning('User import outcome could not be checked for batch %s', batch_id)
        return 'unknown'
    except DatabaseError:
        logger.warning('User import outcome could not be checked for batch %s', batch_id)
        return 'unknown'


def repair_import_cache(batch_id, version):
    """Cache repair is independent of account execution and its attempt budget."""
    batch = UserImportBatch.objects.filter(pk=batch_id, version=version, status='COMPLETED').first()
    if batch is None:
        return
    failed = False
    try:
        invalidate_import_caches(batch.pk)
    except Exception:
        failed = True
        logger.warning('User import cache repair pending for batch %s', batch_id)
    with transaction.atomic():
        current = UserImportBatch.objects.select_for_update().get(pk=batch.pk)
        if current.status != UserImportBatch.Status.COMPLETED or current.version != version:
            return
        errors = [item for item in current.errors if item.get('code') != 'cache_repair_pending']
        if failed:
            errors.append(ImportIssue('cache_repair_pending',
                                      _('Accounts were saved. Display caches are waiting for repair.')).as_dict())
        if errors != current.errors:
            current.errors = errors
            current.save(update_fields=['errors', 'updated_at'])
