"""Account ledger walking, cleared state, and bank reconciliation.

Balances here are always derived from the ledger. ``Reconciliation`` rows are history only.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    Account,
    BalanceAdjustment,
    Expense,
    IncomeAllocation,
    Reconciliation,
    Refund,
    SavingsGoalEntry,
    Transfer,
)
from .money import MoneyField
from .services import account_balance, audit, ensure_open, ensure_savings_goals, money

# Deterministic tie-break between kinds that share a date and timestamp.
KIND_ORDER = {"income": 0, "transfer": 1, "refund": 2, "expense": 3, "adjustment": 4}


@dataclass
class LedgerItem:
    """One movement on one account. ``base`` is the transaction kind; ``role`` the clear side."""

    base: str
    role: str
    pk: int
    row_pk: int
    date: date
    created_at: datetime
    amount: Decimal  # signed: + inflow, - outflow
    cleared_at: datetime | None
    description: str
    balance_after: Decimal = field(default=Decimal("0.00"))

    @property
    def key(self) -> str:
        return f"{self.role}:{self.row_pk}"

    @property
    def sort_key(self):
        return row_sort_key(self.base, self.date, self.created_at, self.pk)

    @property
    def label(self) -> str:
        return {
            "expense": "Expense",
            "refund": "Refund",
            "income": "Income",
            "transfer": "Transfer",
            "adjustment": "Balance correction",
        }[self.base]


def row_sort_key(base, entry_date, created_at, pk):
    return (entry_date, created_at, KIND_ORDER[base], pk)


def account_ledger(account: Account, through: date | None = None) -> list[LedgerItem]:
    """Every movement on the account, oldest first, with running balance after each."""
    limit = {"date__lte": through} if through else {}
    items: list[LedgerItem] = []
    for x in Expense.objects.filter(account=account, **limit):
        items.append(
            LedgerItem(
                "expense",
                "expense",
                x.pk,
                x.pk,
                x.date,
                x.created_at,
                -x.amount,
                x.cleared_at,
                x.merchant or x.notes or "Expense",
            )
        )
    for r in Refund.objects.filter(account=account, **limit):
        items.append(
            LedgerItem(
                "refund",
                "refund",
                r.pk,
                r.pk,
                r.date,
                r.created_at,
                r.amount,
                r.cleared_at,
                r.merchant or r.notes or "Refund",
            )
        )
    for a in IncomeAllocation.objects.filter(
        account=account, **({"income__date__lte": through} if through else {})
    ).select_related("income"):
        items.append(
            LedgerItem(
                "income",
                "allocation",
                a.income_id,
                a.pk,
                a.income.date,
                a.income.created_at,
                a.amount,
                a.cleared_at,
                a.income.source,
            )
        )
    for t in Transfer.objects.filter(source=account, **limit).select_related("destination"):
        items.append(
            LedgerItem(
                "transfer",
                "transfer_out",
                t.pk,
                t.pk,
                t.date,
                t.created_at,
                -t.amount,
                t.source_cleared_at,
                t.notes or f"Transfer to {t.destination.name}",
            )
        )
    for t in Transfer.objects.filter(destination=account, **limit).select_related("source"):
        items.append(
            LedgerItem(
                "transfer",
                "transfer_in",
                t.pk,
                t.pk,
                t.date,
                t.created_at,
                t.amount,
                t.destination_cleared_at,
                t.notes or f"Transfer from {t.source.name}",
            )
        )
    for j in BalanceAdjustment.objects.filter(account=account, **limit):
        items.append(
            LedgerItem(
                "adjustment",
                "adjustment",
                j.pk,
                j.pk,
                j.date,
                j.created_at,
                j.amount if j.is_increase else -j.amount,
                j.cleared_at,
                f"Balance correction: {j.reason}",
            )
        )
    items.sort(key=lambda i: i.sort_key)
    running = account.opening_balance
    for item in items:
        running += item.amount
        item.balance_after = money(running)
    return items


def _set_cleared(item: LedgerItem, when):
    if item.role == "expense":
        Expense.objects.filter(pk=item.row_pk).update(cleared_at=when)
    elif item.role == "refund":
        Refund.objects.filter(pk=item.row_pk).update(cleared_at=when)
    elif item.role == "allocation":
        IncomeAllocation.objects.filter(pk=item.row_pk).update(cleared_at=when)
    elif item.role == "transfer_out":
        Transfer.objects.filter(pk=item.row_pk).update(source_cleared_at=when)
    elif item.role == "transfer_in":
        Transfer.objects.filter(pk=item.row_pk).update(destination_cleared_at=when)
    else:
        BalanceAdjustment.objects.filter(pk=item.row_pk).update(cleared_at=when)


def uncleared_items(account: Account, through: date | None = None) -> list[LedgerItem]:
    return [i for i in account_ledger(account, through) if i.cleared_at is None]


def uncleared_summary(account: Account) -> tuple[int, Decimal]:
    items = uncleared_items(account)
    return len(items), money(sum((i.amount for i in items), Decimal("0")))


def reconcile_account(
    *,
    household,
    actor,
    account_id,
    statement_date: date,
    statement_balance: Decimal,
    selected_keys,
    post_correction: bool,
    reason: str = "",
    notes: str = "",
) -> Reconciliation:
    """Reconcile an account. Everything is recomputed server-side inside one transaction."""
    statement_balance = money(statement_balance)
    reason = reason.strip()
    with transaction.atomic():
        account = Account.objects.select_for_update().get(
            pk=account_id, household=household, archived_at=None
        )
        ensure_open(household, statement_date)
        ledger_balance = account_balance(account, through=statement_date)
        difference = money(statement_balance - ledger_balance)
        available = {i.key: i for i in uncleared_items(account, statement_date)}
        chosen = []
        for key in dict.fromkeys(selected_keys):
            if key not in available:
                raise ValidationError(
                    "One of the selected items cannot be cleared. Compare again and retry."
                )
            chosen.append(available[key])
        adjustment = None
        now = timezone.now()
        if difference != 0:
            if not post_correction:
                raise ValidationError(
                    f"The bank balance differs from the app’s balance by ${abs(difference):,.2f}. "
                    "Find the missing or incorrect transaction and add or fix it, or tick the "
                    "option to post a balance correction with a reason."
                )
            if not reason:
                raise ValidationError("Enter a short reason for the balance correction.")
            if len(reason) > 255:
                raise ValidationError("Keep the correction reason to 255 characters or fewer.")
            # Resolve the savings system goal before the correction exists, so a first-time
            # goal bootstrap cannot count the correction twice.
            unallocated = (
                ensure_savings_goals(household)[1] if account.type == Account.Type.SAVINGS else None
            )
            adjustment = BalanceAdjustment.objects.create(
                household=household,
                account=account,
                date=statement_date,
                amount=abs(difference),
                is_increase=difference > 0,
                reason=reason,
                created_by=actor,
                cleared_at=now,
            )
            if unallocated is not None:
                # Keep goal partitions equal to the Savings ledger: the correction is
                # unexplained money, so it belongs to Unallocated Savings.
                SavingsGoalEntry.objects.create(
                    goal=unallocated,
                    amount=difference,
                    date=statement_date,
                    kind=SavingsGoalEntry.Kind.RECONCILIATION,
                    note="Bank reconciliation correction",
                    created_by=actor,
                    adjustment=adjustment,
                )
        for item in chosen:
            _set_cleared(item, now)
        reconciliation = Reconciliation.objects.create(
            household=household,
            account=account,
            statement_date=statement_date,
            statement_balance=statement_balance,
            ledger_balance=ledger_balance,
            adjustment=adjustment,
            notes=notes,
            created_by=actor,
        )
        audit(
            household,
            actor,
            reconciliation,
            "create",
            {
                "account": account.name,
                "statement_date": statement_date.isoformat(),
                "statement_balance": str(statement_balance),
                "ledger_balance": str(ledger_balance),
                "difference": str(difference),
                "cleared_count": len(chosen),
                "correction_posted": adjustment is not None,
            },
        )
    return reconciliation


class StatementForm(forms.Form):
    statement_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    statement_balance = MoneyField()

    def clean_statement_date(self):
        value = self.cleaned_data["statement_date"]
        if value > timezone.localdate():
            raise forms.ValidationError("The statement date cannot be in the future.")
        return value


class ReconcileForm(StatementForm):
    post_correction = forms.BooleanField(required=False)
    reason = forms.CharField(max_length=255, required=False)
