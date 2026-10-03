import base64
import json
import os
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import yaml
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connections
from django.middleware.csrf import get_token
from django.test import Client, LiveServerTestCase, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, \
    override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import ProblemData, ProblemDataEditSession, ProblemDataRevision, ProblemTestCase, TestDataUpload
from judge.models.tests.util import create_problem, create_user
from judge.tasks.test_data_upload import validate_test_data_upload
from judge.utils.problem_data_publication import apply_revision
from judge.utils.problem_data_storage import StorageManager
from judge.utils.test_data_upload import UploadError, cleanup_uploads, staging_lock, staging_path, validate_archive
from judge.views import test_data_upload as views
from judge.views.problem_data import ProblemDataView


def archive_bytes(entries=None):
    stream = BytesIO()
    with ZipFile(stream, 'w', ZIP_STORED) as archive:
        for name, content in (entries or [('1.in', b'1'), ('1.out', b'2')]):
            archive.writestr(name, content)
    return stream.getvalue()


@override_settings(DMOJ_TEST_UPLOAD_MAX_ENTRIES=10, DMOJ_TEST_UPLOAD_MAX_UNCOMPRESSED=100,
                   DMOJ_TEST_UPLOAD_VALIDATION_SECONDS=10)
class ArchiveValidationTests(SimpleTestCase):
    def test_valid_archive_ignores_desktop_metadata(self):
        data = archive_bytes([('1.in', b'1'), ('1.out', b'2'), ('__MACOSX/x', b''),
                              ('.DS_Store', b''), ('._1.in', b'')])
        self.assertEqual(validate_archive(BytesIO(data)), ['1.in', '1.out'])

    def test_unsafe_names_and_symlinks(self):
        names = ['../x', '/x', 'x/../y', './x', 'C:/x', 'x\\y']
        symlink = ZipInfo('link')
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for name in names + [symlink]:
            with self.subTest(name=name), self.assertRaises(UploadError):
                validate_archive(BytesIO(archive_bytes([(name, b'x')])))

    def test_duplicate_and_size_limits(self):
        with self.assertWarns(UserWarning):
            duplicate = archive_bytes([('x', b''), ('x', b'')])
        for data in (duplicate, archive_bytes([('big', b'a' * 101)]),
                     archive_bytes([(str(i), b'') for i in range(11)])):
            with self.assertRaises(UploadError):
                validate_archive(BytesIO(data))

    def test_crc_is_checked(self):
        data = archive_bytes([('x', b'unique-content')]).replace(b'unique-content', b'broken-content')
        with self.assertRaises(BadZipFile):
            validate_archive(BytesIO(data))

    @override_settings(DMOJ_TEST_UPLOAD_MAX_DIRECTORY_SIZE=1)
    def test_directory_limit_is_checked_before_loading_entries(self):
        with patch('judge.utils.test_data_upload.ZipFile') as constructor, self.assertRaises(UploadError):
            validate_archive(BytesIO(archive_bytes()))
        constructor.assert_not_called()


@override_settings(DMOJ_TEST_UPLOAD_ENABLED=True, DMOJ_TEST_UPLOAD_ACCEPT_NEW=True,
                   DMOJ_TEST_UPLOAD_DISK_RESERVE=0, DMOJ_TEST_UPLOAD_INTERNAL_SECRET='test-internal-secret',
                   DMOJ_TEST_UPLOAD_CLEANUP_GRACE_SECONDS=0)
