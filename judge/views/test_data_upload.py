import base64
import json
import logging
import os
import uuid
from functools import wraps
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext, gettext_noop
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from judge.models import Problem, ProblemDataEditSession, TestDataUpload
from judge.utils.test_data_upload import (
    ACTIVE_UPLOADS, LIVE_UPLOADS, UploadError, assert_problem_unlocked, check_editor, deadline, enqueue_validation,
    owned_session, reconcile_upload, reserve_upload_space, staging_lock, staging_path, touch_session,
)

logger = logging.getLogger(__name__)


def api(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            response = view(request, *args, **kwargs)
        except UploadError as exc:
            response = JsonResponse({'error': {'code': exc.code, 'message': gettext(exc.message),
                                               **exc.details}, **exc.details},
                                    status=exc.status)
        response['Cache-Control'] = 'no-store'
        return response
    return wrapped


def data(request):
    try:
        body = json.loads(request.body or b'{}') if request.content_type == 'application/json' else request.POST.dict()
        if not isinstance(body, dict):
            raise ValueError
        return body
    except (ValueError, UnicodeError):
        raise UploadError('invalid_request', gettext_noop('Expected a JSON object.'), 400)


def problem_for(request, code, lock=False):
    query = Problem.objects.select_for_update() if lock else Problem.objects
    try:
        problem = query.get(code=code)
    except Problem.DoesNotExist:
        raise UploadError('not_found', gettext_noop('Unknown problem.'), 404)
    check_editor(problem, request.user)
    return problem


def require_enabled(new=False):
    if not settings.DMOJ_TEST_UPLOAD_ENABLED or (new and not settings.DMOJ_TEST_UPLOAD_ACCEPT_NEW):
        raise UploadError('disabled', gettext_noop('New resumable uploads are currently disabled.'), 503)


def serialize_session(session, include_token=True):
    result = {'id': str(session.id), 'status': session.status, 'expires_at': session.expires_at.isoformat(),
              'owner': session.user.get_username()}
    if include_token:
        result['token'] = str(session.token)
    return result


def serialize_upload(upload, entries=False):
    result = {'id': str(upload.id), 'token': str(upload.token), 'filename': upload.filename,
              'size': upload.size, 'actual_size': upload.actual_size, 'fingerprint': upload.fingerprint,
              'status': upload.status, 'error': gettext(upload.error) if upload.error else '',
              'tus_url': (settings.DMOJ_TEST_UPLOAD_TUS_ENDPOINT + upload.id.hex
                          if upload.status not in ('CREATED', 'CREATING') else None)}
    if entries and upload.status in ('READY', 'APPLIED'):
        result['entries'] = upload.entries
    return result


def session_response(session):
    return JsonResponse({'session': serialize_session(session), 'config': {
        'chunk_size': settings.DMOJ_TEST_UPLOAD_CHUNK_SIZE, 'max_size': settings.DMOJ_TEST_UPLOAD_MAX_SIZE,
        'tus_endpoint': settings.DMOJ_TEST_UPLOAD_TUS_ENDPOINT,
        'heartbeat_seconds': settings.DMOJ_TEST_UPLOAD_HEARTBEAT_SECONDS,
    }, 'uploads': [serialize_upload(upload) for upload in session.uploads.exclude(
        status__in=('CANCELED', 'EXPIRED')).order_by('created_at')]})


@require_POST
@api
def create_session(request, problem):
    require_enabled(new=True)
    with transaction.atomic():
        problem = problem_for(request, problem, lock=True)
        try:
            assert_problem_unlocked(problem)
        except UploadError as exc:
            current = ProblemDataEditSession.objects.filter(problem=problem, status__in=('ACTIVE', 'APPLYING')).first()
            if current:
                exc.details.update(session=serialize_session(current, include_token=False),
                                   can_revoke=request.user.is_superuser)
                if current.user_id == request.user.pk:
                    exc.message = (gettext_noop('Your previous edit session is still active. '
                                   'Continue in its original tab or end it first.'))
            raise
        session = ProblemDataEditSession(problem=problem, user=request.user)
        session.expires_at = deadline(session)
        session.save()
    return session_response(session)


@require_POST
@api
def session_heartbeat(request, problem, session_id):
    body = data(request)
    with transaction.atomic():
        problem = problem_for(request, problem, lock=True)
        session = owned_session(problem, request.user, session_id, body.get('edit_token'), for_update=True)
        touch_session(session)
        for upload in session.uploads.filter(status__in=LIVE_UPLOADS):
            reconcile_upload(upload)
    return session_response(session)


def finish_session(session, status):
    now = timezone.now()
    session.status = status
    session.save(update_fields=['status'])
    session.uploads.filter(status__in=LIVE_UPLOADS).exclude(status='APPLYING').update(
        status='CANCELED', finished_at=now, reserved_bytes=0)


@require_POST
@api
def end_session(request, problem, session_id):
    with transaction.atomic():
        problem = problem_for(request, problem, lock=True)
        session = owned_session(problem, request.user, session_id, data(request).get('edit_token'), for_update=True)
        finish_session(session, 'ENDED')
    return JsonResponse({'session': serialize_session(session)})


@require_POST
@api
def revoke_session(request, problem, session_id):
    with transaction.atomic():
        problem = problem_for(request, problem, lock=True)
        if not request.user.is_superuser:
            raise UploadError('permission_denied',
                              gettext_noop('Only administrators may revoke editing sessions.'), 403)
        session = ProblemDataEditSession.objects.filter(pk=session_id, problem=problem).first()
        if not session:
            raise UploadError('not_found', gettext_noop('Unknown session.'), 404)
        if session.status == 'APPLYING':
            raise UploadError('applying', gettext_noop('The new test data is being published. Wait for it to finish.'))
        if session.status == 'ACTIVE':
            finish_session(session, 'REVOKED')
            logger.warning('Test data edit session %s revoked by user %s', session.pk, request.user.pk)
    return JsonResponse({'session': serialize_session(session, include_token=False)})


@require_POST
@api
def create_upload(request, problem):
    require_enabled(new=True)
    body = data(request)
    size, filename = body.get('size'), body.get('filename')
    fingerprint = body.get('fingerprint', '')
    if (type(size) is not int or size <= 0 or size > settings.DMOJ_TEST_UPLOAD_MAX_SIZE or
            not isinstance(filename, str) or not filename.lower().endswith('.zip') or len(filename) > 255 or
            not isinstance(fingerprint, str) or len(fingerprint) > 128):
        raise UploadError('invalid_file', gettext_noop('Choose a ZIP within the allowed file size.'), 400)
    with staging_lock('admission'), transaction.atomic():
        problem = problem_for(request, problem, lock=True)
        session = owned_session(problem, request.user, body.get('edit_session_id'), body.get('edit_token'), True)
        if session.uploads.filter(status__in=LIVE_UPLOADS).exists():
            raise UploadError('upload_exists', gettext_noop('Cancel the previous upload before choosing another file.'))
        reserve_upload_space(size, problem)
        upload = TestDataUpload.objects.create(session=session, filename=filename, size=size,
                                               fingerprint=fingerprint, reserved_bytes=2 * size)
        touch_session(session)
    return JsonResponse({'upload': serialize_upload(upload), 'tus_endpoint': settings.DMOJ_TEST_UPLOAD_TUS_ENDPOINT,
                         'chunk_size': settings.DMOJ_TEST_UPLOAD_CHUNK_SIZE}, status=201)


def get_owned_upload(request, code, upload_id, body=None):
    body = body or {}
    problem = problem_for(request, code, lock=True)
    session = owned_session(problem, request.user,
                            body.get('edit_session_id') or request.headers.get('X-Edit-Session'),
                            body.get('edit_token') or request.headers.get('X-Edit-Token'), True)
    upload = TestDataUpload.objects.select_for_update().filter(pk=upload_id, session=session).first()
    if not upload:
        raise UploadError('not_found', gettext_noop('Unknown upload.'), 404)
    return session, upload


@require_GET
@api
def upload_status(request, problem, upload_id):
    with transaction.atomic():
        session, upload = get_owned_upload(request, problem, upload_id)
        reconcile_upload(upload)
    response = JsonResponse({'upload': serialize_upload(upload, entries=request.GET.get('entries') == '1'),
                             'session': serialize_session(session)})
    response['Cache-Control'] = 'no-store'
    return response


@require_POST
@api
def cancel_upload(request, problem, upload_id):
    with transaction.atomic():
        session, upload = get_owned_upload(request, problem, upload_id, data(request))
        if upload.status in ('APPLYING', 'APPLIED'):
            raise UploadError('applying', gettext_noop('This upload is already being applied.'))
        upload.status, upload.finished_at, upload.reserved_bytes = 'CANCELED', timezone.now(), 0
        upload.save(update_fields=['status', 'finished_at', 'reserved_bytes'])
        touch_session(session)
    return JsonResponse({'upload': serialize_upload(upload)})


def token_upload(request):
    # Authorization is reserved for the site's account-wide API middleware.
    auth = request.headers.get('X-Test-Upload-Token', '')
    try:
        token = uuid.UUID(auth)
    except ValueError:
        raise UploadError('unauthorized', gettext_noop('Invalid upload token.'), 403)
    upload = TestDataUpload.objects.select_related('session__problem', 'session__user').filter(token=token).first()
    if not upload:
        raise UploadError('unauthorized', gettext_noop('Invalid upload token.'), 403)
    return upload


def check_upload_lease(upload):
    session = upload.session
    check_editor(session.problem, session.user)
    if session.status != 'ACTIVE' or session.expires_at <= timezone.now():
        raise UploadError('session_expired', gettext_noop('The editing session has expired.'), 403)
    return session


def require_internal(request):
    secret = settings.DMOJ_TEST_UPLOAD_INTERNAL_SECRET
    if not secret:
        raise UploadError('not_configured', gettext_noop('Internal upload authentication is not configured.'), 503)
    if not constant_time_compare(secret, request.headers.get('X-Upload-Secret', '')):
        raise UploadError('unauthorized', gettext_noop('Internal upload authentication is required.'), 403)


@csrf_exempt
@api
def authorize_tus(request):
    # This URL is Nginx-internal; upload token plus cookie identifies the editor.
    require_enabled()
    require_internal(request)
    upload = token_upload(request)
    if not request.user.is_authenticated or request.user.pk != upload.session.user_id:
        raise UploadError('unauthorized', gettext_noop('The upload belongs to a different user.'), 403)
    session = check_upload_lease(upload)
    method = request.headers.get('X-Original-Method', '')
    path = urlsplit(request.headers.get('X-Original-URI', '')).path
    base = settings.DMOJ_TEST_UPLOAD_TUS_ENDPOINT
    if method == 'POST':
        if path != base or upload.status != 'CREATED':
            raise UploadError('invalid_upload', gettext_noop('This upload cannot be created again.'), 403)
        try:
            if request.headers.get('Upload-Defer-Length') or request.headers.get('Upload-Concat'):
                raise ValueError
            if int(request.headers.get('Upload-Length', '-1')) != upload.size:
                raise ValueError
            metadata = {}
            for part in request.headers.get('Upload-Metadata', '').split(','):
                key, value = part.strip().split(' ', 1)
                metadata[key] = base64.b64decode(value, validate=True).decode()
            if uuid.UUID(metadata.get('upload_id', '')) != upload.pk:
                raise ValueError
        except (ValueError, UnicodeError):
            raise UploadError('invalid_upload', gettext_noop('Invalid upload metadata.'), 403)
    elif method in ('PATCH', 'HEAD'):
        if path != base + upload.id.hex or upload.status not in ACTIVE_UPLOADS + ('UPLOADED', 'VALIDATING', 'READY'):
            raise UploadError('invalid_upload', gettext_noop('The upload is no longer writable.'), 403)
        if method == 'PATCH' and upload.status not in ACTIVE_UPLOADS:
            raise UploadError('invalid_upload', gettext_noop('The upload has already finished.'), 403)
    else:
        raise UploadError('method_not_allowed', gettext_noop('This upload operation is not allowed.'), 403)
    if method in ('POST', 'PATCH'):
        try:
            length = int(request.headers.get('X-Original-Content-Length', '-1'))
            if length < 0 or length > settings.DMOJ_TEST_UPLOAD_CHUNK_SIZE or (method == 'POST' and length):
                raise ValueError
        except ValueError:
            raise UploadError('invalid_chunk', gettext_noop('The upload request is too large or has no length.'), 403)
        # Recheck under the same row lock used by revoke/publish before refreshing a lease.
        with transaction.atomic():
            Problem.objects.select_for_update().get(pk=session.problem_id)
            upload.refresh_from_db()
            session = check_upload_lease(upload)
            if upload.status not in ACTIVE_UPLOADS:
                raise UploadError('invalid_upload', gettext_noop('The upload is no longer writable.'), 403)
            touch_session(session)
            TestDataUpload.objects.filter(pk=upload.pk).update(last_activity=timezone.now())
    return HttpResponse(status=204)


@csrf_exempt
@require_POST
@api
def tus_hook(request):
    # Nginx injects the internal secret, which tusd forwards with the upload token.
    require_internal(request)
    upload = token_upload(request)
    body = data(request)
    hook_type = body.get('Type')
    event = body.get('Event', {})
    if not isinstance(event, dict):
        raise UploadError('invalid_hook', gettext_noop('Invalid hook payload.'), 400)
    info = event.get('Upload', {})
    if not isinstance(info, dict):
        raise UploadError('invalid_hook', gettext_noop('Invalid hook payload.'), 400)
    if hook_type == 'pre-create':
        with transaction.atomic():
            Problem.objects.select_for_update().get(pk=upload.session.problem_id)
            upload.refresh_from_db()
            session = check_upload_lease(upload)
            if (upload.status != 'CREATED' or info.get('Size') != upload.size or
                    info.get('SizeIsDeferred') or info.get('IsPartial') or info.get('IsFinal') or
                    not isinstance(info.get('MetaData'), dict) or
                    info['MetaData'].get('upload_id') != str(upload.pk) or
                    os.path.exists(staging_path(upload))):
                return JsonResponse({'RejectUpload': True, 'HTTPResponse': {'StatusCode': 409}})
            # Reserve the ID before releasing the row lock. Two concurrent POSTs
            # must never both reach filestore.NewUpload, which truncates files.
            upload.status, upload.last_activity = 'CREATING', timezone.now()
            upload.save(update_fields=['status', 'last_activity'])
            touch_session(session)
        return JsonResponse({'ChangeFileInfo': {'ID': upload.id.hex}})
    if info.get('ID') != upload.id.hex:
        raise UploadError('invalid_hook', gettext_noop('The hook refers to a different upload.'), 403)
    if hook_type not in ('post-create', 'post-receive', 'post-finish'):
        return JsonResponse({})
    with transaction.atomic():
        Problem.objects.select_for_update().get(pk=upload.session.problem_id)
        upload.refresh_from_db()
        # Duplicate callbacks are successful no-ops, including terminal sessions.
        if upload.status in ('UPLOADED', 'VALIDATING', 'READY', 'APPLYING', 'APPLIED'):
            return JsonResponse({})
        try:
            session = check_upload_lease(upload)
        except UploadError:
            if hook_type == 'post-receive':
                return JsonResponse({'StopUpload': True, 'HTTPResponse': {'StatusCode': 403}})
            raise
        if upload.status not in ACTIVE_UPLOADS:
            raise UploadError('invalid_upload', gettext_noop('The upload has ended.'), 403)
        if hook_type in ('post-create', 'post-receive'):
            upload.status = 'UPLOADING'
        else:
            path = staging_path(upload)
            if (info.get('Offset') != upload.size or info.get('Size') != upload.size or
                    not os.path.isfile(path) or os.path.getsize(path) != upload.size):
                raise UploadError('invalid_upload', gettext_noop('The upload is incomplete.'), 409)
            upload.status, upload.actual_size = 'UPLOADED', upload.size
            transaction.on_commit(lambda: enqueue_validation(upload.pk))
        upload.last_activity = timezone.now()
        upload.save(update_fields=['status', 'actual_size', 'last_activity'])
        touch_session(session)
    return JsonResponse({})
