"""Session-only CSV upload and owner-scoped preview endpoints."""

import json
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import auth
from django.contrib.auth.models import User
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, RequestDataTooBig, TooManyFieldsSent
from django.db import DatabaseError, transaction
from django.http import HttpResponse, JsonResponse
from django.http.multipartparser import MultiPartParserError
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from judge.models import Profile, UserImportBatch, UserImportOperation, UserImportRow
from judge.tasks.user_import import dispatch_import_execution, dispatch_import_validation
from judge.utils.managed_login import PENDING_LOGIN_KEY, discard_login_session, session_epoch_matches
from judge.utils.user_import import CSV_FIELDS, UserImportError, parse_idempotency_key, read_import_csv, \
    require_user_import_enabled, upload_request_digest
from judge.utils.user_import_execution import TERMINAL_IMPORTS, reserve_import_submission
from judge.utils.user_import_progress import read_import_progress
from judge.utils.user_import_service import ImportBatchConflict
from judge.utils.user_import_upload import UserImportUploadHandler
from judge.utils.user_import_validation import expire_import_preview, reserve_import_upload

PAGE_SIZE = 50


def import_error(code, message, status, **details):
    response = JsonResponse({'error': {'code': code, 'message': str(message)}, **details}, status=status)
    response['Cache-Control'] = 'no-store'
    return response


def import_session_error(request):
    if request.headers.get('Authorization') or getattr(request, '_api_authenticated', False):
        return import_error('session_required', _('Use a superuser session to import users.'), 403)
    if '_impersonate' in request.session or getattr(request.user, 'is_impersonate', False):
        return import_error('permission_denied', _('End impersonation before importing users.'), 403)
    if PENDING_LOGIN_KEY in request.session:
        return import_error('two_factor_required', _('Complete two-factor authentication first.'), 403)
    if not request.user.is_authenticated:
        return import_error('authentication_required', _('Log in as a superuser.'), 401)
    try:
        actor = User.objects.select_related('profile').get(pk=request.user.pk)
        profile = actor.profile
    except (User.DoesNotExist, Profile.DoesNotExist):
        return import_error('permission_denied', _('A valid superuser account and profile are required.'), 403)
    if (not session_epoch_matches(request.session, profile) or
            not constant_time_compare(request.session.get(auth.HASH_SESSION_KEY, ''), actor.get_session_auth_hash())):
        discard_login_session(request)
        return import_error('session_expired', _('Your session expired. Log in again.'), 401)
    if not actor.is_active or not actor.is_staff or not actor.is_superuser or profile.is_banned:
        return import_error('permission_denied', _('Only active superusers can import users.'), 403)
    if (profile.rotate_password_on_login or
            ((profile.is_totp_enabled or profile.is_webauthn_enabled) and not request.session.get('2fa_passed'))):
        return import_error('two_factor_required', _('Complete two-factor authentication first.'), 403)
    if request.session.get('password_pwned') and not settings.VNOJ_OFFICIAL_CONTEST_MODE:
        return import_error('password_change_required', _('Change your password before importing users.'), 403)
    request.user = request._cached_user = actor
    request.profile = profile


