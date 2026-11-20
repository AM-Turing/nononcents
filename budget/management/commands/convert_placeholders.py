"""Replace placeholder ledger rows with balance corrections, without changing balances.

Placeholder expenses and incomes were used to force account balances to match the
bank. They distort spending and income. Each one becomes a BalanceAdjustment on the
same account, date and amount, so every account balance on every date is unchanged,
while spending, income and category totals stop including them.

Dry run by default: the conversion runs inside a transaction that is rolled back,
so the before/after report reflects the real result. Pass --apply to commit.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from budget.models import (
    Account,
    BalanceAdjustment,
    Category,
    Expense,
    Household,
    Income,
    SavingsGoalEntry,
)
from budget.services import account_balance, audit, ensure_open, update_bill_status


class _DryRun(Exception):
    pass


def _spending_and_bills(home):
    totals = {}
    for kind in (Category.Type.SPENDING, Category.Type.BILL):
        totals[kind] = Expense.objects.filter(household=home, category__type=kind).aggregate(
            v=Sum("amount")
        )["v"] or Decimal("0.00")
    return totals


def _snapshot(home, accounts, dates):
    balances = {(a.pk, d): account_balance(a, d) for a in accounts for d in dates}
    for a in accounts:
        balances[(a.pk, None)] = account_balance(a)
    goal_sum = SavingsGoalEntry.objects.filter(goal__household=home).aggregate(v=Sum("amount"))[
        "v"
    ] or Decimal("0.00")
    income = Income.objects.filter(household=home).aggregate(v=Sum("amount"))["v"] or Decimal(
        "0.00"
    )
    return {
        "balances": balances,
        "goal_sum": goal_sum,
        "income": income,
        "expenses": _spending_and_bills(home),
    }


class Command(BaseCommand):
    help = "Convert placeholder expenses/incomes into balance corrections (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--household", type=int, required=True)
        parser.add_argument("--expense", type=int, nargs="*", default=[])
        parser.add_argument("--income", type=int, nargs="*", default=[])
        parser.add_argument(
            "--apply", action="store_true", help="Commit the conversion (default: dry run)."
        )

    def handle(self, *args, **options):
        try:
            home = Household.objects.get(pk=options["household"])
        except Household.DoesNotExist as exc:
            raise CommandError("No such household.") from exc
        expense_ids, income_ids = options["expense"], options["income"]
        if not expense_ids and not income_ids:
            raise CommandError("Name at least one --expense or --income id.")

        expenses = list(
            Expense.objects.filter(household=home, pk__in=expense_ids).select_related(
                "account", "category"
            )
        )
        incomes = list(
            Income.objects.filter(household=home, pk__in=income_ids).prefetch_related(
                "allocations__account"
            )
        )
        missing = (set(expense_ids) - {e.pk for e in expenses}) | (
            set(income_ids) - {i.pk for i in incomes}
        )
        if missing:
            raise CommandError(f"Not found in this household: {sorted(missing)}")

        accounts = list(Account.objects.filter(household=home).order_by("pk"))
        dates = sorted(
            {d for row in [*expenses, *incomes] for d in (row.date - timedelta(days=1), row.date)}
            | {timezone.localdate()}
        )
        before = _snapshot(home, accounts, dates)

        self.stdout.write(f"Household {home.pk}: {home.name}")
        self.stdout.write("Rows to convert:")
        for e in expenses:
            self.stdout.write(
                f"  Expense #{e.pk} {e.date} {e.account.name} -${e.amount} "
                f"[{e.category.name}] {e.merchant!r}"
            )
        for i in incomes:
            parts = ", ".join(f"{a.account.name} +${a.amount}" for a in i.allocations.all())
            self.stdout.write(f"  Income #{i.pk} {i.date} {i.source!r}: {parts}")

        try:
            with transaction.atomic():
                created = self._convert(home, expenses, incomes)
                after = _snapshot(home, accounts, dates)
                self._verify(before, after, accounts, dates)
                self._report(created, before, after, accounts)
                if not options["apply"]:
                    raise _DryRun
        except _DryRun:
            self.stdout.write(self.style.WARNING("DRY RUN: nothing was changed. Use --apply."))
            return
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        self.stdout.write(self.style.SUCCESS("Applied."))

    def _convert(self, home, expenses, incomes):
        created = []
        for e in expenses:
            ensure_open(home, e.date)
            if e.refunds.exists():
                raise CommandError(f"Expense #{e.pk} has refunds; convert manually.")
            created.append(
                self._replace(
                    home,
                    source=f"budget.Expense#{e.pk}",
                    label=f"expense #{e.pk} ({e.merchant or e.category.name})",
                    account=e.account,
                    entry_date=e.date,
                    amount=e.amount,
                    is_increase=False,
                    goal_entries=list(e.savings_goal_entries.all()),
                )
            )
            occurrence = e.bill_occurrence
            e.delete()
            if occurrence:
                update_bill_status(occurrence)
        for i in incomes:
            ensure_open(home, i.date)
            for allocation in i.allocations.select_related("account"):
                created.append(
                    self._replace(
                        home,
                        source=f"budget.Income#{i.pk}",
                        label=f"income #{i.pk} ({i.source})",
                        account=allocation.account,
                        entry_date=i.date,
                        amount=allocation.amount,
                        is_increase=True,
                        goal_entries=list(allocation.savings_goal_entries.all()),
                    )
                )
            i.delete()
        return created

    def _replace(
        self, home, *, source, label, account, entry_date, amount, is_increase, goal_entries
    ):
        adjustment = BalanceAdjustment.objects.create(
            household=home,
            account=account,
            date=entry_date,
            amount=amount,
            is_increase=is_increase,
            reason=f"Opening balance correction (was placeholder {label})",
            notes="Converted by convert_placeholders; balances unchanged.",
        )
        # Keep savings-goal partitions identical: the linked entries cascade away with
        # the placeholder, so re-create them unlinked on the same goal and date.
        for entry in goal_entries:
            SavingsGoalEntry.objects.create(
                goal=entry.goal,
                amount=entry.amount,
                date=entry.date,
                kind=SavingsGoalEntry.Kind.RECONCILIATION,
                note=f"Carried over from placeholder {label}",
                adjustment=adjustment,
            )
        audit(
            home,
            None,
            adjustment,
            "create",
            {
                "converted_from": source,
                "account": account.name,
                "amount": str(amount),
                "is_increase": is_increase,
                "date": entry_date.isoformat(),
            },
        )
        return adjustment

    def _verify(self, before, after, accounts, dates):
        changed = [
            key for key, value in before["balances"].items() if after["balances"][key] != value
        ]
        if changed:
            raise CommandError(f"Balances would change for {changed}; nothing applied.")
        if before["goal_sum"] != after["goal_sum"]:
            raise CommandError("Savings-goal total would change; nothing applied.")

    def _report(self, created, before, after, accounts):
        self.stdout.write("Balance corrections created:")
        for adj in created:
            sign = "+" if adj.is_increase else "-"
            self.stdout.write(f"  {adj.date} {adj.account.name} {sign}${adj.amount}: {adj.reason}")
        self.stdout.write("Account balances (before -> after), checked on every affected date:")
        for account in accounts:
            now_before = before["balances"][(account.pk, None)]
            now_after = after["balances"][(account.pk, None)]
            self.stdout.write(f"  {account.name}: ${now_before} -> ${now_after}")
        delta_spending: dict[str, Decimal] = defaultdict(Decimal)
        for kind, value in before["expenses"].items():
            delta_spending[kind] = after["expenses"][kind] - value
        self.stdout.write(
            "Totals no longer distorted: "
            f"income {before['income']} -> {after['income']}; "
            f"spending-category expenses change {delta_spending[Category.Type.SPENDING]}; "
            f"bill-category expenses change {delta_spending[Category.Type.BILL]}; "
            f"savings goals {before['goal_sum']} -> {after['goal_sum']}"
        )
