"""Opt-in MySQL test settings; never point the bootstrap connection at app data."""

from copy import deepcopy

from django.core.exceptions import ImproperlyConfigured

from dmoj.settings import *  # noqa: F401,F403

DATABASES = deepcopy(DATABASES)  # noqa: F405
if DATABASES['default']['ENGINE'] != 'django.db.backends.mysql':
    raise ImproperlyConfigured('Managed login integration tests require MySQL/MariaDB, not SQLite.')
DATABASES['default']['NAME'] = 'utcoj_managed_login_bootstrap_unused'
DATABASES['default']['TEST'] = {'NAME': 'test_utcoj_managed_login'}
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
DEFAULT_USER_LANGUAGE = 'CPP17'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
COMPRESS_ENABLED = False
COMPRESS_OFFLINE = False
VNOJ_ENABLE_SYNC_API = True
