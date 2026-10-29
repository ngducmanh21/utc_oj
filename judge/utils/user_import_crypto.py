"""Authenticated temporary secrets bound to batch, row, purpose and expiry."""

import json
import uuid
from datetime import datetime, timezone as datetime_timezone

from cryptography.fernet import Fernet, InvalidToken
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from judge.utils.user_import import UserImportError, require_user_import_enabled


def _context(batch_id, row_number, purpose, expires_at):
    if (type(row_number) is not int or row_number < 1 or purpose not in ('password', 'prepared_hash') or
            not isinstance(expires_at, datetime) or timezone.is_naive(expires_at)):
        raise UserImportError('invalid_secret_context', _('Invalid temporary credential context.'))
    try:
        batch_id = str(uuid.UUID(str(batch_id)))
    except (ValueError, TypeError, AttributeError):
        raise UserImportError('invalid_secret_context', _('Invalid temporary credential context.')) from None
    return [batch_id, row_number, purpose, expires_at.astimezone(datetime_timezone.utc).isoformat()]


@sensitive_variables()
def seal_import_secret(value, *, batch_id, row_number, purpose, expires_at, now=None):
    config = require_user_import_enabled()
    now = now or timezone.now()
    context = _context(batch_id, row_number, purpose, expires_at)
    ttl = config.password_seconds if purpose == 'password' else config.prepared_password_seconds
    if not isinstance(value, str) or not 0 < (expires_at - now).total_seconds() <= ttl:
        raise UserImportError('expired_secret', _('The temporary credential expired. Upload the CSV again.'))
    payload = {'context': context, 'value': value}
    token = Fernet(config.keys[config.active_key_id].encode()).encrypt_at_time(
        json.dumps(payload, ensure_ascii=False).encode(), int(now.timestamp()),
    ).decode()
    return config.active_key_id + ':' + token


@sensitive_variables()
def open_import_secret(ciphertext, *, batch_id, row_number, purpose, expires_at, now=None):
    config = require_user_import_enabled()
    now = now or timezone.now()
    if expires_at is None or timezone.is_naive(expires_at) or now >= expires_at:
        raise UserImportError('expired_secret', _('The temporary credential expired. Upload the CSV again.'))
    context = _context(batch_id, row_number, purpose, expires_at)
    ttl = config.password_seconds if purpose == 'password' else config.prepared_password_seconds
    try:
        key_id, token = ciphertext.split(':', 1)
        key = config.keys[key_id]
        raw = Fernet(key.encode()).decrypt_at_time(token.encode(), ttl, int(now.timestamp()))
        payload = json.loads(raw)
        if payload['context'] != context or not isinstance(payload['value'], str):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, InvalidToken):
        raise UserImportError('invalid_secret', _('The temporary credential could not be verified.')) from None
    return payload['value']
