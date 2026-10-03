"""Prepare immutable problem files, then publish a recoverable revision.

Database transactions cannot roll back filesystem changes. The journal is committed first;
recovery finishes the same revision if a process dies between filesystem and DB.
Old files are deliberately retained until operators confirm no judge uses them.
"""
import copy
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import yaml
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext

from judge.models import Problem, ProblemData, ProblemDataRevision, ProblemTestCase, problem_data_storage
from judge.utils.problem_data import ProblemDataCompiler

_writing_problem = ContextVar('test_data_writing_problem', default=None)
FILE_FIELDS = ('zipfile', 'generator', 'custom_checker', 'custom_grader', 'custom_header')


@contextmanager
def publication_writes(problem_id):
    token = _writing_problem.set(problem_id)
    try:
        yield
    finally:
        _writing_problem.reset(token)


def guard_test_data_write(problem_id):
    if _writing_problem.get() == problem_id or not getattr(settings, 'DMOJ_TEST_UPLOAD_ENABLED', False):
        return
    from judge.utils.test_data_upload import UploadError, assert_problem_unlocked
    try:
        assert_problem_unlocked(Problem.objects.select_for_update().get(pk=problem_id))
    except UploadError as error:
        raise ValidationError(gettext(str(error)))
    if ProblemDataRevision.objects.filter(problem_id=problem_id).exists():
        raise ValidationError(gettext('Use the test data editor to update a versioned problem.'))


@contextmanager
def guarded_data_change(problem_id):
    if not getattr(settings, 'DMOJ_TEST_UPLOAD_ENABLED', False):
        yield
        return
    with transaction.atomic():
        guard_test_data_write(problem_id)
        yield


class CaseList(list):
    def count(self):
        return len(self)


def _snapshot(instance, exclude=()):
    result = {}
    for field in instance._meta.concrete_fields:
        if field.name in exclude or field.primary_key:
            continue
        value = getattr(instance, field.attname)
        result[field.attname] = (value.name or '') if isinstance(field, models.FileField) else value
    return result


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path, content):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix='.publish-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), settings.FILE_UPLOAD_PERMISSIONS or 0o644)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _atomic_remove(path):
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _fsync_dir(path.parent)


def _replace_file_references(value, replacements):
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_file_references(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_file_references(item, replacements) for key, item in value.items()}
    return value


def _copy_files(problem, data, upload, revision_id, session_id):
    """All referenced executable/support files must be immutable too."""
    from judge.utils.test_data_upload import staging_path

    directory = Path(problem_data_storage.path(problem.code))
    directory.mkdir(parents=True, exist_ok=True)
    sources = {}
    for field in FILE_FIELDS:
        value = getattr(data, field)
        if field == 'zipfile' and upload:
            sources[field] = (Path(staging_path(upload)), '.zip')
        elif value and not value._committed:
            sources[field] = (value.file, Path(value.name).suffix.lower())
        elif value:
            # Revisions are immutable. Editing points/checker settings must not
            # duplicate an unchanged multi-gigabyte ZIP every time.
            relative_name = value.name.split('/', 1)
            if len(relative_name) == 2 and relative_name[0] == problem.code:
                parts = Path(relative_name[1]).parts
                if len(parts) == 3 and parts[0] == '_revisions':
                    try:
                        immutable_id = uuid.UUID(hex=parts[1])
                    except ValueError:
                        immutable_id = None
                    if immutable_id and immutable_id.hex == parts[1]:
                        if not Path(value.path).is_file():
                            raise ValidationError(gettext('An existing test data attachment is missing.'))
                        continue
            sources[field] = (Path(value.path), Path(value.name).suffix.lower())
    required = sum(source.stat().st_size if isinstance(source, Path) else source.size
                   for source, _ in sources.values())
    reserve = getattr(settings, 'DMOJ_TEST_UPLOAD_DISK_RESERVE', 5 * 1024 ** 3)
    # Clearing data and reusing immutable files allocate no new file bytes.
    if required and shutil.disk_usage(directory).free < reserve + required:
        raise ValidationError(gettext('Not enough disk space to prepare the new test data.'))
    relative = Path('_revisions') / revision_id.hex
    target_dir = directory / relative
    target_dir.mkdir(parents=True)
    _atomic_write(target_dir / '.preparation', str(session_id).encode())
    replacements = {}
    for field, (source, suffix) in sources.items():
        if suffix not in ('', '.zip', '.cpp', '.h', '.pas', '.java', '.py'):
            raise ValidationError(gettext('Unsupported test data attachment extension.'))
        destination = target_dir / (field + suffix)
        with destination.open('xb') as output:
            if isinstance(source, Path):
                with source.open('rb') as input_file:
                    shutil.copyfileobj(input_file, output, length=1024 * 1024)
            else:
                source.seek(0)
                for chunk in source.chunks():
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        old_name = getattr(data, field).name
        new_relative = (relative / destination.name).as_posix()
        if old_name:
            replacements[old_name] = new_relative
            replacements[old_name.split('/', 1)[-1]] = new_relative
            replacements[Path(old_name).name] = new_relative
        setattr(data, field, '%s/%s' % (problem.code, new_relative))
    _fsync_dir(target_dir)
    _fsync_dir(target_dir.parent)
    for field in ('checker_args', 'grader_args'):
        value = getattr(data, field)
        if value:
            setattr(data, field, json.dumps(_replace_file_references(json.loads(value), replacements)))


