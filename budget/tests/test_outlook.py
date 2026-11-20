from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from budget.models import (
    Account,
    BillOccurrence,
    Category,
    Expense,
    Household,
    Income,
    IncomeAllocation,
    PayPeriod,
    PayPeriodBudget,
    RecurringBill,
)
from budget.outlook import default_outlook_range, outlook_data
from budget.services import create_transfer, ensure_savings_goals


@pytest.mark.django_db
def test_default_outlook_uses_completed_period_and_same_paycheck_next_month():
    home = Household.objects.create()
    first = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 15),
        expected_deposit_date=date(2026, 8, 13),
        start_date=date(2026, 8, 13),
        end_date=date(2026, 8, 30),
    )
    second = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 9, 1),
        expected_deposit_date=date(2026, 8, 31),
        start_date=date(2026, 8, 31),
        end_date=date(2026, 9, 13),
    )
    third = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 9, 15),
        expected_deposit_date=date(2026, 9, 14),
        start_date=date(2026, 9, 14),
        end_date=date(2026, 9, 29),
    )
    for period in (first, second, third):
        PayPeriodBudget.objects.create(pay_period=period)

    assert default_outlook_range(home, date(2026, 9, 5)) == (
        date(2026, 8, 13),
        date(2026, 9, 13),
    )


@pytest.mark.django_db
def test_outlook_excludes_fulfilled_goal_purchase_from_adjusted_savings_decline():
    home = Household.objects.create()
    user = get_user_model().objects.create_user("outlook-user")
    savings = Account.objects.create(
        household=home,
        name="Savings",
        type=Account.Type.SAVINGS,
        opening_balance=Decimal("1000.00"),
    )
    spending = Account.objects.create(household=home, name="Spending", type=Account.Type.SPENDING)
    category = Category.objects.create(
        household=home,
        name="Dining",
        type=Category.Type.SPENDING,
        is_essential=False,
    )
    goal, _ = ensure_savings_goals(home)
    create_transfer(
        household=home,
        actor=user,
        entry_date=date(2026, 9, 3),
        source=savings,
        destination=spending,
        amount="1000.00",
        savings_source_allocations=[(goal, "1000.00", True)],
    )
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 9, 4),
        amount=Decimal("100.00"),
        account=spending,
        category=category,
        merchant="Cafe",
    )

    data = outlook_data(home, date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 5))

    assert data["savings_change"] == Decimal("-1000.00")
    assert data["fulfilled_savings"] == Decimal("1000.00")
    assert data["savings_health"] == "held steady"
    assert data["monthly_discretionary"] == Decimal("101.47")
    assert data["cut_scenarios"][0]["potential"] == Decimal("10.15")
    assert data["category_cut_rows"][0]["name"] == "Dining"
    assert data["category_cut_rows"][0]["scenarios"][0] == {
        "name": "Limited",
        "rate": Decimal("10"),
        "amount": Decimal("10.15"),
    }
    assert next(point for point in data["daily_chart_points"] if point["date"] == date(2026, 9, 4))[
        "amount"
    ] == Decimal("100.00")


@pytest.mark.django_db
def test_outlook_never_counts_bills_due_before_the_range_or_current_period():
    home = Household.objects.create()
    bills_account = Account.objects.create(household=home, name="Bills", type=Account.Type.BILLS)
    category = Category.objects.create(household=home, name="Utilities", type=Category.Type.BILL)
    bill = RecurringBill.objects.create(
        household=home,
        name="Electric",
        default_amount=Decimal("100.00"),
        due_day=10,
        category=category,
        account=bills_account,
        effective_start=date(2026, 1, 1),
    )
    for due, amount in ((date(2026, 8, 10), "50.00"), (date(2026, 9, 10), "100.00")):
        BillOccurrence.objects.create(
            bill=bill,
            category=category,
            account=bills_account,
            due_date=due,
            expected_amount=Decimal(amount),
        )
    Expense.objects.create(
        household=home,
        created_by=get_user_model().objects.create_user("prior-bill-user"),
        date=date(2026, 8, 15),
        amount=Decimal("75.00"),
        account=bills_account,
        category=category,
        merchant="Electric company",
    )

    data = outlook_data(home, date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 5))

    assert "overdue_bills" not in data
    assert data["recurring_total"] == Decimal("100.00")
    assert data["projected_bills_remaining"] == Decimal("100.00")
    assert [row["due_date"] for row in data["bill_rows"]] == [date(2026, 9, 10)]
    assert data["prior_bills"] == Decimal("75.00")


