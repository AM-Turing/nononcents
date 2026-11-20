"""Mark existing paychecks and anchor pay periods to their real deposit dates.

Marks non-irregular incomes whose source matches one of ``--source`` (case-insensitive,
exact) as paychecks, then runs the same boundary sync used when a paycheck is recorded.
Only period start/end/actual-deposit dates and bill-occurrence period links change;
no money row is altered, so every account balance is unchanged (verified).

Dry run by default: everything runs inside a transaction that is rolled back, so the
report shows the real result. Pass --apply to commit.
"""

from collections import Counter
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum

from budget.models import Account, Household, Income, PayPeriod
from budget.pay_periods import sync_pay_period_boundaries
from budget.services import account_balance, audit


class _DryRun(Exception):
    pass


def _income_per_period(home):
    rows = {}
    for period in PayPeriod.objects.filter(household=home).order_by("start_date"):
        total = Income.objects.filter(
            household=home, date__range=(period.start_date, period.end_date)
        ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
        paychecks = Income.objects.filter(
            household=home,
            is_paycheck=True,
            date__range=(period.start_date, period.end_date),
        ).count()
        rows[period.pk] = (total, paychecks)
    return rows


class Command(BaseCommand):
    help = "Mark paychecks by source and anchor pay periods to deposit dates (dry run)."

    def add_arguments(self, parser):
        parser.add_argument("--household", type=int, required=True)
        parser.add_argument(
            "--source",
            action="append",
            default=[],
            help="Income source that is the regular paycheck (repeatable).",
        )
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        home = Household.objects.filter(pk=options["household"]).first()
        if home is None:
            raise CommandError("Household not found.")
        sources = {value.casefold() for value in options["source"]}
        accounts = list(Account.objects.filter(household=home))
        balances_before = {a.pk: account_balance(a) for a in accounts}
        out = self.stdout.write
        try:
            with transaction.atomic():
                income_before = _income_per_period(home)
                candidates = [
                    income
                    for income in Income.objects.filter(
                        household=home, is_paycheck=False, is_irregular=False
                    ).order_by("date")
                    if income.source.casefold() in sources
                ]
                out(f"Incomes to mark as paychecks: {len(candidates)}")
                for income in candidates:
                    out(f"  #{income.pk} {income.date} {income.source} ${income.amount:,.2f}")
                    income.is_paycheck = True
                    income.save(update_fields=["is_paycheck"])
                    audit(home, None, income, "update", {"is_paycheck": True})

                result = sync_pay_period_boundaries(home)
                out(f"Pay periods changed: {len(result.changes)}")
                for change in result.changes:
                    b, a = change.before, change.after
                    out(
                        f"  #{change.period.pk} nominal {change.period.nominal_payday}: "
                        f"{b['start_date']}..{b['end_date']} -> {a['start_date']}..{a['end_date']}"
                        f" (paycheck landed {a['actual_deposit_date'] or 'not recorded'})"
                    )
                for note in result.kept:
                    out(f"  kept: {note}")
                for income in result.unassigned:
                    out(f"  not attributed (no nominal payday nearby): #{income.pk} {income.date}")
                out(f"Bill occurrences moved to another period: {result.occurrences_moved}")

                income_after = _income_per_period(home)
                out("Income per period (before -> after, paychecks inside):")
                for period in PayPeriod.objects.filter(household=home).order_by("start_date"):
                    before, after = income_before[period.pk], income_after[period.pk]
                    if before != (after[0], 0) or after[1]:
                        out(
                            f"  {period}: ${before[0]:,.2f} -> ${after[0]:,.2f} "
                            f"({after[1]} paycheck{'s' if after[1] != 1 else ''})"
                        )
                counts = Counter(after[1] for after in income_after.values())
                if any(n > 1 for n in counts):
                    out("WARNING: a period contains more than one paycheck.")

                changed = [a for a in accounts if account_balance(a) != balances_before[a.pk]]
                if changed:
                    raise CommandError("Account balances changed; rolled back.")
                out(
                    "Account balances unchanged: "
                    + ", ".join(f"{a.name} ${balances_before[a.pk]:,.2f}" for a in accounts)
                )
                if not options["apply"]:
                    raise _DryRun
        except _DryRun:
            out("Dry run: nothing was saved. Re-run with --apply to commit.")
            return
        out("Applied.")
