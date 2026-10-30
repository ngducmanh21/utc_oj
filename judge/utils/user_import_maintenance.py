"""Bounded recovery and retention; operation/batch tombstones are never deleted."""

from datetime import timedelta

from django.db import OperationalError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from judge.models import UserImportBatch, UserImportRow
from judge.utils.user_import import ImportIssue
from judge.utils.user_import_config import get_user_import_config
from judge.utils.user_import_execution import LOCK_BUSY, TERMINAL_IMPORTS, close_import_execution, \
    database_error_code, reconcile_import_execution, repair_import_cache
from judge.utils.user_import_validation import clear_import_secrets, close_validation, expire_import_preview


def _recover_pending(batch_id):
    config = get_user_import_config()
    try:
        with transaction.atomic():
            batch = UserImportBatch.objects.select_for_update(nowait=True).get(pk=batch_id)
            expire_import_preview(batch)
            if batch.status not in ('VALIDATING', 'QUEUED'):
                return batch.status.lower(), batch.version
            now = timezone.now()
            if batch.status == 'QUEUED' and now >= batch.secret_expires_at:
                close_import_execution(batch, UserImportBatch.Status.EXPIRED, [ImportIssue(
                    'expired_secret', _('Temporary credentials expired. Upload the CSV again.'))])
                return 'expired', batch.version
            if not config.enabled:
                return 'disabled', batch.version
            if batch.lease_expires_at and now < batch.lease_expires_at:
                return 'active', batch.version
            if batch.status == 'VALIDATING' and batch.validation_attempts >= config.validation_attempts:
                close_validation(batch, UserImportBatch.Status.FAILED, [ImportIssue(
                    'validation_failed', _('Validation could not finish. Upload the CSV again.'))])
                return 'failed', batch.version
            if (batch.attempt_token is None and batch.task_id and
                    batch.updated_at > now - timedelta(seconds=config.dispatch_seconds)):
                return 'active', batch.version
            batch.task_id = ''
            batch.attempt_token = None
            batch.lease_expires_at = None
            batch.save(update_fields=['task_id', 'attempt_token', 'lease_expires_at', 'updated_at'])
            return 'validate' if batch.status == 'VALIDATING' else 'execute', batch.version
    except OperationalError as error:
        if database_error_code(error) in LOCK_BUSY:
            return 'busy', None
        raise


def recover_import_batches(*, limit=100, batch_id=None, dry_run=False):
    from judge.tasks.user_import import dispatch_import_execution, dispatch_import_validation
    config = get_user_import_config()
    now = timezone.now()
    raw_deadline = now - timedelta(seconds=config.password_seconds)
    delivery_deadline = now - timedelta(seconds=config.dispatch_seconds)
    due = (Q(status='RUNNING') & (Q(lease_expires_at__lte=now) | Q(secret_expires_at__lte=now)) |
           Q(status='VALIDATING') & (Q(created_at__lte=raw_deadline) | Q(lease_expires_at__lte=now) |
                                     Q(attempt_token__isnull=True, task_id='') | Q(updated_at__lte=delivery_deadline)) |
           Q(status='QUEUED') & (Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)) &
           (Q(task_id='') | Q(updated_at__lte=delivery_deadline)) |
           Q(status='COMPLETED') & ~Q(errors=[]))
    candidates = UserImportBatch.objects.filter(due).order_by('updated_at', 'pk')
    if batch_id is not None:
        candidates = candidates.filter(pk=batch_id)
    items = list(candidates.values_list('pk', 'version', 'status')[:limit])
    result = {'candidates': len(items), 'dispatched': 0, 'completed': 0, 'closed': 0, 'pending': 0}
    if dry_run:
        return result
    for key, version, state in items:
        if state == 'COMPLETED':
            repair_import_cache(key, version)
            result['completed'] += 1
            continue
        if state == 'RUNNING':
            outcome = reconcile_import_execution(key, version)
            if outcome == 'completed':
                repair_import_cache(key, version)
                result['completed'] += 1
                continue
            if outcome == 'queued' and config.enabled:
                if dispatch_import_execution(key, version):
                    result['dispatched'] += 1
                else:
                    result['pending'] += 1
            elif outcome in ('failed', 'expired', 'conflict'):
                result['closed'] += 1
            else:
                result['pending'] += 1
            continue
        outcome, version = _recover_pending(key)
        if outcome in ('validate', 'execute'):
            dispatch = dispatch_import_validation if outcome == 'validate' else dispatch_import_execution
            if dispatch(key, version):
                result['dispatched'] += 1
            else:
                result['pending'] += 1
        elif outcome in ('failed', 'expired', 'conflict', 'invalid'):
            result['closed'] += 1
        else:
            result['pending'] += 1
    return result


def cleanup_import_batches(*, limit=100, dry_run=False):
    config = get_user_import_config()
    now = timezone.now()
    report_deadline = now - timedelta(seconds=config.report_seconds)
    rows = UserImportRow.objects.filter(batch_id=OuterRef('pk'))
    secrets = rows.exclude(password_ciphertext='', prepared_password_ciphertext='')
    candidates = UserImportBatch.objects.annotate(has_rows=Exists(rows), has_secrets=Exists(secrets))
    expired = (Q(status='VALIDATING', created_at__lte=now - timedelta(seconds=config.password_seconds)) |
               Q(status='PREVIEW_READY', preview_expires_at__lte=now) |
               Q(status__in=('QUEUED', 'RUNNING'), secret_expires_at__lte=now))
    outdated = Q(finished_at__lte=report_deadline) & (Q(has_rows=True) | ~Q(snapshot_digest='') | ~Q(errors=[]))
    retained = Q(status__in=TERMINAL_IMPORTS) & (Q(has_secrets=True) | outdated)
    candidates = candidates.filter(expired | retained).order_by('updated_at', 'pk')
    ids = list(candidates.values_list('pk', flat=True)[:limit])
    result = {'candidates': len(ids), 'expired': 0, 'secrets_cleared': 0, 'reports_removed': 0, 'busy': 0}
    if dry_run:
        return result
    for batch_id in ids:
        # Never use TTL to guess the outcome of an execution transaction.
        peek = UserImportBatch.objects.get(pk=batch_id)
        if peek.status == 'RUNNING':
            outcome = reconcile_import_execution(batch_id, peek.version)
            if outcome in ('busy', 'unknown', 'active'):
                result['busy'] += 1
                continue
        with transaction.atomic():
            batch = UserImportBatch.objects.select_for_update(skip_locked=True).filter(pk=batch_id).first()
            if batch is None:
                result['busy'] += 1
                continue
            before = batch.status
            expire_import_preview(batch)
            if batch.status == 'QUEUED' and timezone.now() >= batch.secret_expires_at:
                close_import_execution(batch, UserImportBatch.Status.EXPIRED, [ImportIssue(
                    'expired_secret', _('Temporary credentials expired. Upload the CSV again.'))])
            if before != batch.status and batch.status == 'EXPIRED':
                result['expired'] += 1
            if batch.status not in TERMINAL_IMPORTS:
                continue
            if batch.rows.exclude(password_ciphertext='', prepared_password_ciphertext='').exists():
                clear_import_secrets(batch)
                result['secrets_cleared'] += 1
            if batch.finished_at and batch.finished_at <= report_deadline:
                batch.rows.all().delete()
                batch.errors = []
                batch.snapshot_digest = ''
                batch.save(update_fields=['errors', 'snapshot_digest', 'updated_at'])
                result['reports_removed'] += 1
    return result
