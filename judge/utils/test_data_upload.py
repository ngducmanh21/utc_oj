"""Lease, resource admission and bounded ZIP validation for resumable test data."""
import fcntl
import json
import logging
import os
import shutil
import stat
import time
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext, gettext_noop

from judge.models.test_data_upload import ProblemDataEditSession, TestDataUpload

logger = logging.getLogger(__name__)
ACTIVE_UPLOADS = ('CREATED', 'CREATING', 'UPLOADING')
LIVE_UPLOADS = ACTIVE_UPLOADS + ('UPLOADED', 'VALIDATING', 'READY', 'APPLYING')
TERMINAL_UPLOADS = ('FAILED', 'CANCELED', 'EXPIRED')


class UploadError(Exception):
    def __init__(self, code, message, status=409, **details):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details


def upload_root():
    root = settings.DMOJ_TEST_UPLOAD_ROOT
    if not root or not os.path.isabs(root):
        raise UploadError('not_configured', gettext_noop('Upload staging is not configured.'), 503)
    path = Path(root)
    path.mkdir(mode=0o770, parents=True, exist_ok=True)
    return path


def staging_path(upload):
    return str(upload_root() / upload.id.hex)


def tus_busy(upload):
    path = Path(staging_path(upload))
    return any(path.with_name(path.name + suffix).exists() for suffix in ('.lock', '.stop'))


def tus_file_info(upload):
    """Read only the expected filestore sidecar; never follow its Storage paths."""
    path = Path(staging_path(upload))
    try:
        with path.with_name(path.name + '.info').open() as stream:
            info = json.loads(stream.read(16385))
        if (not isinstance(info, dict) or info.get('ID') != upload.id.hex or info.get('Size') != upload.size or
                info.get('SizeIsDeferred') or info.get('IsPartial') or info.get('IsFinal')):
            return None
        return info
    except (OSError, ValueError):
        return None


def enqueue_validation(upload_id):
    """The DB is the queue journal; polling/cleanup can retry a broker outage."""
    from kombu.exceptions import OperationalError

    from judge.tasks.test_data_upload import validate_test_data_upload
    try:
        validate_test_data_upload.delay(str(upload_id))
    except (OperationalError, OSError):
        logger.exception('Could not queue test upload %s; recovery will retry', upload_id)


def reconcile_upload(upload):
    """Recover a lost tusd callback while holding the problem row lock."""
    now = timezone.now()
    if upload.session.status != 'ACTIVE' or upload.session.expires_at <= now:
        return
    if upload.status in ACTIVE_UPLOADS and tus_file_info(upload):
        size = os.path.getsize(staging_path(upload))
        if size == upload.size and not tus_busy(upload):
            upload.status, upload.actual_size = 'UPLOADED', size
            upload.last_activity = now
            upload.save(update_fields=['status', 'actual_size', 'last_activity'])
            transaction.on_commit(lambda: enqueue_validation(upload.pk))
        elif upload.status in ('CREATED', 'CREATING'):
            upload.status = 'UPLOADING'
            upload.save(update_fields=['status'])
    elif upload.status == 'CREATING' and upload.last_activity < now - timedelta(minutes=5):
        # Pre-create was accepted, but tusd died before creating the file. Reusing
        # that ID could truncate a late POST, so require a fresh upload record.
        upload.status, upload.finished_at, upload.reserved_bytes = 'FAILED', now, 0
        upload.error = gettext_noop('Upload creation was interrupted. Cancel it and select the ZIP again.')
        upload.save(update_fields=['status', 'finished_at', 'reserved_bytes', 'error'])
    elif upload.status in ('UPLOADED', 'VALIDATING') and upload.last_activity < now - timedelta(seconds=60):
        upload.last_activity = now
        upload.save(update_fields=['last_activity'])
        transaction.on_commit(lambda: enqueue_validation(upload.pk))


@contextmanager
def staging_lock(name, blocking=True):
    """Shared filesystem lock works across site processes and Celery containers."""
    path = upload_root() / ('.' + name + '.lock')
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise UploadError('busy', gettext_noop('Another upload task is active.'))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def check_editor(problem, user):
    if not user.is_authenticated or not user.is_active or not (user.is_superuser or problem.is_editable_by(user)):
        raise UploadError('permission_denied', gettext_noop('You cannot edit this problem.'), 403)
    if problem.is_archived or problem.is_deleted or problem.is_manually_managed:
        raise UploadError('unavailable', gettext_noop('The problem data cannot be edited.'), 409)


def deadline(session, now=None):
    now = now or timezone.now()
    return min(now + timedelta(seconds=settings.DMOJ_TEST_UPLOAD_SESSION_SECONDS),
               session.created_at + timedelta(seconds=settings.DMOJ_TEST_UPLOAD_MAX_AGE_SECONDS))


def _expire_sessions(problem):
    # Call only while holding the problem row lock. APPLYING is recovered separately.
    expired = ProblemDataEditSession.objects.filter(problem=problem, status='ACTIVE', expires_at__lte=timezone.now())
    for session in expired:
        session.status = 'EXPIRED'
        session.save(update_fields=['status'])
        session.uploads.filter(status__in=LIVE_UPLOADS).exclude(status='APPLYING').update(
            status='EXPIRED', finished_at=timezone.now(), reserved_bytes=0)


