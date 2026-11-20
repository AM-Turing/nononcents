from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import models, transaction

from budget.models import BillOccurrence, Household
from budget.services import audit, computed_bill_status, ensure_open, update_bill_status


class Command(BaseCommand):
    help = (
        "Remove unpaid occurrences of removed bills dated after their end, and fix stale "
        "occurrence statuses. Dry run unless --apply is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Make the changes.")
        parser.add_argument("--household", type=int, help="Only this household id.")

    @staticmethod
    def _describe(item, extra):
        return (
            f"  #{item.pk} {item.bill.name} due {item.due_date.isoformat()} {extra} "
            f"target ${item.target_amount} paid ${item.amount_paid}"
        )

    @staticmethod
    def _locked(home, item):
        try:
            ensure_open(home, item.due_date)
        except ValidationError:
            return True
        return False

    def handle(self, *args, **options):
        apply = options["apply"]
        households = Household.objects.all()
        if options["household"] is not None:
            households = households.filter(pk=options["household"])
        deleted = fixed = skipped = 0
        with transaction.atomic():
            for home in households:
                self.stdout.write(f"Household {home.pk}")
                ghosts = (
                    BillOccurrence.objects.filter(
                        bill__household=home,
                        bill__active=False,
                        bill__effective_end__isnull=False,
                        due_date__gt=models.F("bill__effective_end"),
                        expenses__isnull=True,
                    )
                    .select_related("bill")
                    .order_by("due_date", "pk")
                )
                ghost_ids = set()
                for item in ghosts:
                    ghost_ids.add(item.pk)
                    if self._locked(home, item):
                        skipped += 1
                        self.stdout.write(self._describe(item, "SKIPPED (locked period)"))
                        continue
                    deleted += 1
                    self.stdout.write(self._describe(item, "DELETE (removed bill, no payments)"))
                    if apply:
                        audit(
                            home,
                            None,
                            item,
                            "delete",
                            {"due_date": item.due_date.isoformat(), "reason": "cleanup"},
                        )
                        item.delete()
                stale = (
                    BillOccurrence.objects.filter(bill__household=home)
                    .exclude(pk__in=ghost_ids)
                    .select_related("bill")
                    .order_by("due_date", "pk")
                )
                for item in stale:
                    after = computed_bill_status(item)
                    if after == item.status:
                        continue
                    label = f"STATUS {item.status} -> {after}"
                    if self._locked(home, item):
                        skipped += 1
                        self.stdout.write(self._describe(item, f"SKIPPED (locked period) {label}"))
                        continue
                    fixed += 1
                    self.stdout.write(self._describe(item, label))
                    if apply:
                        before = item.status
                        update_bill_status(item)
                        audit(
                            home,
                            None,
                            item,
                            "update",
                            {"status": item.status, "previous_status": before, "reason": "cleanup"},
                        )
        mode = "Applied" if apply else "Dry run (no changes made)"
        self.stdout.write(
            f"{mode}: {deleted} to delete, {fixed} status fixes, "
            f"{skipped} skipped in locked periods."
        )
