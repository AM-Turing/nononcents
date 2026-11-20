from django.core.management.base import BaseCommand

from budget.activity_log import prune_activity_log


class Command(BaseCommand):
    help = "Remove activity-log entries older than ACTIVITY_LOG_RETENTION_DAYS (0 keeps all)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only print the count.")

    def handle(self, *args, **options):
        count = prune_activity_log(dry_run=options["dry_run"])
        verb = "Would remove" if options["dry_run"] else "Removed"
        self.stdout.write(f"{verb} {count} activity-log entries.")
