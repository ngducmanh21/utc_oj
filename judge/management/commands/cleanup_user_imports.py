import json

from django.core.management.base import BaseCommand, CommandError

from judge.utils.user_import_maintenance import cleanup_import_batches


class Command(BaseCommand):
    help = 'Expire import secrets and remove retained reports, preserving replay tombstones. Run hourly.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        if options['limit'] < 1:
            raise CommandError('The limit must be positive.')
        try:
            result = cleanup_import_batches(limit=options['limit'], dry_run=options['dry_run'])
        except Exception:
            raise CommandError('Import cleanup is unavailable. Retry after the database is available.') from None
        self.stdout.write(json.dumps(result))
