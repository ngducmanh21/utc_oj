"""Configuration shared by importer checks and the opt-in import services."""

import base64
import re
from dataclasses import dataclass, field

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class UserImportConfig:
    enabled: bool
    max_bytes: int
    max_rows: int
    max_password_length: int
    preview_seconds: int
    password_seconds: int
    prepared_password_seconds: int
    report_seconds: int
    max_attempts: int
    validation_attempts: int
    active_key_id: str
    keys: dict = field(repr=False)
    digest_key: bytes = field(repr=False)


def _key(value, setting):
    try:
        if not isinstance(value, str) or len(value) != 44:
            raise ValueError()
        raw = base64.b64decode(value, altchars=b'-_', validate=True)
        if len(raw) != 32:
            raise ValueError()
        Fernet(value.encode('ascii'))
    except (ValueError, TypeError, UnicodeError):
        raise ImproperlyConfigured('%s must be a URL-safe base64 encoded 32-byte key.' % setting) from None
    return raw


def get_user_import_config():
    enabled = settings.DMOJ_USER_IMPORT_ENABLED
    if type(enabled) is not bool:
        raise ImproperlyConfigured('DMOJ_USER_IMPORT_ENABLED must be a boolean.')
    numbers = {}
    for name in ('MAX_BYTES', 'MAX_ROWS', 'MAX_PASSWORD_LENGTH', 'PREVIEW_SECONDS', 'PASSWORD_SECONDS',
                 'PREPARED_PASSWORD_SECONDS', 'REPORT_SECONDS', 'MAX_ATTEMPTS', 'VALIDATION_ATTEMPTS'):
        value = getattr(settings, 'DMOJ_USER_IMPORT_' + name)
        if type(value) is not int or value <= 0:
            raise ImproperlyConfigured('DMOJ_USER_IMPORT_%s must be a positive integer.' % name)
        numbers[name.lower()] = value
    if not numbers['password_seconds'] <= numbers['preview_seconds'] <= numbers['prepared_password_seconds']:
        raise ImproperlyConfigured('User import password, preview and prepared password TTLs must be ordered.')
    if numbers['report_seconds'] < numbers['prepared_password_seconds']:
        raise ImproperlyConfigured('User import reports must outlive prepared passwords.')
    keys = settings.DMOJ_USER_IMPORT_KEYS
    active = settings.DMOJ_USER_IMPORT_ACTIVE_KEY_ID
    digest = settings.DMOJ_USER_IMPORT_DIGEST_KEY
    if not isinstance(keys, dict) or not isinstance(active, str):
        raise ImproperlyConfigured('User import keys must be a key-ID mapping and the active ID must be text.')
    for key_id, value in keys.items():
        if not isinstance(key_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', key_id):
            raise ImproperlyConfigured('User import key IDs must contain 1-32 letters, digits, underscores or hyphens.')
        _key(value, 'DMOJ_USER_IMPORT_KEYS')
    if active and active not in keys:
        raise ImproperlyConfigured('DMOJ_USER_IMPORT_ACTIVE_KEY_ID must identify a configured key.')
    digest_key = _key(digest, 'DMOJ_USER_IMPORT_DIGEST_KEY') if digest else b''
    if enabled and (not active or not digest_key):
        raise ImproperlyConfigured('Enabling user import requires an active encryption key and a digest key.')
    if digest_key and digest_key in {_key(value, 'DMOJ_USER_IMPORT_KEYS') for value in keys.values()}:
        raise ImproperlyConfigured('User import encryption and digest keys must be different.')
    return UserImportConfig(enabled=enabled, active_key_id=active, keys=dict(keys), digest_key=digest_key, **numbers)
