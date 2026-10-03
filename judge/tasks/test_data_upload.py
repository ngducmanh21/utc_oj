import logging
import os
import time
import zlib
from zipfile import BadZipFile

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_noop

from judge.models import Problem, TestDataUpload
from judge.utils.test_data_upload import (
    UploadError, check_editor, staging_lock, staging_path, touch_session, validate_archive,
)

__all__ = ('validate_test_data_upload',)
logger = logging.getLogger(__name__)


def _live_upload(upload_id):
    upload = TestDataUpload.objects.select_related('session__problem', 'session__user').get(pk=upload_id)
    session = upload.session
    if (upload.status not in ('UPLOADED', 'VALIDATING') or session.status != 'ACTIVE' or
            session.expires_at <= timezone.now()):
        raise UploadError('stale_task', gettext_noop('This upload session has ended.'))
    check_editor(session.problem, session.user)
    return upload


@shared_task(bind=True, max_retries=None, soft_time_limit=660, time_limit=720, acks_late=True,
             reject_on_worker_lost=True)
def validate_test_data_upload(self, upload_id):
    try:
        with staging_lock('validation', blocking=False), staging_lock('file-%s' % upload_id.replace('-', ''),
                                                                      blocking=False):
            try:
                with transaction.atomic():
                    upload = TestDataUpload.objects.select_related('session').get(pk=upload_id)
                    Problem.objects.select_for_update().get(pk=upload.session.problem_id)
                    upload = _live_upload(upload_id)
                    upload.status = 'VALIDATING'
                    upload.save(update_fields=['status'])
                    touch_session(upload.session)
                last_check = [0.0]

                def heartbeat():
                    if time.monotonic() - last_check[0] < 5:
                        return
                    with transaction.atomic():
                        Problem.objects.select_for_update().get(pk=upload.session.problem_id)
                        current = _live_upload(upload_id)
                        touch_session(current.session)
                        TestDataUpload.objects.filter(pk=upload_id).update(last_activity=timezone.now())
                    last_check[0] = time.monotonic()

                path = staging_path(upload)
                if os.path.getsize(path) != upload.size:
                    raise UploadError('invalid_zip', gettext_noop('The uploaded file size does not match.'))
                names = validate_archive(path, heartbeat)
                with transaction.atomic():
                    Problem.objects.select_for_update().get(pk=upload.session.problem_id)
                    upload = _live_upload(upload_id)
                    upload.entries, upload.status, upload.last_activity = names, 'READY', timezone.now()
                    upload.save(update_fields=['entries', 'status', 'last_activity'])
                    touch_session(upload.session)
                logger.info('Test upload %s validated: %s entries', upload_id, len(names))
            except TestDataUpload.DoesNotExist:
                return
            except (UploadError, BadZipFile, OSError, RuntimeError, ValueError, zlib.error,
                    SoftTimeLimitExceeded) as exc:
                if isinstance(exc, UploadError) and exc.code == 'stale_task':
                    return
                # CAS prevents an old delivery from overwriting cancel/revoke/publication.
                message = (exc.message if isinstance(exc, UploadError) else
                           gettext_noop('ZIP validation failed: invalid or unreadable archive.'))
                TestDataUpload.objects.filter(pk=upload_id, status__in=('UPLOADED', 'VALIDATING')).update(
                    status='FAILED', error=message, finished_at=timezone.now(), reserved_bytes=0)
                logger.warning('Test upload %s failed validation: %s', upload_id, type(exc).__name__)
    except UploadError as exc:
        if exc.code != 'busy':
            raise
        # Do not occupy either Celery worker while another archive is being checked.
        raise self.retry(countdown=15)