def prepare_revision(problem, user, session_id, token, upload_id, data_form, cases_formset):
    from judge.models import ProblemDataEditSession
    from judge.utils.test_data_upload import owned_session, ready_upload

    # Mark APPLYING durably before doing IO, so expiry/revocation cannot steal the lease.
    with transaction.atomic():
        problem = Problem.objects.select_for_update().get(pk=problem.pk)
        existing = ProblemDataRevision.objects.filter(session_id=session_id, problem=problem).first()
        if existing:
            session = existing.session
            if session.user_id != user.pk or str(session.token) != str(token):
                raise ValidationError(gettext('Invalid editing session.'))
            if existing.status in ('PREPARED', 'APPLYING', 'APPLIED'):
                return existing
            raise ValidationError(gettext('This publication failed. Start a new editing session.'))
        session = owned_session(problem, user, session_id, token, for_update=True)
        upload = ready_upload(session, upload_id, for_update=True) if upload_id else None
        session.status = 'APPLYING'
        session.save(update_fields=['status'])
        if upload:
            upload.status = 'APPLYING'
            upload.save(update_fields=['status'])

    revision_id = uuid.uuid4()
    try:
        data = copy.copy(data_form.instance)
        cases = CaseList()
        for form in cases_formset.forms:
            if form.cleaned_data and not form.cleaned_data.get('DELETE'):
                original = form.cleaned_data.get('id')
                if original and original.dataset_id != problem.pk:
                    raise ValidationError(gettext('Invalid testcase.'))
                case = copy.copy(form.instance)
                if case.pk and not ProblemTestCase.objects.filter(pk=case.pk, dataset=problem).exists():
                    raise ValidationError(gettext('Invalid testcase.'))
                case.dataset_id = problem.pk
                cases.append(case)
        if not cases and upload:
            raise ValidationError(gettext('Add at least one testcase before saving the test data.'))
        if not cases:
            # Clearing the final testcase removes the active archive too, so a
            # reload cannot auto-fill the deleted cases from the previous ZIP.
            data.zipfile = ''
        cases.sort(key=lambda case: case.order)
        old_data = ProblemData.objects.filter(problem=problem).first() or ProblemData(problem=problem)
        previous = _snapshot(old_data, exclude=('problem',))
        from judge.utils.test_data_upload import staging_lock
        with staging_lock('admission'):
            _copy_files(problem, data, upload, revision_id, session.pk)
        files = []
        if cases:
            files = upload.entries if upload else problem_data_storage.get_problem_metadata(problem)['files']
        init = ProblemDataCompiler(problem, data, cases, files, persist=False).make_init()
        if not init and cases:
            raise ValidationError(gettext('The test configuration is empty.'))
        data.feedback = ''
        # Empty journals remove init.yml, matching the legacy editor. Writing an
        # empty YAML file would leave the judge's previous test cache active.
        init_content = yaml.safe_dump(init) if init else ''
        init_path = Path(problem_data_storage.path('%s/init.yml' % problem.code))
        old_init = init_path.read_text() if init_path.exists() else None
        # Retain old files even when django-cleanup observes a FileField replacement.
        marker = init_path.parent / '.utcoj-retain-revisions'
        _atomic_write(marker, b'Files retained for running judges; use the revision recovery runbook.\n')
        with transaction.atomic():
            Problem.objects.select_for_update().get(pk=problem.pk)
            session = ProblemDataEditSession.objects.select_for_update().get(pk=session.pk)
            if session.status != 'APPLYING':
                raise ValidationError(gettext('The editing session no longer owns publication.'))
            from judge.utils.test_data_upload import check_editor
            check_editor(problem, session.user)
            return ProblemDataRevision.objects.create(
                id=revision_id, problem=problem, session=session, upload=upload,
                previous_data=previous, data=_snapshot(data, exclude=('problem',)),
                cases=[dict(_snapshot(case, exclude=('dataset',)), id=case.pk) for case in cases],
                previous_init=old_init, init=init_content,
            )
    except Exception:
        # Nothing has changed in the active dataset. Keep a valid ZIP ready for correction.
        with transaction.atomic():
            Problem.objects.select_for_update().get(pk=problem.pk)
            ProblemDataEditSession.objects.filter(pk=session.pk, status='APPLYING').update(status='ACTIVE')
            if upload:
                type(upload).objects.filter(pk=upload.pk, status='APPLYING').update(status='READY')
        # This directory belongs solely to this unpublished attempt.
        target = Path(problem_data_storage.path(problem.code)) / '_revisions' / revision_id.hex
        if target.exists() and not ProblemDataRevision.objects.filter(pk=revision_id).exists():
            shutil.rmtree(target)
        raise


