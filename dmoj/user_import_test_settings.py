"""Dedicated MySQL test DB; AppConfig.ready must never bootstrap against app data."""

from copy import deepcopy

from django.core.exceptions import ImproperlyConfigured

from dmoj.settings import *  # noqa: F401,F403

DATABASES = deepcopy(DATABASES)  # noqa: F405
if DATABASES['default']['ENGINE'] != 'django.db.backends.mysql':
    raise ImproperlyConfigured('User import tests require MySQL/MariaDB.')
DATABASES['default']['NAME'] = 'utcoj_user_import_bootstrap_unused'
DATABASES['default']['TEST'] = {'NAME': 'test_utcoj_user_import'}
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
DEFAULT_USER_LANGUAGE = 'CPP17'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
COMPRESS_ENABLED = False
COMPRESS_OFFLINE = False
VNOJ_ENABLE_SYNC_API = True