def assert_problem_unlocked(problem):
    """Use inside a transaction after select_for_update on Problem for a write guard."""
    _expire_sessions(problem)
    active = ProblemDataEditSession.objects.filter(problem=problem, status__in=('ACTIVE', 'APPLYING')).first()
    if active:
        raise UploadError('locked', gettext('Test data is being edited by %s.') % active.user.get_username(),
                          owner=active.user.get_username())


def owned_session(problem, user, session_id, token=None, for_update=False):
    check_editor(problem, user)
    try:
        session_id = uuid.UUID(str(session_id))
    except (ValueError, TypeError, AttributeError):
        raise UploadError('invalid_session', gettext_noop('Start an editing session first.'), 403)
    query = ProblemDataEditSession.objects
    if for_update:
        query = query.select_for_update()
    session = query.filter(pk=session_id, problem=problem, user=user).first()
    if not session or token is None or not constant_time_compare(str(session.token), str(token)):
        raise UploadError('invalid_session', gettext_noop('The editing session does not belong to you.'), 403)
    if session.status != 'ACTIVE' or session.expires_at <= timezone.now():
        raise UploadError('session_expired', gettext_noop('The editing session has ended. Start a new session.'))
    return session


def touch_session(session):
    now = timezone.now()
    session.last_activity = now
    session.expires_at = deadline(session, now)
    session.save(update_fields=['last_activity', 'expires_at'])


def ready_upload(session, upload_id, for_update=False):
    try:
        upload_id = uuid.UUID(str(upload_id))
    except (ValueError, TypeError, AttributeError):
        raise UploadError('invalid_upload', gettext_noop('Unknown upload.'), 404)
    query = TestDataUpload.objects
    if for_update:
        query = query.select_for_update()
    upload = query.filter(pk=upload_id, session=session).first()
    if not upload:
        raise UploadError('invalid_upload', gettext_noop('Unknown upload.'), 404)
    if upload.status != 'READY':
        raise UploadError('not_ready', gettext_noop('Wait for the ZIP validation to finish.'))
    if not os.path.isfile(staging_path(upload)) or os.path.getsize(staging_path(upload)) != upload.size:
        raise UploadError('missing_upload', gettext_noop('The uploaded file is missing or has changed.'))
    return upload


def reserve_upload_space(size, problem):
    """Call with admission flock held. Account for copies on the destination mount too."""
    active = TestDataUpload.objects.filter(status__in=ACTIVE_UPLOADS, session__status='ACTIVE',
                                           session__expires_at__gt=timezone.now())
    if active.count() >= settings.DMOJ_TEST_UPLOAD_MAX_ACTIVE:
        raise UploadError('upload_capacity', gettext_noop('All upload slots are busy. Try again shortly.'), 429)
    from judge.models import problem_data_storage

    destination = Path(problem_data_storage.path(problem.code))
    destination.mkdir(parents=True, exist_ok=True)
    roots = {os.stat(upload_root()).st_dev: upload_root(), os.stat(destination).st_dev: destination}
    reserved = dict.fromkeys(roots, 0)
    for upload in TestDataUpload.objects.filter(status__in=LIVE_UPLOADS):
        try:
            written = os.path.getsize(staging_path(upload))
        except OSError:
            written = 0
        staging_device = os.stat(upload_root()).st_dev
        reserved[staging_device] += max(0, upload.size - written)
        other = Path(problem_data_storage.path(upload.session.problem.code))
        while not other.exists():
            other = other.parent
        device = other.stat().st_dev
        if device in reserved:
            reserved[device] += upload.size
    reserved[os.stat(upload_root()).st_dev] += size
    reserved[os.stat(destination).st_dev] += size
    for device, root in roots.items():
        if shutil.disk_usage(root).free - reserved[device] < settings.DMOJ_TEST_UPLOAD_DISK_RESERVE:
            raise UploadError('insufficient_storage',
                              gettext_noop('There is not enough free disk space for this upload.'), 507)


