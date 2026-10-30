"""Disposable DB for opt-in Redis/prefork user import acceptance tests."""

import base64
from copy import deepcopy

from dmoj.user_import_test_settings import *  # noqa: F401,F403

DATABASES = deepcopy(DATABASES)  # noqa: F405
DATABASES['default']['NAME'] = 'utcoj_import_integration_bootstrap_unused'
DATABASES['default']['TEST'] = {'NAME': 'test_utcoj_user_import_integration'}
AUTH_PASSWORD_VALIDATORS = []
DMOJ_USER_IMPORT_ENABLED = True
# Public test fixtures only; these keys must never be used for application data.
DMOJ_USER_IMPORT_KEYS = {'integration': base64.urlsafe_b64encode(b'a' * 32).decode()}
DMOJ_USER_IMPORT_ACTIVE_KEY_ID = 'integration'
DMOJ_USER_IMPORT_DIGEST_KEY = base64.urlsafe_b64encode(b'c' * 32).decode()
DMOJ_USER_IMPORT_EXECUTION_LEASE_SECONDS = 2
DMOJ_USER_IMPORT_DISPATCH_SECONDS = 2
LANGUAGE_CODE = 'en'
