from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from budget.models import Household
from budget.services import end_of_month_ahead, ensure_bill_occurrences


class Command(BaseCommand):
    help = "Create missing monthly bill occurrences ahead of time. Safe to run daily."

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, default=3, help="Months ahead (default 3).")
        parser.add_argument("--household", type=int, help="Only this household id.")

    def handle(self, *args, **options):
        if options["months"] < 0:
            raise CommandError("--months must not be negative.")
        households = Household.objects.all()
        if options["household"] is not None:
            households = households.filter(pk=options["household"])
        through = end_of_month_ahead(timezone.localdate(), options["months"])
        total = 0
        for home in households:
            created = ensure_bill_occurrences(home, through=through)
            total += len(created)
            self.stdout.write(f"Household {home.pk}: created {len(created)} occurrence(s).")
        self.stdout.write(f"Total created: {total}.")
