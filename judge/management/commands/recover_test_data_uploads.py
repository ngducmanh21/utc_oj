import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from judge.models import Problem, ProblemDataEditSession, ProblemDataRevision, problem_data_storage
from judge.utils.problem_data_publication import apply_revision
from judge.utils.test_data_upload import UploadError, staging_lock


class Command(BaseCommand):
    help = 'Finish journaled test publications and release abandoned preparation leases.'

    def remove_abandoned_files(self, session):
        root = Path(problem_data_storage.path(session.problem.code)) / '_revisions'
        if not root.is_dir():
            return
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                revision_id = uuid.UUID(hex=directory.name)
                marker = directory / '.preparation'
                if (directory.name != revision_id.hex or marker.is_symlink() or
                        marker.read_text() != str(session.pk)):
                    continue
            except (ValueError, OSError):
                continue
            if not ProblemDataRevision.objects.filter(pk=revision_id).exists():
                shutil.rmtree(directory)
                self.stdout.write('Removed abandoned revision directory %s' % revision_id)

    def handle(self, *args, **options):
        failures = 0
        for revision in ProblemDataRevision.objects.filter(status__in=('PREPARED', 'APPLYING')):
            try:
                with staging_lock('publish-%s' % revision.problem_id, blocking=False):
                    apply_revision(revision.pk)
                self.stdout.write('Recovered revision %s' % revision.pk)
            except UploadError as error:
                if error.code != 'busy':
                    raise CommandError(error.message)
            except Exception as error:
                failures += 1
                self.stderr.write('Revision %s: %s' % (revision.pk, error))

        # Preparation may die before it has a journal. No active data was changed.
        abandoned = ProblemDataEditSession.objects.filter(
            status='APPLYING', revision__isnull=True,
            last_activity__lt=timezone.now() - timedelta(minutes=15),
        )
        for session in abandoned:
            try:
                with staging_lock('publish-%s' % session.problem_id, blocking=False), transaction.atomic():
                    Problem.objects.select_for_update().get(pk=session.problem_id)
                    session.refresh_from_db()
                    if session.status != 'APPLYING' or ProblemDataRevision.objects.filter(session=session).exists():
                        continue
                    session.status = 'EXPIRED'
                    session.save(update_fields=['status'])
                    session.uploads.filter(status='APPLYING').update(
                        status='EXPIRED', finished_at=timezone.now(), reserved_bytes=0,
                    )
                    self.remove_abandoned_files(session)
                    self.stdout.write('Released abandoned preparation %s' % session.pk)
            except UploadError as error:
                if error.code != 'busy':
                    raise CommandError(error.message)
        if failures:
            raise CommandError('%s publication(s) still require recovery.' % failures)
