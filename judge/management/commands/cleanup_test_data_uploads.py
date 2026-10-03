import json

from django.core.management.base import BaseCommand, CommandError

from judge.utils.test_data_upload import UploadError, cleanup_uploads


class Command(BaseCommand):
    help = 'Expire abandoned test-data editing sessions and remove unused staging files.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        try:
            result = cleanup_uploads(dry_run=options['dry_run'])
        except UploadError as exc:
            if exc.code == 'busy':
                self.stdout.write('Another cleanup is active; skipped.')
                return
            raise CommandError(exc.message)
        self.stdout.write(json.dumps(result))
