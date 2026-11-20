from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Sum

from budget.models import (
    Account,
    BalanceAdjustment,
    Category,
    Expense,
    Household,
    Income,
    Refund,
    SavingsGoal,
    SavingsGoalEntry,
)
from budget.services import account_balance, create_income


@pytest.fixture
def ledger(db):
    home = Household.objects.create()
    user = get_user_model().objects.create_user("owner", password="long-enough-password")
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    savings = Account.objects.create(household=home, name="Savings", type="savings")
    unexpected = Category.objects.create(household=home, name="Unexpected", type="spending")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    goal = SavingsGoal.objects.create(household=home, name="Emergency Fund", target_amount=1000)
    SavingsGoal.objects.create(
        household=home, name="Unallocated Savings", target_amount=0, is_system=True
    )
    real_income = create_income(
        household=home,
        actor=user,
        income_date=date(2026, 7, 1),
        source="Pay",
        amount="3000",
        allocations=[(spending, "2000"), (bills, "900"), (savings, "100")],
        savings_goal=goal,
    )
    placeholder_expense = Expense.objects.create(
        household=home,
        date=date(2026, 7, 7),
        account=spending,
        category=unexpected,
        amount=Decimal("250.00"),
        merchant="PlaceHolder",
    )
    real_expense = Expense.objects.create(
        household=home,
        date=date(2026, 7, 8),
        account=spending,
        category=groceries,
        amount=Decimal("40.00"),
        merchant="Market",
    )
    placeholder_income = create_income(
        household=home,
        actor=user,
        income_date=date(2026, 7, 15),
        source="Placeholder",
        amount="80",
        allocations=[(bills, "50"), (savings, "30")],
        savings_goal=goal,
    )
    return {
        "home": home,
        "accounts": [spending, bills, savings],
        "goal": goal,
        "placeholder_expense": placeholder_expense,
        "placeholder_income": placeholder_income,
        "real_expense": real_expense,
        "real_income": real_income,
        "groceries": groceries,
    }


def _balances(accounts):
    days = [date(2026, 7, d) for d in range(1, 20)] + [None]
    return {(a.pk, d): account_balance(a, d) for a in accounts for d in days}


def _goal_sum(home):
    return SavingsGoalEntry.objects.filter(goal__household=home).aggregate(v=Sum("amount"))["v"]


def _run(ledger, *extra):
    out = StringIO()
    call_command(
        "convert_placeholders",
        "--household",
        str(ledger["home"].pk),
        "--expense",
        str(ledger["placeholder_expense"].pk),
        "--income",
        str(ledger["placeholder_income"].pk),
        *extra,
        stdout=out,
    )
    return out.getvalue()


def test_dry_run_changes_nothing(ledger):
    balances = _balances(ledger["accounts"])
    output = _run(ledger)

    assert "DRY RUN" in output
    assert "PlaceHolder" in output
    assert Expense.objects.filter(pk=ledger["placeholder_expense"].pk).exists()
    assert Income.objects.filter(pk=ledger["placeholder_income"].pk).exists()
    assert not BalanceAdjustment.objects.exists()
    assert _balances(ledger["accounts"]) == balances


def test_apply_keeps_every_balance_and_goal_total(ledger):
    balances = _balances(ledger["accounts"])
    goal_total = _goal_sum(ledger["home"])

    output = _run(ledger, "--apply")

    assert "Applied." in output
    assert _balances(ledger["accounts"]) == balances
    assert _goal_sum(ledger["home"]) == goal_total
    assert not Expense.objects.filter(pk=ledger["placeholder_expense"].pk).exists()
    assert not Income.objects.filter(pk=ledger["placeholder_income"].pk).exists()
    adjustments = BalanceAdjustment.objects.order_by("date", "account__name")
    assert [(a.account.name, a.amount, a.is_increase, a.date) for a in adjustments] == [
        ("Spending", Decimal("250.00"), False, date(2026, 7, 7)),
        ("Bills", Decimal("50.00"), True, date(2026, 7, 15)),
        ("Savings", Decimal("30.00"), True, date(2026, 7, 15)),
    ]
    carried = SavingsGoalEntry.objects.get(kind=SavingsGoalEntry.Kind.RECONCILIATION)
    assert (carried.goal, carried.amount, carried.date) == (
        ledger["goal"],
        Decimal("30.00"),
        date(2026, 7, 15),
    )


def test_apply_removes_placeholders_from_spending_and_income(ledger):
    _run(ledger, "--apply")

    spent = Expense.objects.filter(
        household=ledger["home"], category__type=Category.Type.SPENDING
    ).aggregate(v=Sum("amount"))["v"]
    income = Income.objects.filter(household=ledger["home"]).aggregate(v=Sum("amount"))["v"]
    assert spent == Decimal("40.00")
    assert income == Decimal("3000.00")


def test_refuses_expense_with_refunds(ledger):
    Refund.objects.create(
        household=ledger["home"],
        date=date(2026, 7, 9),
        account=ledger["accounts"][0],
        category=ledger["groceries"],
        amount=Decimal("5.00"),
        original_expense=ledger["placeholder_expense"],
    )

    with pytest.raises(CommandError, match="has refunds"):
        _run(ledger, "--apply")
    assert Expense.objects.filter(pk=ledger["placeholder_expense"].pk).exists()
    assert not BalanceAdjustment.objects.exists()


def test_rejects_rows_from_another_household(ledger):
    other = Household.objects.create(name="Other")
    with pytest.raises(CommandError, match="Not found"):
        call_command(
            "convert_placeholders",
            "--household",
            str(other.pk),
            "--expense",
            str(ledger["placeholder_expense"].pk),
            stdout=StringIO(),
        )


def test_requires_a_row(ledger):
    with pytest.raises(CommandError, match="at least one"):
        call_command(
            "convert_placeholders", "--household", str(ledger["home"].pk), stdout=StringIO()
        )


def test_today_balance_reported_unchanged(ledger):
    output = _run(ledger)
    spending = ledger["accounts"][0]
    now = account_balance(spending, date.today() + timedelta(days=1))
    assert f"Spending: ${now} -> ${now}" in output
