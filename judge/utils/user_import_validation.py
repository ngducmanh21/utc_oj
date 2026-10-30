"""Reserve uploads and coordinate bounded validation attempts; no account writes."""

import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from judge.models import UserImportBatch, UserImportOperation, UserImportRow
from judge.utils.user_import import ImportIssue, ImportRow, ParsedImport, UserImportError, \
    get_or_create_import_operation, parse_user_csv, require_user_import_enabled, validate_import_accounts
from judge.utils.user_import_crypto import seal_import_secret

ACTIVE_IMPORTS = (UserImportBatch.Status.VALIDATING, UserImportBatch.Status.QUEUED, UserImportBatch.Status.RUNNING)


def clear_import_secrets(batch):
    batch.rows.update(password_ciphertext='', password_expires_at=None,
                      prepared_password_ciphertext='', prepared_password_expires_at=None)


def close_validation(batch, status, issues=()):
    """Caller holds the batch lock. Never close an execution or a completed batch."""
    if batch.status != UserImportBatch.Status.VALIDATING:
        return
    batch.status = status
    batch.errors = [issue.as_dict() for issue in issues]
    batch.finished_at = timezone.now()
    batch.lease_expires_at = None
    batch.attempt_token = None
    batch.save(update_fields=['status', 'errors', 'finished_at', 'lease_expires_at', 'attempt_token', 'updated_at'])
    clear_import_secrets(batch)
    batch.rows.exclude(status=UserImportRow.Status.INVALID).update(status=UserImportRow.Status.NOT_APPLIED)


def expire_import_preview(batch, *, now=None):
    """Caller holds the batch lock; running executions are left to M4 recovery."""
    now = now or timezone.now()
    if batch.status == UserImportBatch.Status.VALIDATING:
        deadline = min(batch.secret_expires_at, batch.created_at + timedelta(
            seconds=require_user_import_enabled().password_seconds))
        if now >= deadline:
            close_validation(batch, UserImportBatch.Status.EXPIRED, [ImportIssue(
                'expired_secret', _('Temporary credentials expired. Upload the CSV again.'))])
    elif batch.status == UserImportBatch.Status.PREVIEW_READY and now >= batch.preview_expires_at:
        batch.status = UserImportBatch.Status.EXPIRED
        batch.finished_at = now
        batch.save(update_fields=['status', 'finished_at', 'updated_at'])
        clear_import_secrets(batch)


@sensitive_variables()
@transaction.atomic
def reserve_import_upload(actor, key, digest, data, muted):
    config = require_user_import_enabled()
    # Match the worker's batch -> actor lock order when expiring old admissions.
    deadline = timezone.now() - timedelta(seconds=config.password_seconds)
    for active in UserImportBatch.objects.select_for_update().filter(
            actor_id=actor.pk, status=UserImportBatch.Status.VALIDATING, created_at__lte=deadline).order_by('pk'):
        expire_import_preview(active)
    # Serialize admission across different keys for one actor, without a site-wide lock.
    current_actor = User.objects.select_for_update().get(pk=actor.pk)
    if (not current_actor.is_active or not current_actor.is_staff or not current_actor.is_superuser or
            getattr(actor, 'is_impersonate', False)):
        raise PermissionDenied()

    def create_batch():
        if UserImportBatch.objects.filter(actor=current_actor, status__in=ACTIVE_IMPORTS).exists():
            raise UserImportError('active_batch', _('Wait for your active import to finish validating or running.'))
        parsed = parse_user_csv(data)
        if parsed.issues:
            error = UserImportError('invalid_csv', _('Correct the CSV errors before importing.'))
            error.issues = parsed.issues
            raise error
        now = timezone.now()
        batch = UserImportBatch.objects.create(
            actor=current_actor, idempotency_key=key, muted=muted,
            preview_expires_at=now + timedelta(seconds=config.preview_seconds),
            secret_expires_at=now + timedelta(seconds=config.prepared_password_seconds),
            total_rows=len(parsed.rows),
        )
        password_expiry = now + timedelta(seconds=config.password_seconds)
        for source in parsed.rows:
            row = UserImportRow(batch=batch, row_number=source.row_number, line_number=source.line_number,
                                username=source.username, input_data=source.metadata())
            if source.password:
                row.password_ciphertext = seal_import_secret(
                    source.password, batch_id=batch.pk, row_number=source.row_number, purpose='password',
                    expires_at=password_expiry, now=now,
                )
                row.password_expires_at = password_expiry
            row.save()
        return batch

    return get_or_create_import_operation(current_actor, UserImportOperation.Action.UPLOAD, key, digest, create_batch)


