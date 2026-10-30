"""Store validated import metadata and replace temporary plaintext with a hash.

These helpers write importer tables only. The account upsert is a later milestone.
"""

from datetime import timedelta

from django.contrib.auth.hashers import identify_hasher, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from judge.models import UserImportBatch, UserImportRow
from judge.utils.user_import import UserImportError, require_user_import_enabled, validate_import_accounts
from judge.utils.user_import_crypto import open_import_secret, seal_import_secret


@sensitive_variables()
def store_import_rows(batch, parsed, *, now=None):
    config = require_user_import_enabled()
    parsed.require_valid()
    validated = validate_import_accounts(parsed)
    validated.require_valid()
    now = now or timezone.now()
    password_expiry = batch.created_at + timedelta(seconds=config.password_seconds)
    if now >= password_expiry or now >= batch.secret_expires_at:
        raise UserImportError('expired_secret', _('The temporary credential expired. Upload the CSV again.'))
    with transaction.atomic():
        current = UserImportBatch.objects.select_for_update().get(pk=batch.pk)
        if current.status != UserImportBatch.Status.VALIDATING or current.rows.exists():
            raise UserImportError('batch_conflict', _('The batch already has rows or is no longer validating.'))
        rows = []
        for source in validated.rows:
            row = UserImportRow(
                batch=current, row_number=source.row_number, line_number=source.line_number,
                username=source.username, target_id=source.target_id,
                action=UserImportRow.Action.UPDATE if source.target_id else UserImportRow.Action.CREATE,
                input_data=source.metadata(),
            )
            if source.password:
                row.password_ciphertext = seal_import_secret(
                    source.password, batch_id=current.pk, row_number=source.row_number, purpose='password',
                    expires_at=password_expiry, now=now,
                )
                row.password_expires_at = password_expiry
            row.save()
            rows.append(row)
        current.total_rows = len(rows)
        current.save(update_fields=['total_rows', 'updated_at'])
    return rows


@sensitive_variables()
def get_import_password(row, *, now=None):
    require_user_import_enabled()
    if not row.password_ciphertext:
        return None
    return open_import_secret(row.password_ciphertext, batch_id=row.batch_id, row_number=row.row_number,
                              purpose='password', expires_at=row.password_expires_at, now=now)


@sensitive_variables()
def get_prepared_import_password(row, *, now=None):
    require_user_import_enabled()
    if not row.prepared_password_ciphertext:
        return None
    encoded = open_import_secret(row.prepared_password_ciphertext, batch_id=row.batch_id, row_number=row.row_number,
                                 purpose='prepared_hash', expires_at=row.prepared_password_expires_at, now=now)
    try:
        identify_hasher(encoded)
    except ValueError:
        raise UserImportError('invalid_secret', _('The prepared password hash is invalid.')) from None
    return encoded


@sensitive_variables()
def prepare_import_password(row, candidate_user, *, now=None, validation_token=None):
    """Validate/hash outside locks, then swap the row secret atomically.

    candidate_user is the unsaved post-import account state. This function never
    saves it. The later validator/executor must also check the account baseline.
    """
    require_user_import_enabled()
    if candidate_user.username != row.username or candidate_user.pk != row.target_id:
        raise UserImportError('invalid_candidate', _('Password validation needs the matching account.'))
    password = get_import_password(row, now=now)
    if password is None:
        if row.prepared_password_ciphertext:
            return get_prepared_import_password(row, now=now)
        if row.input_data.get('password_provided'):
            raise UserImportError('missing_password_secret', _('Upload the CSV again to validate this password.'))
        return None
    invalid = False
    try:
        validate_password(password, user=candidate_user)
    except ValidationError:
        invalid = True
    encoded = None if invalid else make_password(password)
    # No network/expensive hash work occurs while holding the row lock.
    current_time = now or timezone.now()
    with transaction.atomic():
        if validation_token is not None:
            from judge.utils.user_import_validation import locked_validation
            locked_validation(row.batch_id, row.batch.version, validation_token)
        else:
            UserImportBatch.objects.select_for_update().get(pk=row.batch_id)
        current = UserImportRow.objects.select_for_update().select_related('batch').get(pk=row.pk)
        if (current.batch.status != UserImportBatch.Status.VALIDATING or
                current.password_ciphertext != row.password_ciphertext or current.input_data != row.input_data or
                current.target_id != row.target_id):
            raise UserImportError('row_conflict', _('The import row changed during password validation.'))
        # Recheck expiry after the potentially slow validation/hash work.
        get_import_password(current, now=current_time)
        current.password_ciphertext = ''
        current.password_expires_at = None
        current.prepared_password_ciphertext = ''
        current.prepared_password_expires_at = None
        fields = ['password_ciphertext', 'password_expires_at',
                  'prepared_password_ciphertext', 'prepared_password_expires_at']
        if invalid:
            current.status = UserImportRow.Status.INVALID
            current.errors = [{'code': 'invalid_password', 'field': 'password', 'row': current.row_number,
                               'line': current.line_number,
                               'message': str(_('The password does not satisfy the configured password rules.'))}]
            fields.extend(['status', 'errors'])
        if encoded is not None:
            expiry = current.batch.secret_expires_at
            current.prepared_password_ciphertext = seal_import_secret(
                encoded, batch_id=current.batch_id, row_number=current.row_number, purpose='prepared_hash',
                expires_at=expiry, now=current_time,
            )
            current.prepared_password_expires_at = expiry
        current.save(update_fields=fields)
    if invalid:
        raise UserImportError('invalid_password', _('The password does not satisfy the configured password rules.'))
    return encoded
