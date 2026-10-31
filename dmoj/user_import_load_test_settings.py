"""Real password costs for the opt-in, disposable M5 load gate."""

from copy import deepcopy

from dmoj.settings import AUTH_PASSWORD_VALIDATORS as PRODUCTION_VALIDATORS
from dmoj.user_import_integration_test_settings import *  # noqa: F401,F403

AUTH_PASSWORD_VALIDATORS = deepcopy(PRODUCTION_VALIDATORS)
PASSWORD_HASHERS = ['django.contrib.auth.hashers.PBKDF2PasswordHasher']
DMOJ_USER_IMPORT_EXECUTION_LEASE_SECONDS = 600
DMOJ_USER_IMPORT_DISPATCH_SECONDS = 60