def import_endpoint(method, *, html=False):
    def decorate(view):
        protected = csrf_protect(view)

        @csrf_exempt
        @sensitive_post_parameters()
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            request.no_profile_update = True
            try:
                config = require_user_import_enabled()
                error = import_session_error(request)
                if error is not None:
                    if html and error.status_code == 401 and request.headers.get('Accept') != 'application/json':
                        return redirect_to_login(request.get_full_path(), reverse('auth_login'))
                    return error
                if request.method != method:
                    response = import_error('method_not_allowed', _('This method is not allowed.'), 405)
                    response['Allow'] = method
                    return response
                if request.method == 'POST':
                    if view.__name__ == 'post_file':
                        # Install before the explicit CSRF check parses multipart data.
                        request.upload_handlers = [UserImportUploadHandler(request, config.max_bytes)]
                        limit = config.max_bytes + 65536
                    else:
                        limit = 16384
                    try:
                        length = int(request.META.get('CONTENT_LENGTH') or 0)
                    except ValueError:
                        return import_error('invalid_request', _('Invalid request length.'), 422)
                    if length > limit:
                        return import_error('file_too_large', _('The request exceeds the upload size limit.'), 413)
                response = protected(request, *args, **kwargs)
                if getattr(request, 'user_import_upload_too_large', False):
                    return import_error('file_too_large', _('The CSV exceeds the upload size limit.'), 413)
                return response
            except UserImportError as error:
                if error.code in ('disabled', 'not_found'):
                    status = 404
                elif error.code == 'file_too_large':
                    status = 413
                elif isinstance(error, ImportBatchConflict) or error.code in (
                        'active_batch', 'idempotency_conflict', 'batch_conflict', 'batch_busy', 'expired_secret'):
                    status = 409
                elif error.code == 'unavailable':
                    status = 503
                else:
                    status = 422
                issues = getattr(error, 'issues', ())
                return import_error(error.code, error.message, status,
                                    **({'issues': [issue.as_dict() for issue in issues]} if issues else {}))
            except PermissionDenied:
                return import_error('permission_denied', _('Only active superusers can import users.'), 403)
            except (RequestDataTooBig, TooManyFieldsSent):
                return import_error('file_too_large', _('The request exceeds the upload size limit.'), 413)
            except MultiPartParserError:
                return import_error('invalid_request', _('Upload one UTF-8 CSV file.'), 422)
            except (DatabaseError, ImproperlyConfigured):
                return import_error('unavailable', _('User import is temporarily unavailable. Retry the same request.'),
                                    503)
            except Exception:
                # Never let an upload/parser exception render credentials in a debug page.
                return import_error('unavailable', _('User import is temporarily unavailable. Retry the same request.'),
                                    503)
            finally:
                # UploadedFile.close() alone does not overwrite saved temp files;
                # this route uses only the bounded in-memory handler above.
                for upload in getattr(request, '_files', {}).values():
                    upload.close()

        wrapped = never_cache(wrapped)
        wrapped = transaction.non_atomic_requests(wrapped)
        wrapped.user_import_api = not html
        wrapped.user_import_page = html
        return wrapped
    return decorate


def owned_batch(request, batch_id):
    batch = UserImportBatch.objects.filter(pk=batch_id, actor_id=request.user.pk).first()
    if batch is None:
        return
    now = timezone.now()
    validation_expired = (batch.status == UserImportBatch.Status.VALIDATING and
                          now >= batch.created_at + timedelta(seconds=require_user_import_enabled().password_seconds))
    if validation_expired or (batch.status == UserImportBatch.Status.PREVIEW_READY and now >= batch.preview_expires_at):
        with transaction.atomic():
            current = UserImportBatch.objects.select_for_update(skip_locked=True).filter(pk=batch.pk).first()
            if current:
                expire_import_preview(current)
                batch = current
        if current is None:
            # Submit may have won the race with preview expiry. Never wait for
            # its account transaction just to render an older preview state.
            batch = UserImportBatch.objects.get(pk=batch.pk, actor_id=request.user.pk)
    return batch


