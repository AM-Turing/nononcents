"""First-run checklist on Home, mirroring docs/guide/getting-started.md.

Each step is ticked from data, never stored. The card is shown only for a profile that has not
been used yet (no income, expense, refund or transfer) and still has a step left, so an
established profile never sees it.
"""

from decimal import Decimal

from django.urls import reverse

from .models import (
    Account,
    Category,
    Expense,
    Income,
    PayPeriod,
    RecurringBill,
    Refund,
    StatementImport,
    Transfer,
)

STARTER_SPENDING = ("General", "ATM Withdrawal")
STARTER_BILL = ("Other Bills",)


def setup_steps(home) -> list[dict]:
    accounts = Account.objects.filter(household=home, archived_at=None)
    categories = Category.objects.filter(household=home, archived_at=None)
    extra_categories = (
        categories.filter(type=Category.Type.SPENDING).exclude(name__in=STARTER_SPENDING).exists()
        or categories.filter(type=Category.Type.BILL).exclude(name__in=STARTER_BILL).exists()
    )
    return [
        {
            "label": "Check your accounts (Spending, Bills and Savings are ready; add others)",
            "url": reverse("accounts"),
            "done": accounts.exists(),
        },
        {
            "label": "Enter each account's opening balance",
            "url": reverse("accounts"),
            "done": accounts.exclude(opening_balance=Decimal("0.00")).exists(),
        },
        {
            "label": "Add the pay period you are in",
            "url": reverse("pay_periods"),
            "done": PayPeriod.objects.filter(household=home).exists(),
        },
        {
            "label": "Add your recurring bills",
            "url": reverse("bill_add"),
            "done": RecurringBill.objects.filter(household=home).exists(),
        },
        {
            "label": "Add the categories you use",
            "url": reverse("categories"),
            "done": extra_categories,
        },
        {
            "label": "Check a bank statement",
            "url": reverse("statements"),
            "done": StatementImport.objects.filter(household=home).exists(),
        },
    ]


def setup_checklist(home) -> list[dict] | None:
    """The steps to show on Home, or None when the card should not appear."""
    if home is None:
        return None
    steps = setup_steps(home)
    if all(step["done"] for step in steps):
        return None
    in_use = any(
        model.objects.filter(household=home).exists()
        for model in (Expense, Income, Refund, Transfer)
    )
    return None if in_use else steps