class TestDataUploadTests(TestCase):
    fixtures = ['language_small']

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = override_settings(DMOJ_TEST_UPLOAD_ROOT=self.temp.name + '/staging',
                                        DMOJ_PROBLEM_DATA_ROOT=self.temp.name + '/problems',
                                        DMOJ_STORAGE_CONFIG_PATH=None)
        self.config.enable()
        self.addCleanup(self.config.disable)
        old_storage = StorageManager._instance
        StorageManager._instance = None
        self.addCleanup(setattr, StorageManager, '_instance', old_storage)
        self.user = create_user('upload-editor', is_superuser=True, is_staff=True)
        self.other = create_user('other-editor', is_superuser=True, is_staff=True)
        self.problem = create_problem('uploadtest')
        self.factory = RequestFactory()
        self.queue = patch('judge.tasks.test_data_upload.validate_test_data_upload.delay').start()
        self.addCleanup(patch.stopall)

    def request(self, view, body=None, user=None, method='post', **kwargs):
        request = getattr(self.factory, method)('/test', data=json.dumps(body or {}),
                                                content_type='application/json') if method == 'post' else \
            self.factory.get('/test', data=body or {})
        request.user = user or self.user
        return view(request, problem=self.problem.code, **kwargs)

    def session(self):
        response = self.request(views.create_session)
        self.assertEqual(response.status_code, 200, response.content)
        data = json.loads(response.content)['session']
        return ProblemDataEditSession.objects.get(pk=data['id'])

    def credentials(self, session):
        return {'edit_session_id': str(session.pk), 'edit_token': str(session.token)}

    def upload(self, session, content=None, status='CREATED'):
        content = content or archive_bytes()
        response = self.request(views.create_upload, dict(self.credentials(session),
                                                          filename='tests.zip', size=len(content)))
        self.assertEqual(response.status_code, 201, response.content)
        upload = TestDataUpload.objects.get(pk=json.loads(response.content)['upload']['id'])
        upload.status = status
        upload.entries = ['1.in', '1.out'] if status == 'READY' else []
        upload.save()
        if status != 'CREATED':
            Path(staging_path(upload)).write_bytes(content)
        return upload

    def hook(self, upload, event, info=None, secret='test-internal-secret'):
        request = self.factory.post('/internal/test-data-uploads/hooks/', data=json.dumps({
            'Type': event, 'Event': {'Upload': info if info is not None else {
                'ID': upload.id.hex, 'Size': upload.size, 'Offset': upload.size,
                'MetaData': {'upload_id': str(upload.pk)},
            }},
        }), content_type='application/json', HTTP_X_TEST_UPLOAD_TOKEN=str(upload.token),
            HTTP_X_UPLOAD_SECRET=secret)
        return views.tus_hook(request)

    def form_payload(self, session, upload=None):
        body = dict(self.credentials(session), **{
            'problem-data-checker': 'standard', 'problem-data-checker_type': 'testlib',
            'problem-data-grader': 'standard', 'problem-data-io_method': 'standard',
            'cases-TOTAL_FORMS': '1', 'cases-INITIAL_FORMS': '0',
            'cases-MAX_NUM_FORMS': '1000', 'cases-MIN_NUM_FORMS': '0',
            'cases-0-order': '1', 'cases-0-type': 'C', 'cases-0-points': '100',
            'cases-0-input_file': '1.in', 'cases-0-output_file': '1.out',
        })
        if upload:
            body['upload_id'] = str(upload.pk)
        return body

    def save_form(self, body):
        request = self.factory.post(reverse('problem_data', args=[self.problem.code]), body)
        request.user = self.user
        request.profile = self.user.profile
        request.in_contest = False
        return ProblemDataView.as_view()(request, problem=self.problem.code)

    def published_data(self):
        session = self.session()
        upload = self.upload(session, status='READY')
        self.assertEqual(self.save_form(self.form_payload(session, upload)).status_code, 302)
        return ProblemData.objects.get(problem=self.problem)

    def delete_all_payload(self, session, clear_zip=False):
        body = self.form_payload(session)
        body.update({
            'cases-INITIAL_FORMS': '1',
            'cases-0-id': str(self.problem.cases.get().pk),
            'cases-0-DELETE': 'on',
        })
        if clear_zip:
            body['problem-data-zipfile-clear'] = 'on'
        return body

    def test_view_does_not_create_data_or_lease(self):
        view = ProblemDataView()
        view.object, view.request = self.problem, self.factory.get('/')
        self.assertIsNone(view.get_data_form().instance.pk)
        self.assertFalse(ProblemData.objects.exists())
        self.assertFalse(ProblemDataEditSession.objects.exists())

    def test_only_one_session_even_for_same_user(self):
        session = self.session()
        for user in (self.user, self.other):
            response = self.request(views.create_session, user=user)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('token', json.loads(response.content)['session'])
        response = self.request(views.session_heartbeat, {'edit_token': str(session.token)},
                                user=self.other, session_id=session.pk)
        self.assertEqual(response.status_code, 403)

    def test_expiry_and_revoke_prevent_stale_writes(self):
        session = self.session()
        upload = self.upload(session)
        self.assertEqual(self.request(views.revoke_session, session_id=session.pk).status_code, 200)
        self.assertEqual(self.hook(upload, 'pre-create').status_code, 403)
        validate_test_data_upload.run(str(upload.pk))
        upload.refresh_from_db()
        self.assertEqual(upload.status, 'CANCELED')
        replacement = self.session()
        replacement.expires_at = timezone.now() - timedelta(seconds=1)
        replacement.save()
        self.session()
        replacement.refresh_from_db()
        self.assertEqual(replacement.status, 'EXPIRED')

    def test_hook_secret_and_creation_reservation(self):
        upload = self.upload(self.session())
        self.assertEqual(self.hook(upload, 'pre-create', secret='').status_code, 403)
        first = self.hook(upload, 'pre-create')
        self.assertEqual(json.loads(first.content)['ChangeFileInfo']['ID'], upload.id.hex)
        second = self.hook(upload, 'pre-create')
        self.assertTrue(json.loads(second.content)['RejectUpload'])
        upload.refresh_from_db()
        self.assertEqual(upload.status, 'CREATING')

    def test_authorization_binds_method_path_size_cookie_and_token(self):
        upload = self.upload(self.session())
        headers = {
            'HTTP_X_TEST_UPLOAD_TOKEN': str(upload.token), 'HTTP_X_UPLOAD_SECRET': 'test-internal-secret',
            'HTTP_X_ORIGINAL_METHOD': 'POST', 'HTTP_X_ORIGINAL_URI': '/uploads/',
            'HTTP_X_ORIGINAL_CONTENT_LENGTH': '0', 'HTTP_UPLOAD_LENGTH': str(upload.size),
            'HTTP_UPLOAD_METADATA': 'upload_id ' + base64.b64encode(str(upload.pk).encode()).decode(),
        }
        request = self.factory.get('/', **headers)
        request.user = self.user
        self.assertEqual(views.authorize_tus(request).status_code, 204)
        for changes in ({'HTTP_X_ORIGINAL_METHOD': 'GET'}, {'HTTP_X_ORIGINAL_URI': '/uploads/other'},
                        {'HTTP_X_ORIGINAL_CONTENT_LENGTH': '1'}, {'HTTP_UPLOAD_LENGTH': '1'},
                        {'HTTP_UPLOAD_CONCAT': 'partial'}, {'HTTP_X_UPLOAD_SECRET': ''}):
            request = self.factory.get('/', **dict(headers, **changes))
            request.user = self.user
            self.assertEqual(views.authorize_tus(request).status_code, 403)
        request = self.factory.get('/', **headers)
        request.user = self.other
        self.assertEqual(views.authorize_tus(request).status_code, 403)

    def test_lost_callback_recovered_and_listing_is_requested_separately(self):
        session = self.session()
        upload = self.upload(session, status='UPLOADING')
        Path(staging_path(upload) + '.info').write_text(json.dumps({'ID': upload.id.hex, 'Size': upload.size}))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.request(views.session_heartbeat, self.credentials(session), session_id=session.pk)
        self.assertNotIn('entries', json.loads(response.content)['uploads'][0])
        self.queue.assert_called_once_with(str(upload.pk))
        validate_test_data_upload.run(str(upload.pk))
        upload.refresh_from_db()
        self.assertEqual(upload.status, 'READY')
        self.assertEqual(upload.entries, ['1.in', '1.out'])
        self.assertEqual(self.hook(upload, 'post-finish').status_code, 200)
        self.assertEqual(self.queue.call_count, 1)

    def test_invalid_zip_does_not_change_active_data(self):
        session = self.session()
        upload = self.upload(session, content=b'not a zip', status='UPLOADED')
        validate_test_data_upload.run(str(upload.pk))
        upload.refresh_from_db()
        self.assertEqual(upload.status, 'FAILED')
        self.assertFalse(ProblemData.objects.exists())

    def test_corrupt_deflate_fails_validation_and_releases_reservation(self):
        stream = BytesIO()
        with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as archive:
            archive.writestr('1.in', b'content' * 20)
        content = bytearray(stream.getvalue())
        # First DEFLATE byte follows the 30-byte local header and filename.
        content[30 + len('1.in')] = 0x07  # Reserved block type, raises zlib.error.
        upload = self.upload(self.session(), content=bytes(content), status='UPLOADED')
        validate_test_data_upload.run(str(upload.pk))
        upload.refresh_from_db()
        self.assertEqual(upload.status, 'FAILED')
        self.assertEqual(upload.reserved_bytes, 0)

    def test_manually_managed_toggle_is_blocked_during_edit(self):
        self.session()
        self.problem.is_manually_managed = True
        with self.assertRaises(ValidationError):
            self.problem.save()
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_manually_managed)

    def test_soft_delete_allowed_after_published_edit_but_not_during_edit(self):
        session = self.session()
        upload = self.upload(session, status='READY')
        with self.assertRaises(ValidationError):
            self.problem.mark_as_deleted()
        self.problem.refresh_from_db()
        self.assertEqual(self.save_form(self.form_payload(session, upload)).status_code, 302)
        self.problem.mark_as_deleted()
        self.problem.refresh_from_db()
        self.assertIsNotNone(self.problem.deleted_at)

    def test_form_error_keeps_ready_zip_and_published_files(self):
        session = self.session()
        upload = self.upload(session, status='READY')
        body = self.form_payload(session, upload)
        body['cases-0-output_file'] = 'missing.out'
        result = self.save_form(body)
        self.assertEqual(result.status_code, 200)
        upload.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(upload.status, 'READY')
        self.assertEqual(session.status, 'ACTIVE')
        self.assertFalse(ProblemDataRevision.objects.exists())
        self.assertEqual(list(Path(self.temp.name + '/problems/uploadtest/_revisions').iterdir()), [])

    def test_save_is_idempotent_and_retains_previous_zip(self):
        with self.settings(DMOJ_TEST_UPLOAD_ENABLED=False):
            data = ProblemData(problem=self.problem)
            data.zipfile.save('old.zip', ContentFile(archive_bytes()))
        old = Path(data.zipfile.path)
        session = self.session()
        upload = self.upload(session, status='READY')
        body = self.form_payload(session, upload)
        for attempt in range(2):
            self.assertEqual(self.save_form(body).status_code, 302, attempt)
        self.assertEqual(ProblemDataRevision.objects.count(), 1)
        self.assertTrue(old.exists())
        data.refresh_from_db()
        init = Path(self.temp.name + '/problems/uploadtest/init.yml')
        self.assertEqual(yaml.safe_load(init.read_text())['archive'], data.zipfile.name.split('/', 1)[1])
        self.assertEqual(stat.S_IMODE(init.stat().st_mode), 0o644)
        self.assertEqual(ProblemTestCase.objects.filter(dataset=self.problem).count(), 1)
        upload.refresh_from_db()
        self.assertEqual(upload.status, 'APPLIED')
        self.assertEqual(upload.reserved_bytes, 0)
        cleanup_uploads()
        self.assertFalse(Path(staging_path(upload)).exists())
        self.assertTrue(Path(data.zipfile.path).exists())

    def test_clear_zip_without_replacement_removes_active_data(self):
        data = self.published_data()
        old_file = Path(data.zipfile.path)
        session = self.session()
        body = self.delete_all_payload(session, clear_zip=True)
        # Removing active data must remain available when no upload disk budget
        # remains; this save does not copy a replacement ZIP.
        with self.settings(DMOJ_TEST_UPLOAD_DISK_RESERVE=10 ** 20):
            for attempt in range(2):
                self.assertEqual(self.save_form(body).status_code, 302, attempt)
        data.refresh_from_db()
        session.refresh_from_db()
        self.assertFalse(data.zipfile)
        self.assertEqual(data.zipfile_size, 0)
        self.assertFalse(self.problem.cases.exists())
        self.assertFalse(data.has_yml())
        self.assertEqual(session.status, 'APPLIED')
        self.assertEqual(TestDataUpload.objects.count(), 1)
        revision = ProblemDataRevision.objects.get(session=session)
        self.assertEqual(revision.previous_data['zipfile'],
                         old_file.relative_to(Path(self.temp.name) / 'problems').as_posix())
        self.assertEqual(revision.init, '')
        self.assertEqual(revision.cases, [])
        self.assertEqual(revision.status, 'APPLIED')
        # An in-flight judge may still hold the old immutable archive.
        self.assertTrue(old_file.exists())
        self.session()

    def test_delete_all_cases_without_clear_checkbox_removes_archive(self):
        data = self.published_data()
        session = self.session()
        self.assertEqual(self.save_form(self.delete_all_payload(session)).status_code, 302)
        data.refresh_from_db()
        self.problem.refresh_from_db()
        self.assertFalse(data.zipfile)
        self.assertFalse(self.problem.cases.exists())
        self.assertFalse(data.has_yml())
        metadata = data.zipfile.storage.get_problem_metadata(self.problem)
        self.assertEqual(metadata['files'], [])
        self.assertEqual(metadata['testcases'], {})

    def test_empty_cases_with_replacement_zip_keeps_current_dataset(self):
        data = self.published_data()
        old_archive = data.zipfile.name
        old_init = Path(self.temp.name + '/problems/uploadtest/init.yml').read_text()
        session = self.session()
        upload = self.upload(session, status='READY')
        body = self.delete_all_payload(session)
        body['upload_id'] = str(upload.pk)
        self.assertEqual(self.save_form(body).status_code, 200)
        data.refresh_from_db()
        session.refresh_from_db()
        upload.refresh_from_db()
        self.assertEqual(data.zipfile.name, old_archive)
        self.assertEqual(Path(data.zipfile.path).read_bytes(), archive_bytes())
        self.assertEqual(Path(self.temp.name + '/problems/uploadtest/init.yml').read_text(), old_init)
        self.assertEqual(self.problem.cases.count(), 1)
        self.assertEqual(session.status, 'ACTIVE')
        self.assertEqual(upload.status, 'READY')

    def test_interrupted_clear_recovers_without_restoring_old_tests(self):
        data = self.published_data()
        old_archive = data.zipfile.name
        session = self.session()
        original_save = ProblemDataRevision.save

        def fail_after_clear(revision, *args, **kwargs):
            if revision.status == 'APPLIED':
                raise OSError('simulated crash after removing init.yml')
            return original_save(revision, *args, **kwargs)

        with patch.object(ProblemDataRevision, 'save', fail_after_clear):
            self.assertEqual(self.save_form(self.delete_all_payload(session, clear_zip=True)).status_code, 200)
        data.refresh_from_db()
        self.assertEqual(data.zipfile.name, old_archive)
        self.assertEqual(self.problem.cases.count(), 1)
        self.assertFalse(data.has_yml())
        revision = ProblemDataRevision.objects.get(session=session)
        self.assertEqual(revision.status, 'PREPARED')
        # The filesystem switch already committed: complete the journal even if
        # the editor loses permission before recovery, as for a normal upload.
        self.user.is_superuser = False
        self.user.save(update_fields=['is_superuser'])
        call_command('recover_test_data_uploads', stdout=StringIO(), verbosity=0)
        call_command('recover_test_data_uploads', stdout=StringIO(), verbosity=0)
        data.refresh_from_db()
        revision.refresh_from_db()
        session.refresh_from_db()
        self.assertFalse(data.zipfile)
        self.assertFalse(self.problem.cases.exists())
        self.assertFalse(data.has_yml())
        self.assertEqual(revision.status, 'APPLIED')
        self.assertEqual(session.status, 'APPLIED')

    def test_failure_after_filesystem_switch_can_recover(self):
        session = self.session()
        upload = self.upload(session, status='READY')
        original_save = ProblemDataRevision.save

        def fail_after_publish(revision, *args, **kwargs):
            if revision.status == 'APPLIED':
                raise OSError('simulated crash after replacing init.yml')
            return original_save(revision, *args, **kwargs)

        with patch.object(ProblemDataRevision, 'save', fail_after_publish):
            self.assertEqual(self.save_form(self.form_payload(session, upload)).status_code, 200)
        revision = ProblemDataRevision.objects.get()
        self.assertEqual(revision.status, 'PREPARED')
        self.assertFalse(ProblemData.objects.exists())
        with staging_lock('publish-%s' % self.problem.pk):
            apply_revision(revision.pk)
            apply_revision(revision.pk)
        self.assertEqual(ProblemTestCase.objects.count(), 1)
        session.refresh_from_db()
        self.assertEqual(session.status, 'APPLIED')

    def test_legacy_writes_and_rename_are_guarded(self):
        session = self.session()
        data = ProblemData(problem=self.problem, checker='floats')
        with self.assertRaises(ValidationError):
            data.save()
        self.assertFalse(ProblemData.objects.exists())
        self.problem.code = 'renamed'
        with self.assertRaises(ValidationError):
            self.problem.save()
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.code, 'uploadtest')
        response = self.request(views.end_session, self.credentials(session), session_id=session.pk)
        self.assertEqual(response.status_code, 200)

    def test_cleanup_skips_running_validator_and_tusd(self):
        session = self.session()
        upload = self.upload(session, status='UPLOADED')
        self.request(views.cancel_upload, self.credentials(session), upload_id=upload.pk)
        path = Path(staging_path(upload))
        with staging_lock('file-%s' % upload.id.hex):
            self.assertEqual(cleanup_uploads()['removed_files'], 0)
        Path(str(path) + '.lock').touch()
        self.assertEqual(cleanup_uploads()['removed_files'], 0)
        Path(str(path) + '.lock').unlink()
        self.assertEqual(cleanup_uploads(dry_run=True)['removed_files'], 1)
        self.assertTrue(path.exists())
        self.assertEqual(cleanup_uploads()['removed_files'], 1)

    def test_rollout_switch_does_not_interrupt_existing_sessions(self):
        session = self.session()
        with self.settings(DMOJ_TEST_UPLOAD_ACCEPT_NEW=False):
            self.assertEqual(self.request(views.create_session).status_code, 503)
            self.assertEqual(self.request(views.session_heartbeat, self.credentials(session),
                                          session_id=session.pk).status_code, 200)

    def test_disk_floor_and_global_transfer_capacity(self):
        session = self.session()
        with self.settings(DMOJ_TEST_UPLOAD_DISK_RESERVE=10 ** 20):
            response = self.request(views.create_upload, dict(self.credentials(session), filename='a.zip', size=10))
        self.assertEqual(response.status_code, 507)
        self.upload(session)
        other = create_problem('uploadother')
        self.problem = other
        second = self.session()
        with self.settings(DMOJ_TEST_UPLOAD_MAX_ACTIVE=1):
            response = self.request(views.create_upload, dict(self.credentials(second), filename='a.zip', size=10))
        self.assertEqual(response.status_code, 429)

    def test_missing_staging_does_not_end_the_edit_session(self):
        session = self.session()
        with self.settings(DMOJ_TEST_UPLOAD_ROOT=None):
            response = self.request(views.create_upload,
                                    dict(self.credentials(session), filename='tests.zip', size=512))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(json.loads(response.content)['error']['code'], 'not_configured')
            self.assertFalse(session.uploads.exists())
            heartbeat = self.request(views.session_heartbeat, self.credentials(session), session_id=session.pk)
            self.assertEqual(heartbeat.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, 'ACTIVE')
        self.upload(session)

    def test_disabled_user_and_csrf_are_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse('test_data_edit_session', args=[self.problem.code]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'csrf_failed')
        session = self.session()
        upload = self.upload(session)
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.hook(upload, 'pre-create').status_code, 403)

    def test_edit_and_upload_lifecycle_through_public_urls(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        csrf_token = get_token(self.factory.get('/'))
        client.cookies[settings.CSRF_COOKIE_NAME] = csrf_token
        base = '/problem/%s/data/uploads/' % self.problem.code

        def post(path, body=None, status=200):
            response = client.post(base + path, data=json.dumps(body or {}), content_type='application/json',
                                   HTTP_X_CSRFTOKEN=csrf_token)
            self.assertEqual(response.status_code, status, response.content)
            return response.json()

        session = post('session/')['session']
        credentials = {'edit_session_id': session['id'], 'edit_token': session['token']}
        heartbeat = post('session/%s/heartbeat/' % session['id'], credentials)
        self.assertEqual(heartbeat['session']['id'], session['id'])
        upload = post('files/', dict(credentials, filename='tests.zip', size=len(archive_bytes())),
                      status=201)['upload']
        response = client.get(base + 'files/%s/' % upload['id'],
                              HTTP_X_EDIT_SESSION=session['id'], HTTP_X_EDIT_TOKEN=session['token'])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['upload']['status'], 'CREATED')
        canceled = post('files/%s/cancel/' % upload['id'], credentials)
        self.assertEqual(canceled['upload']['status'], 'CANCELED')
        active = post('session/%s/heartbeat/' % session['id'], credentials)
        self.assertEqual(active['session']['status'], 'ACTIVE')
        ended = post('session/%s/end/' % session['id'], credentials)
        self.assertEqual(ended['session']['status'], 'ENDED')
        replacement = post('session/')['session']
        revoked = post('session/%s/revoke/' % replacement['id'])
        self.assertEqual(revoked['session']['status'], 'REVOKED')

    @override_settings(DEBUG=True, DMOJ_TEST_UPLOAD_LOCAL_PROXY_URL='http://127.0.0.1:8080')
    def test_local_data_editor_redirects_to_upload_proxy(self):
        client = Client()
        client.force_login(self.user)
        path = reverse('problem_data', args=[self.problem.code]) + '?test-data-saved=1'
        response = client.get(path, HTTP_HOST='127.0.0.1:8000')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, 'http://127.0.0.1:8080' + path)
        response = client.get(path, HTTP_HOST='127.0.0.1:8080')
        self.assertEqual(response.status_code, 200)
        with self.settings(DEBUG=False):
            response = client.get(path, HTTP_HOST='127.0.0.1:8000')
        self.assertEqual(response.status_code, 200)

    def test_checker_only_edit_and_ordered_cases(self):
        session = self.session()
        upload = self.upload(session, status='READY')
        self.assertEqual(self.save_form(self.form_payload(session, upload)).status_code, 302)
        old_file = ProblemData.objects.get(problem=self.problem).zipfile.path
        session = self.session()
        body = self.form_payload(session)
        body['problem-data-checker'] = 'identical'
        body['cases-TOTAL_FORMS'] = '2'
        body['cases-0-order'] = '2'
        for name, value in list(body.items()):
            if name.startswith('cases-0-'):
                body[name.replace('cases-0-', 'cases-1-')] = value
        body['cases-1-order'] = '1'
        body['cases-1-points'] = '25'
        self.assertEqual(self.save_form(body).status_code, 302)
        revision = ProblemDataRevision.objects.get(session=session)
        init = yaml.safe_load(revision.init)
        self.assertEqual(init['checker'], 'identical')
        self.assertEqual([case['points'] for case in init['test_cases']], [25, 100])
        self.assertTrue(Path(old_file).exists())
        self.assertEqual(ProblemData.objects.get(problem=self.problem).zipfile.path, old_file)

    def test_custom_checker_reference_uses_immutable_copy(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        session = self.session()
        upload = self.upload(session, status='READY')
        body = self.form_payload(session, upload)
        body.update({
            'problem-data-checker': 'bridged',
            'problem-data-custom_checker': SimpleUploadedFile('check.cpp', b'int main() {}'),
            'problem-data-checker_args': json.dumps({'files': 'check.cpp', 'type': 'testlib'}),
        })
        self.assertEqual(self.save_form(body).status_code, 302)
        revision = ProblemDataRevision.objects.get()
        checker = yaml.safe_load(revision.init)['checker']['args']['files']
        self.assertTrue(checker.startswith('_revisions/'))
        self.assertTrue(Path(self.temp.name + '/problems/uploadtest/' + checker).is_file())

    def test_abandoned_preparation_recovery_releases_reservations(self):
        session = self.session()
        upload = self.upload(session, status='APPLYING')
        session.status = 'APPLYING'
        session.last_activity = timezone.now() - timedelta(hours=1)
        session.save()
        abandoned = Path(self.temp.name) / 'problems' / self.problem.code / '_revisions' / uuid.uuid4().hex
        abandoned.mkdir(parents=True)
        (abandoned / '.preparation').write_text(str(session.pk))
        (abandoned / 'zipfile.zip').write_bytes(b'partial upload')
        call_command('recover_test_data_uploads', stdout=StringIO(), verbosity=0)
        upload.refresh_from_db()
        self.assertEqual(upload.status, 'EXPIRED')
        self.assertEqual(upload.reserved_bytes, 0)
        self.assertFalse(abandoned.exists())

    def test_cleanup_only_removes_old_uuid_orphans(self):
        session = self.session()
        upload = self.upload(session, status='READY')
        root = Path(staging_path(upload)).parent
        orphan = root / uuid.uuid4().hex
        unrelated = root / 'operators-note.txt'
        for path in (orphan, unrelated):
            path.write_text('keep unknown names')
            os.utime(path, (0, 0))
        self.assertEqual(cleanup_uploads()['removed_files'], 1)
        self.assertFalse(orphan.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(Path(staging_path(upload)).exists())


@override_settings(DMOJ_TEST_UPLOAD_ENABLED=True, DMOJ_TEST_UPLOAD_ACCEPT_NEW=True)
class ConcurrentEditSessionTests(TransactionTestCase):
    fixtures = ['language_small']
    setUp = TestDataUploadTests.setUp
    request = TestDataUploadTests.request

    def test_two_concurrent_requests_only_grant_one_lease(self):
        barrier = threading.Barrier(2)

        def acquire(user):
            try:
                barrier.wait(timeout=5)
                return self.request(views.create_session, user=user).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(acquire, (self.user, self.other)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(ProblemDataEditSession.objects.filter(status='ACTIVE').count(), 1)


@override_settings(DMOJ_TEST_UPLOAD_ENABLED=True, DMOJ_TEST_UPLOAD_ACCEPT_NEW=True,
                   DMOJ_TEST_UPLOAD_DISK_RESERVE=0, DMOJ_TEST_UPLOAD_INTERNAL_SECRET='test-internal-secret')
class TusdIntegrationTests(LiveServerTestCase):
    """Optional real tusd test: TUSD_BINARY=/path/to/tusd python manage.py test ..."""

    fixtures = ['language_small']
    request = TestDataUploadTests.request
    session = TestDataUploadTests.session
    upload = TestDataUploadTests.upload
    credentials = TestDataUploadTests.credentials

    def setUp(self):
        binary = os.environ.get('TUSD_BINARY')
        if not binary:
            self.skipTest('Set TUSD_BINARY to run the real tusd integration check.')
        TestDataUploadTests.setUp(self)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.url = 'http://127.0.0.1:%s/uploads/' % port
        log = tempfile.TemporaryFile()
        self.addCleanup(log.close)
        process = subprocess.Popen([
            binary, '-host=127.0.0.1', '-port=%s' % port, '-base-path=/uploads/',
            '-upload-dir=%s/staging' % self.temp.name, '-disable-download', '-disable-termination',
            '-behind-proxy', '-max-size=%s' % settings.DMOJ_TEST_UPLOAD_MAX_SIZE,
            '-hooks-http=%s/internal/test-data-uploads/hooks/' % self.live_server_url,
            '-hooks-http-forward-headers=X-Test-Upload-Token,X-Upload-Secret',
            '-hooks-enabled-events=pre-create,post-create,post-finish,post-receive',
            '-progress-hooks-interval=30s',
        ], stdout=log, stderr=log)

        def stop():
            process.terminate()
            process.wait(timeout=10)

        self.addCleanup(stop)
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    break
            except OSError:
                if process.poll() is not None:
                    log.seek(0)
                    self.fail(log.read().decode())
                time.sleep(0.05)
        else:
            self.fail('tusd did not start')

    def tus(self, upload, method, body=None, url=None, **headers):
        request = Request(url or self.url, data=body, method=method, headers={
            'Tus-Resumable': '1.0.0', 'X-Test-Upload-Token': str(upload.token),
            'X-Upload-Secret': 'test-internal-secret',
            **({'Cookie': self.proxy_cookie} if hasattr(self, 'proxy_cookie') else {}), **headers,
        })
        try:
            return urlopen(request, timeout=30)
        except HTTPError as error:
            if error.code >= 500:
                proxy_log = getattr(self, 'proxy_error_log', None)
                detail = proxy_log.read_text() if proxy_log and proxy_log.exists() else ''
                self.fail('tusd error: ' + error.read().decode() + '\n' + detail)
            raise

    def test_real_tusd_chunks_resume_and_duplicate_creation(self):
        # A 306 MiB ZIP can be requested for the deployment acceptance fixture.
        size = int(os.environ.get('TEST_UPLOAD_MIB', '17')) * 1024 * 1024
        archive = archive_bytes([('1.in', b'x' * size), ('1.out', b'2')])
        session = self.session()
        upload = self.upload(session, content=archive)
        headers = {'Upload-Length': str(upload.size),
                   'Upload-Metadata': 'upload_id ' + base64.b64encode(str(upload.pk).encode()).decode()}
        with self.tus(upload, 'POST', b'', **headers) as response:
            self.assertEqual(response.status, 201)
            location = response.headers['Location']
        self.assertTrue(location.endswith('/uploads/' + upload.id.hex))
        self.assertEqual(urlsplit(location).scheme, urlsplit(self.url).scheme)
        self.assertEqual(urlsplit(location).netloc, urlsplit(self.url).netloc)
        chunk_size = 16 * 1024 * 1024
        offset = 0
        while offset < len(archive):
            part = archive[offset:offset + chunk_size]
            with self.tus(upload, 'PATCH', part, location, **{
                'Upload-Offset': str(offset), 'Content-Type': 'application/offset+octet-stream',
            }) as response:
                offset = int(response.headers['Upload-Offset'])
            # HEAD on a new connection supplies the authoritative resume offset.
            with self.tus(upload, 'HEAD', url=location) as response:
                self.assertEqual(int(response.headers['Upload-Offset']), offset)
            if offset == chunk_size:
                with self.assertRaises(HTTPError) as error:
                    self.tus(upload, 'POST', b'', **headers)
                self.assertEqual(error.exception.code, 403 if hasattr(self, 'proxy_cookie') else 409)
                self.assertEqual(Path(staging_path(upload)).stat().st_size, offset)
                # A retry with the old offset must not append the chunk twice.
                with self.assertRaises(HTTPError) as error:
                    self.tus(upload, 'PATCH', part[:1], location, **{
                        'Upload-Offset': '0', 'Content-Type': 'application/offset+octet-stream',
                    })
                self.assertEqual(error.exception.code, 409)
        for _ in range(100):
            upload.refresh_from_db()
            if upload.status == 'UPLOADED':
                break
            time.sleep(0.05)
        self.assertEqual(upload.status, 'UPLOADED')
        self.assertEqual(Path(staging_path(upload)).read_bytes(), archive)
        validate_test_data_upload.run(str(upload.pk))
        upload.refresh_from_db()
        self.assertEqual(upload.status, 'READY')


class NginxTusdIntegrationTests(TusdIntegrationTests):
    """Run the first-upload/resume checks through the real authenticated proxy."""

    def setUp(self):
        nginx = os.environ.get('NGINX_BINARY')
        if not nginx:
            self.skipTest('Set NGINX_BINARY and TUSD_BINARY to check the real upload proxy.')
        super().setUp()
        upstream = self.url.removesuffix('/uploads/')
        root = Path(self.temp.name) / 'nginx'
        root.mkdir()
        self.proxy_error_log = root / 'error.log'
        secret = root / 'secret.conf'
        secret.write_text('proxy_set_header X-Upload-Secret "test-internal-secret";\n')
        snippet = (Path(settings.BASE_DIR) / 'docs/deployment/test-data-upload/nginx-uploads.conf').read_text()
        snippet = snippet.replace('/etc/nginx/snippets/upload-secret.conf', str(secret))
        snippet = snippet.replace('http://site:8000', self.live_server_url)
        snippet = snippet.replace('http://tusd:1080', upstream)
        (root / 'uploads.conf').write_text(snippet)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        config = root / 'nginx.conf'
        config.write_text("""pid %(root)s/nginx.pid;
error_log %(root)s/error.log;
events {}
http {
    access_log off;
    client_body_temp_path %(root)s/body;
    proxy_temp_path %(root)s/proxy;
    fastcgi_temp_path %(root)s/fastcgi;
    uwsgi_temp_path %(root)s/uwsgi;
    scgi_temp_path %(root)s/scgi;
    server {
        listen 127.0.0.1:%(port)s;
        include %(root)s/uploads.conf;
        location / { return 404; }
    }
}
""" % {'root': root, 'port': port})
        client = Client()
        client.force_login(self.user)
        cookie = client.cookies[settings.SESSION_COOKIE_NAME]
        self.proxy_cookie = '%s=%s' % (cookie.key, cookie.value)
        self.url = 'http://127.0.0.1:%s/uploads/' % port
        log = tempfile.TemporaryFile()
        self.addCleanup(log.close)
        process = subprocess.Popen([nginx, '-g', 'daemon off;', '-p', str(root), '-c', str(config)],
                                   stdout=log, stderr=log)

        def stop():
            process.terminate()
            process.wait(timeout=10)

        self.addCleanup(stop)
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    return
            except OSError:
                if process.poll() is not None:
                    log.seek(0)
                    self.fail(log.read().decode())
                time.sleep(0.05)
        self.fail('Nginx did not start')
