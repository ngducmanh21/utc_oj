import json

from django.core.management.base import BaseCommand, CommandError

from judge.utils.user_import_maintenance import recover_import_batches


class Command(BaseCommand):
    help = 'Recover durable user import jobs and independent cache repairs. Run every minute.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--batch', type=str)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        if options['limit'] < 1:
            raise CommandError('The limit must be positive.')
        try:
            result = recover_import_batches(limit=options['limit'], batch_id=options['batch'],
                                            dry_run=options['dry_run'])
        except Exception:
            raise CommandError('Import recovery is unavailable. No new import was created.') from None
        self.stdout.write(json.dumps(result))