@transaction.atomic
def claim_import_validation(batch_id, version):
    config = require_user_import_enabled()
    batch = UserImportBatch.objects.select_for_update().filter(pk=batch_id, version=version).first()
    if batch is None:
        return
    expire_import_preview(batch)
    if batch.status != UserImportBatch.Status.VALIDATING:
        return
    now = timezone.now()
    if batch.lease_expires_at and batch.lease_expires_at > now:
        return
    if batch.validation_attempts >= config.validation_attempts:
        close_validation(batch, UserImportBatch.Status.FAILED, [ImportIssue(
            'validation_failed', _('Validation could not finish. Upload the CSV again.'))])
        return
    batch.validation_attempts += 1
    batch.attempt_token = uuid.uuid4()
    # A validation lease never extends the lifetime of recoverable passwords.
    batch.lease_expires_at = min(batch.secret_expires_at, batch.created_at + timedelta(
        seconds=config.password_seconds))
    batch.save(update_fields=['validation_attempts', 'attempt_token', 'lease_expires_at', 'updated_at'])
    return batch


def locked_validation(batch_id, version, token):
    """Call inside a short atomic block, including the following metadata write."""
    require_user_import_enabled()
    batch = UserImportBatch.objects.select_for_update().get(pk=batch_id)
    if (batch.version != version or batch.status != UserImportBatch.Status.VALIDATING or
            batch.attempt_token != token or not batch.lease_expires_at or
            timezone.now() >= batch.lease_expires_at):
        raise UserImportError('validation_superseded', _('This validation attempt is no longer current.'))
    return batch


@transaction.atomic
def resolve_validation_rows(batch_id, version, token):
    batch = locked_validation(batch_id, version, token)
    actor = User.objects.select_for_update().get(pk=batch.actor_id)
    if not actor.is_active or not actor.is_staff or not actor.is_superuser:
        raise PermissionDenied()
    rows = list(batch.rows.select_for_update())
    sources = tuple(ImportRow(row.row_number, row.line_number, row.username,
                              'provided' if row.input_data['password_provided'] else '',
                              row.input_data['name'], row.input_data['school'], row.input_data['email'],
                              tuple(row.input_data['organizations']), row.input_data['password_rotation'])
                    for row in rows)
    checked = validate_import_accounts(ParsedImport(sources))
    issues = list(checked.issues)
    issues.extend(ImportIssue(**issue) for row in rows for issue in row.errors)
    for row, source in zip(rows, checked.rows):
        action = UserImportRow.Action.UPDATE if source.target_id else UserImportRow.Action.CREATE
        if row.action != UserImportRow.Action.UNKNOWN and (row.target_id != source.target_id or row.action != action):
            issues.append(ImportIssue('username_conflict', _('An account changed. Upload and preview the CSV again.'),
                                      row.row_number, row.line_number, 'username'))
        elif row.action == UserImportRow.Action.UNKNOWN:
            row.target_id, row.action = source.target_id, action
            row.save(update_fields=['target', 'action'])
    return batch, actor, rows, issues


def invalid_row_candidates(rows):
    """Still check every provided password when other account fields are invalid."""
    users = User.objects.in_bulk(row.target_id for row in rows if row.target_id)
    pairs = []
    for row in rows:
        user = users.get(row.target_id)
        values = {field: row.input_data[name] or (getattr(user, field) if user else '')
                  for name, field in (('name', 'first_name'), ('school', 'last_name'), ('email', 'email'))}
        pairs.append((row, User(pk=row.target_id, username=row.username, **values)))
    return pairs


@transaction.atomic
def finish_import_validation(batch_id, version, token, status, issues):
    batch = UserImportBatch.objects.select_for_update().get(pk=batch_id)
    if (batch.version != version or batch.status != UserImportBatch.Status.VALIDATING or
            batch.attempt_token != token):
        return
    by_row = {}
    for issue in issues:
        by_row.setdefault(issue.row, []).append(issue.as_dict())
    for row in batch.rows.select_for_update():
        if row.row_number in by_row:
            row.status = UserImportRow.Status.INVALID
            row.errors = by_row[row.row_number]
            row.save(update_fields=['status', 'errors'])
    close_validation(batch, status, issues)


@transaction.atomic
def release_import_validation(batch_id, version, token):
    """A retryable validation failure has no account commit to reconcile."""
    batch = UserImportBatch.objects.select_for_update().get(pk=batch_id)
    if (batch.version != version or batch.status != UserImportBatch.Status.VALIDATING or
            batch.attempt_token != token):
        return False
    expire_import_preview(batch)
    if batch.status != UserImportBatch.Status.VALIDATING:
        return False
    if batch.validation_attempts >= require_user_import_enabled().validation_attempts:
        close_validation(batch, UserImportBatch.Status.FAILED, [ImportIssue(
            'validation_failed', _('Validation could not finish. Upload the CSV again.'))])
        return False
    batch.attempt_token = None
    batch.lease_expires_at = None
    batch.task_id = ''
    batch.save(update_fields=['attempt_token', 'lease_expires_at', 'task_id', 'updated_at'])
    return True
