"""Validation tasks carry IDs only; importer results stay behind the owner API."""

import logging
import random
import uuid

from celery import shared_task
from celery.exceptions import Retry
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, OperationalError, transaction
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from judge.models import UserImportBatch
from judge.utils.user_import import ImportIssue, UserImportError, require_user_import_enabled
from judge.utils.user_import_service import ImportBatchConflict, capture_import_snapshot, finalize_import_preview
from judge.utils.user_import_storage import prepare_import_password
from judge.utils.user_import_validation import claim_import_validation, finish_import_validation, \
    invalid_row_candidates, locked_validation, release_import_validation, resolve_validation_rows

__all__ = ('validate_user_import',)
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
        for row, candidate in pairs:
            if row.errors:
                continue
            try:
                prepare_import_password(row, candidate, validation_token=token)
            except UserImportError as error:
                if error.code != 'invalid_password':
                    raise
                issues.append(ImportIssue(error.code, error.message, row.row_number, row.line_number, 'password'))
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
