"""Best-effort uncommitted row progress, never a substitute for the SQL marker."""

import logging

from django.core.cache import caches
from django.utils import timezone

from judge.models import UserImportBatch
from judge.utils.user_import_config import get_user_import_config

logger = logging.getLogger(__name__)


def _key(batch_id, token):
    return 'user-import:progress:%s:%s' % (batch_id, token)


def write_import_progress(batch_id, token, processed, total):
    try:
        config = get_user_import_config()
        caches[config.progress_cache].set(_key(batch_id, token), {
            'attempt': str(token), 'processed': processed, 'total': total, 'at': timezone.now().timestamp(),
        }, timeout=config.execution_lease_seconds + config.dispatch_seconds)
    except Exception:
        logger.warning('User import progress unavailable for batch %s', batch_id)


def read_import_progress(batch):
    if batch.status != UserImportBatch.Status.RUNNING or not batch.attempt_token:
        return None
    try:
        config = get_user_import_config()
        value = caches[config.progress_cache].get(_key(batch.pk, batch.attempt_token))
        if (not isinstance(value, dict) or value.get('attempt') != str(batch.attempt_token) or
                type(value.get('processed')) is not int or value.get('total') != batch.total_rows or
                not 0 <= value['processed'] <= batch.total_rows or
                not isinstance(value.get('at'), (int, float)) or
                not 0 <= timezone.now().timestamp() - value['at'] <= config.dispatch_seconds):
            return None
        return {'processed': value['processed'], 'total': batch.total_rows, 'committed': False}
    except Exception:
        return None