def serialize_batch(batch, page=1):
    config = require_user_import_enabled()
    report_expired = (batch.status in TERMINAL_IMPORTS and batch.finished_at and
                      timezone.now() >= batch.finished_at + timedelta(seconds=config.report_seconds))
    pages = 1 if report_expired else max(1, (batch.total_rows + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    rows = batch.rows.defer('password_ciphertext', 'prepared_password_ciphertext', 'baseline').all()
    if report_expired:
        rows = rows.none()
    validated = rows.filter(status__in=(UserImportRow.Status.VALID, UserImportRow.Status.INVALID)).count()
    prepared = rows.filter(input_data__password_provided=True, password_ciphertext='',
                           prepared_password_expires_at__isnull=False).count()
    return {
        'id': str(batch.pk), 'version': batch.version, 'idempotency_key': str(batch.idempotency_key),
        'status': batch.status, 'muted': batch.muted, 'total_rows': batch.total_rows,
        'applied_rows': batch.applied_rows, 'created_users': batch.created_users, 'updated_users': batch.updated_users,
        'validated_rows': validated, 'prepared_passwords': prepared, 'validation_attempts': batch.validation_attempts,
        'preview_expires_at': batch.preview_expires_at.isoformat(), 'errors': [] if report_expired else batch.errors,
        'status_url': reverse('user_import_status', args=[batch.pk]),
        'page': page, 'pages': pages, 'execution_available': not bool(report_expired),
        'execution_attempts': batch.execution_attempts, 'progress': read_import_progress(batch),
        'report_expired': bool(report_expired),
        'execution_dispatch_pending': batch.status == UserImportBatch.Status.QUEUED and not batch.task_id,
        'validation_dispatch_pending': (batch.status == UserImportBatch.Status.VALIDATING and
                                        not batch.task_id and batch.attempt_token is None),
        'rows': [{'row': row.row_number, 'line': row.line_number, 'username': row.username,
                  'action': row.action,
                  'status': 'VALID' if batch.status in ('QUEUED', 'RUNNING') else row.status,
                  'input': row.input_data, 'preview': row.preview, 'errors': row.errors,
                  'result': {} if batch.status in ('QUEUED', 'RUNNING') else row.result}
                 for row in rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]],
    }


def ui_strings():
    return {
        'network': _('Connection interrupted. The import key was kept. Check the same request before retrying.'),
        'missing': _('The server has not received this upload. Choose the same file to retry with the saved key.'),
        'unavailable': _('User import is temporarily unavailable. Retry the same request.'),
        'validating': _('Validating the CSV. No accounts have been changed.'),
        'ready': _('Preview ready. No accounts have been changed.'),
        'invalid': _('Correct all listed errors in your local CSV and upload it again. No accounts were changed.'),
        'closed': _('This import has ended. No accounts were changed.'),
        'completed': _('Import completed. Every account change was saved.'),
        'queued': _('Waiting to import. No accounts have been changed.'),
        'running': _('Import is running. Changes are not committed yet.'),
        'retry_upload': _('Validation is waiting for the queue. Retry the same CSV; the import key will be reused.'),
        'verifying': _('Checking the execution outcome. Keep this import open; do not start another import.'),
        'retry_submit': _('Retry import request'),
        'queue_wait': _('The import is confirmed and waiting for the queue. Retry with the same import key.'),
        'progress': _('Processing {processed}/{total}. These changes are not committed yet.'),
        'not_applied': _('Not applied'), 'saved': _('Saved'),
        'result': _('Saved: {total}. Created: {created}. Updated: {updated}.'),
        'report_expired': _('Detailed rows have expired. '
                            'This import result is retained to prevent duplicate execution.'),
        'mute_changed': _('Mute changed. Upload the CSV again to review a new preview before confirming.'),
        'create': _('Create'), 'update': _('Update'), 'unknown': _('Checking'),
        'provided': _('Provided'), 'keep': _('Keep unchanged'), 'on': _('Enabled'), 'off': _('Disabled'),
        'first_name': _('Name'), 'last_name': _('School'), 'email': _('Email'),
        'empty': _('Empty'), 'none': _('None'), 'line': _('CSV line'),
        'warning': _('Disabling password rotation does not restore a password already consumed by a login.'),
        'summary': _('Rows: {total}. Prepared passwords: {prepared}. Committed accounts: {applied}.'),
        'expiry': _('Confirm before: {time}'), 'page': _('Page {page} of {pages}'),
        'no_storage': _('Browser session storage is unavailable. Keep this page open to retry the same upload.'),
        'too_large': _('The CSV exceeds the upload size limit.'),
    }


@import_endpoint('GET', html=True)
@ensure_csrf_cookie
def index(request):
    batch = None
    pending_key = None
    if 'batch' in request.GET:
        batch = owned_batch(request, parse_idempotency_key(request.GET['batch']))
    elif 'operation_key' in request.GET:
        key = parse_idempotency_key(request.GET['operation_key'])
        pending_key = str(key)
        operation = UserImportOperation.objects.filter(
            actor=request.user, action=UserImportOperation.Action.UPLOAD, idempotency_key=key).first()
        if operation and operation.batch_id:
            batch = owned_batch(request, operation.batch_id)
    if batch is None and ('batch' in request.GET or (
            'operation_key' in request.GET and request.headers.get('Accept') == 'application/json')):
        return import_error('not_found', _('Import request not found.'), 404)
    initial = serialize_batch(batch) if batch else None
    if request.headers.get('Accept') == 'application/json':
        return JsonResponse({'batch': initial})
    config = require_user_import_enabled()
    client_config = {
        'index_url': reverse('user_import'), 'upload_url': reverse('user_import_post_file'),
        'submit_url': reverse('user_import_submit'), 'max_bytes': config.max_bytes, 'max_rows': config.max_rows,
        'storage_key': 'utcoj.user-import.v1.%s' % request.user.pk, 'initial_batch': initial,
        'strings': ui_strings(), 'pending_key': pending_key,
        'confirm_label': _('Confirm import'),
    }
    return render(request, 'user/import/index.html', {
        'title': _('Import users'), 'tab': 'import_users',
        'import_config_json': json.dumps(client_config),
        'csv_fields': CSV_FIELDS, 'max_rows': config.max_rows, 'max_bytes': config.max_bytes,
    })


@import_endpoint('GET')
def sample(request):
    response = HttpResponse(','.join(CSV_FIELDS) + '\r\n', content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="utcoj-users-sample.csv"'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@import_endpoint('POST')
@sensitive_variables()
def post_file(request):
    if getattr(request, 'user_import_upload_too_large', False):
        raise UserImportError('file_too_large', _('The CSV exceeds the upload size limit.'))
    if (request.content_type != 'multipart/form-data' or getattr(request, 'user_import_upload_invalid', False) or
            set(request.FILES) != {'csv_file'} or len(request.FILES.getlist('csv_file')) != 1 or
            not set(request.POST) <= {'muted', 'csrfmiddlewaretoken'} or len(request.POST.getlist('muted')) > 1):
        return import_error('invalid_request', _('Upload one UTF-8 CSV file.'), 422)
    key = parse_idempotency_key(request.headers.get('Idempotency-Key'))
    raw_muted = request.POST.get('muted', 'true')
    if raw_muted not in ('true', 'false'):
        return import_error('invalid_mute', _('The mute option must be true or false.'), 422)
    muted = raw_muted == 'true'
    data = read_import_csv(request.FILES['csv_file'])
    digest = upload_request_digest(data, muted)
    operation, created = reserve_import_upload(request.user, key, digest, data, muted)
    batch = owned_batch(request, operation.batch_id)
    available = dispatch_import_validation(batch.pk, batch.version)
    batch.refresh_from_db()
    payload = serialize_batch(batch)
    if not available:
        return import_error('unavailable', _('Validation could not be queued. Retry this upload with the same key.'),
                            503, batch=payload)
    response = JsonResponse({'batch': payload, 'replayed': not created}, status=202 if created else 200)
    response['Location'] = payload['status_url']
    return response


@import_endpoint('GET')
def status(request, batch_id):
    batch = owned_batch(request, batch_id)
    if batch is None:
        return import_error('not_found', _('Import request not found.'), 404)
    try:
        page = int(request.GET.get('page', '1'))
        if page < 1:
            raise ValueError()
    except ValueError:
        return import_error('invalid_page', _('Use a positive page number.'), 422)
    return JsonResponse({'batch': serialize_batch(batch, page)})


@import_endpoint('POST')
def submit(request):
    if request.content_type != 'application/json':
        return import_error('invalid_request', _('Expected a batch ID, version and confirmation.'), 422)
    try:
        body = request.body
        if len(body) > 16384:
            return import_error('file_too_large', _('The request exceeds the upload size limit.'), 413)
        data = json.loads(body)
        if (not isinstance(data, dict) or set(data) != {'batch_id', 'version', 'confirmed'} or
                type(data['version']) is not int or data['version'] < 1 or data['confirmed'] is not True):
            raise ValueError()
        batch_id = parse_idempotency_key(data['batch_id'])
    except (ValueError, UnicodeError):
        return import_error('invalid_request', _('Expected a batch ID, version and confirmation.'), 422)
    key = parse_idempotency_key(request.headers.get('Idempotency-Key'))
    batch = owned_batch(request, batch_id)
    if batch is None:
        return import_error('not_found', _('Import request not found.'), 404)
    if batch.version != data['version'] or batch.idempotency_key != key:
        return import_error('batch_conflict', _('Use this preview version and its original import key.'), 409)
    batch, replayed = reserve_import_submission(request.user, batch.pk, data['version'], key)
    available = dispatch_import_execution(batch.pk, batch.version) if batch.status == 'QUEUED' else True
    batch.refresh_from_db()
    payload = serialize_batch(batch)
    if not available:
        return import_error('unavailable', _('Import is confirmed but could not be queued. Retry the same request.'),
                            503, batch=payload)
    return JsonResponse({'batch': payload, 'replayed': replayed},
                        status=202 if batch.status in ('QUEUED', 'RUNNING') else 200)