def validate_archive(path, heartbeat=lambda: None):
    """Read each entry in bounded buffers, validating CRC without extracting anything."""
    started = time.monotonic()
    total = 0
    names, seen = [], set()
    # ZipFile eagerly loads the central directory. Check its small EOCD/ZIP64
    # footer first, using the same parser as our Python 3.12 runtime, so an entry
    # count check after opening cannot be used to exhaust worker memory.
    with (open(path, 'rb') if isinstance(path, (str, os.PathLike)) else nullcontext(path)) as stream:
        footer = zipfile._EndRecData(stream)
        if not footer:
            raise BadZipFile('Missing ZIP footer')
        if (footer[zipfile._ECD_ENTRIES_TOTAL] > settings.DMOJ_TEST_UPLOAD_MAX_ENTRIES or
                footer[zipfile._ECD_SIZE] > settings.DMOJ_TEST_UPLOAD_MAX_DIRECTORY_SIZE):
            raise UploadError('invalid_zip',
                              gettext_noop('The ZIP directory exceeds the allowed size or entry count.'), 400)
    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > settings.DMOJ_TEST_UPLOAD_MAX_ENTRIES:
            raise UploadError('invalid_zip', gettext_noop('The ZIP contains too many entries.'), 400)
        for item in entries:
            if time.monotonic() - started > settings.DMOJ_TEST_UPLOAD_VALIDATION_SECONDS:
                raise UploadError('invalid_zip', gettext_noop('ZIP validation took too long.'), 400)
            heartbeat()
            name = item.filename
            parts = PurePosixPath(name).parts
            if (not name or '\x00' in item.orig_filename or '\\' in name or name.startswith('/') or
                    any(part in ('..', '.') for part in name.split('/')) or ':' in parts[0] or
                    len(name) > 500 or name in seen):
                raise UploadError('invalid_zip', gettext_noop('The ZIP contains duplicate or unsafe file names.'), 400)
            seen.add(name)
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise UploadError('invalid_zip',
                                  gettext_noop('Links and special files are not supported in ZIP files.'), 400)
            if item.flag_bits & 1 or item.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                raise UploadError('invalid_zip',
                                  gettext_noop('The ZIP uses encryption or unsupported compression.'), 400)
            total += item.file_size
            if total > settings.DMOJ_TEST_UPLOAD_MAX_UNCOMPRESSED:
                raise UploadError('invalid_zip', gettext_noop('The ZIP expands beyond the allowed size.'), 400)
            if not item.is_dir():
                consumed = 0
                with archive.open(item) as content:
                    while True:
                        if time.monotonic() - started > settings.DMOJ_TEST_UPLOAD_VALIDATION_SECONDS:
                            raise UploadError('invalid_zip', gettext_noop('ZIP validation took too long.'), 400)
                        heartbeat()
                        chunk = content.read(1024 * 1024)
                        if not chunk:
                            break
                        consumed += len(chunk)
                        if consumed > item.file_size:
                            raise UploadError('invalid_zip', gettext_noop('An entry exceeds its declared size.'), 400)
                if consumed != item.file_size:
                    raise BadZipFile('Wrong entry size')
                if '__MACOSX' not in parts and parts[-1].lower() != '.ds_store' and not parts[-1].startswith('._'):
                    names.append(name)
    if not names:
        raise UploadError('invalid_zip', gettext_noop('The ZIP does not contain test files.'), 400)
    return sorted(names)


def cleanup_uploads(dry_run=False):
    from judge.models import Problem
    now = timezone.now()
    cutoff = now - timedelta(seconds=settings.DMOJ_TEST_UPLOAD_CLEANUP_GRACE_SECONDS)
    result = {'expired_sessions': 0, 'removed_files': 0}
    with staging_lock('cleanup', blocking=False), staging_lock('admission'):
        for problem_id in ProblemDataEditSession.objects.filter(
                status='ACTIVE', expires_at__lte=now).values_list('problem_id', flat=True).distinct():
            if not dry_run:
                with transaction.atomic():
                    problem = Problem.objects.select_for_update().get(pk=problem_id)
                    _expire_sessions(problem)
            result['expired_sessions'] += 1
        for upload in TestDataUpload.objects.filter(status__in=TERMINAL_UPLOADS + ('APPLIED',),
                                                    finished_at__lt=cutoff):
            try:
                with staging_lock('file-%s' % upload.id.hex, blocking=False):
                    path = Path(staging_path(upload))
                    # A canceled validator may still have the file open. Its lock
                    # and tusd's lock must both be released before deletion.
                    if tus_busy(upload):
                        continue
                    for candidate in (path, path.with_name(path.name + '.info')):
                        if candidate.exists():
                            result['removed_files'] += 1
                            if not dry_run:
                                candidate.unlink()
            except UploadError as exc:
                if exc.code != 'busy':
                    raise
        # A sidecar with no DB row cannot receive an authorized new request.
        # Only touch tusd's UUID filenames, after the maximum session lifetime.
        orphan_cutoff = now.timestamp() - settings.DMOJ_TEST_UPLOAD_MAX_AGE_SECONDS
        for candidate in upload_root().iterdir():
            name = candidate.name.removesuffix('.info')
            try:
                upload_id = uuid.UUID(hex=name)
            except ValueError:
                continue
            if (name != upload_id.hex or not candidate.is_file() or candidate.is_symlink() or
                    candidate.stat().st_mtime >= orphan_cutoff or
                    TestDataUpload.objects.filter(pk=upload_id).exists() or
                    (upload_root() / (name + '.lock')).exists() or
                    (upload_root() / (name + '.stop')).exists()):
                continue
            result['removed_files'] += 1
            if not dry_run:
                candidate.unlink()
        # Recover lost callbacks/worker deliveries without blocking a cleanup slot.
        if not dry_run:
            for upload in TestDataUpload.objects.filter(status__in=LIVE_UPLOADS, session__status='ACTIVE',
                                                        session__expires_at__gt=now):
                with transaction.atomic():
                    Problem.objects.select_for_update().get(pk=upload.session.problem_id)
                    upload.refresh_from_db()
                    reconcile_upload(upload)
    return result