@pytest.mark.django_db
def test_outlook_projection_uses_balances_once_counted_paychecks_and_recent_pace():
    home = Household.objects.create()
    user = get_user_model().objects.create_user("projection-user")
    spending = Account.objects.create(
        household=home, name="Spending", type=Account.Type.SPENDING, opening_balance="1000.00"
    )
    category = Category.objects.create(household=home, name="Food", type=Category.Type.SPENDING)
    # Completed period of 10 days with $100 net spending -> $10/day pace.
    done = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 9, 1),
        expected_deposit_date=date(2026, 9, 1),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 10),
    )
    current = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 9, 11),
        expected_deposit_date=date(2026, 9, 11),
        start_date=date(2026, 9, 11),
        end_date=date(2026, 9, 20),
    )
    upcoming = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 9, 21),
        expected_deposit_date=date(2026, 9, 21),
        start_date=date(2026, 9, 21),
        end_date=date(2026, 9, 30),
    )
    PayPeriodBudget.objects.create(pay_period=done)
    PayPeriodBudget.objects.create(pay_period=current)
    PayPeriodBudget.objects.create(pay_period=upcoming, expected_paycheck=Decimal("500.00"))
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 9, 5),
        amount=Decimal("100.00"),
        account=spending,
        category=category,
    )
    income = Income.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 9, 11),
        amount=Decimal("400.00"),
        source="Pay",
        is_paycheck=True,
    )
    IncomeAllocation.objects.create(income=income, account=spending, amount=Decimal("400.00"))

    data = outlook_data(home, date(2026, 9, 11), date(2026, 9, 30), date(2026, 9, 15))

    # The 9-11 paycheck is received once; only the 9-21 paycheck is expected.
    assert data["actual_income"] == Decimal("400.00")
    assert data["scheduled_income_remaining"] == Decimal("500.00")
    assert [row["timing"] for row in data["selected_pay_schedule"]] == ["elapsed", "future"]
    assert data["projected_income"] == Decimal("900.00")
    # 15 remaining days at $10/day.
    assert data["projected_spending_remaining"] == Decimal("150.00")
    # Balance at 9-15 is 1300; + 500 paycheck - 150 spending.
    assert data["projected_end_balance"] == Decimal("1650.00")
    assert data["opening_balance"] == Decimal("900.00")
    assert data["forecast_surplus"] == Decimal("750.00")
    assert data["as_of"] == date(2026, 9, 15)


@pytest.mark.django_db
def test_outlook_groups_merchant_names_case_insensitively():
    home = Household.objects.create()
    user = get_user_model().objects.create_user("merchant-user")
    spending = Account.objects.create(household=home, name="Spending", type=Account.Type.SPENDING)
    category = Category.objects.create(
        household=home, name="Household", type=Category.Type.SPENDING
    )
    for index, merchant in enumerate(("Hypermart", "HYPERMART", "hypermart"), 1):
        Expense.objects.create(
            household=home,
            created_by=user,
            date=date(2026, 9, index),
            amount=Decimal("10.00"),
            account=spending,
            category=category,
            merchant=merchant,
        )

    data = outlook_data(home, date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 5))

    assert data["category_rows"][0]["merchants"] == [
        {"name": "Hypermart", "amount": Decimal("30.00")}
    ]
