"""Worker-only test settings; refuse DB/Redis endpoints outside the harness."""

import os
import re
from copy import deepcopy
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from dmoj.user_import_integration_test_settings import *  # noqa: F401,F403

test_database = os.environ.get('UTCOJ_IMPORT_WORKER_DB')
test_broker = os.environ.get('UTCOJ_IMPORT_WORKER_BROKER', '')
test_queue = os.environ.get('UTCOJ_IMPORT_WORKER_QUEUE', '')
url = urlsplit(test_broker)
if (os.environ.get('UTCOJ_IMPORT_WORKER_TEST') != '1' or
        test_database != 'test_utcoj_user_import_integration' or
        url.scheme != 'redis' or url.hostname != '127.0.0.1' or url.username or url.password or
        not url.port or url.path != '/0' or url.query or url.fragment or
        not re.fullmatch(r'utcoj-import-integration-[a-f0-9]{32}', test_queue)):
    raise ImproperlyConfigured('The import worker harness requires an isolated test DB and loopback Redis.')
DATABASES = deepcopy(DATABASES)  # noqa: F405
DATABASES['default']['NAME'] = test_database
CELERY_BROKER_URL = test_broker
CELERY_RESULT_BACKEND = test_broker[:-1] + '2'
CELERY_TASK_DEFAULT_QUEUE = test_queue
CELERY_TASK_ALWAYS_EAGER = False
CELERY_ACCEPT_CONTENT = ['json']
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_TIMEOUT = 0.5
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = False
CELERY_BROKER_TRANSPORT_OPTIONS = {'visibility_timeout': 2, 'socket_timeout': 0.5, 'socket_connect_timeout': 0.5}
CACHES = deepcopy(CACHES)  # noqa: F405
CACHES['import_progress'] = {
    'BACKEND': 'django.core.cache.backends.redis.RedisCache', 'LOCATION': test_broker[:-1] + '1',
    'OPTIONS': {'socket_timeout': 0.5, 'socket_connect_timeout': 0.5},
}
DMOJ_USER_IMPORT_PROGRESS_CACHE = 'import_progress'

if os.environ.get('UTCOJ_IMPORT_LOAD_TEST') == '1':
    from dmoj.user_import_load_test_settings import AUTH_PASSWORD_VALIDATORS, PASSWORD_HASHERS  # noqa: F401
    DMOJ_USER_IMPORT_EXECUTION_LEASE_SECONDS = 600
    DMOJ_USER_IMPORT_DISPATCH_SECONDS = 60