def _apply_revision(revision_id):
    """Idempotent, forward recovery of a previously prepared publication."""
    from django_cleanup.cleanup import refresh

    with transaction.atomic():
        revision = ProblemDataRevision.objects.select_related('problem').get(pk=revision_id)
        problem = Problem.objects.select_for_update().get(pk=revision.problem_id)
        revision = ProblemDataRevision.objects.select_for_update().get(pk=revision_id)
        if revision.status == 'APPLIED':
            return revision
        if revision.status not in ('PREPARED', 'APPLYING') or revision.session.status != 'APPLYING':
            raise ValidationError(gettext('This revision is not available for publication.'))
        revision.status = 'APPLYING'
        revision.save(update_fields=['status'])
        with publication_writes(problem.pk):
            data = ProblemData.objects.filter(problem=problem).first() or ProblemData(problem=problem)
            for field, value in revision.data.items():
                setattr(data, field, value)
            # Revision lifecycle owns file retention instead of django-cleanup.
            refresh(data)
            data.save()
            ids = []
            for values in revision.cases:
                fields = dict(values)
                pk = fields.pop('id')
                if pk:
                    case = ProblemTestCase.objects.get(pk=pk, dataset=problem)
                    for field, value in fields.items():
                        setattr(case, field, value)
                else:
                    case = ProblemTestCase(dataset=problem, **fields)
                case.save()
                ids.append(case.pk)
            ProblemTestCase.objects.filter(dataset=problem).exclude(pk__in=ids).delete()
            # On a DB failure after this point, recovery repeats the same journal.
            init_path = Path(problem_data_storage.path('%s/init.yml' % problem.code))
            from judge.utils.test_data_upload import check_editor
            # After a crash past the filesystem commit point, finish the journal
            # even if the editor has since lost permission. Otherwise recheck it.
            if revision.init:
                published = init_path.exists() and init_path.read_text() == revision.init
            else:
                # Absence is a commit point only when this revision removed an
                # existing init; an already-empty problem still needs permission.
                published = revision.previous_init is not None and not init_path.exists()
            if not published:
                check_editor(problem, revision.session.user)
            if revision.init:
                _atomic_write(init_path, revision.init.encode())
            else:
                _atomic_remove(init_path)
            revision.status = 'APPLIED'
            revision.applied_at = timezone.now()
            revision.error = ''
            revision.save(update_fields=['status', 'applied_at', 'error'])
            type(revision.session).objects.filter(pk=revision.session_id).update(status='APPLIED')
            if revision.upload_id:
                type(revision.upload).objects.filter(pk=revision.upload_id).update(
                    status='APPLIED', finished_at=timezone.now(), reserved_bytes=0,
                )
    return revision


def apply_revision(revision_id):
    from judge.utils.test_data_upload import UploadError

    try:
        revision = _apply_revision(revision_id)
    except UploadError as error:
        with transaction.atomic():
            revision = ProblemDataRevision.objects.get(pk=revision_id)
            Problem.objects.select_for_update().get(pk=revision.problem_id)
            revision.refresh_from_db()
            revision.status, revision.error = 'FAILED', error.message
            revision.save(update_fields=['status', 'error'])
            type(revision.session).objects.filter(pk=revision.session_id).update(status='REVOKED')
            if revision.upload_id:
                type(revision.upload).objects.filter(pk=revision.upload_id).update(
                    status='CANCELED', reserved_bytes=0, finished_at=timezone.now(),
                )
        raise ValidationError(gettext(error.message))
    # Also retry invalidation when a previous successful publication lost its response.
    problem_data_storage.invalidate_problem_metadata(revision.problem)
    return revision
