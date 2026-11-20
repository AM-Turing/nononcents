from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from budget.models import Account, Category, Expense, Household, Refund
from budget.services import (
    account_balance,
    create_income,
    create_transfer,
    expected_deposit_date,
    prorated_target,
)


@pytest.fixture
def data(db):
    home = Household.objects.create()
    user = get_user_model().objects.create_user("tester", password="long-enough-password")
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    savings = Account.objects.create(household=home, name="Savings", type="savings")
    category = Category.objects.create(household=home, name="Groceries", type="spending")
    return home, user, spending, savings, category


@pytest.mark.django_db
def test_acceptance_ledger(data):
    home, user, spending, savings, category = data
    create_income(
        household=home,
        actor=user,
        income_date=date(2026, 8, 1),
        source="Pay",
        amount="1400",
        allocations=[(spending, "1400")],
    )
    expense = Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 1),
        amount="100",
        account=spending,
        category=category,
    )
    assert account_balance(spending) == Decimal("1300.00")
    create_transfer(
        household=home,
        actor=user,
        entry_date=date(2026, 8, 1),
        source=spending,
        destination=savings,
        amount="50",
    )
    assert account_balance(spending) == Decimal("1250.00")
    assert account_balance(savings) == Decimal("50.00")
    Refund.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 1),
        amount="20",
        account=spending,
        category=category,
        original_expense=expense,
    )
    assert account_balance(spending) == Decimal("1270.00")


@pytest.mark.django_db
def test_income_must_allocate_exactly(data):
    home, user, spending, savings, _ = data
    with pytest.raises(ValidationError):
        create_income(
            household=home,
            actor=user,
            income_date=date(2026, 8, 1),
            source="Pay",
            amount="2000",
            allocations=[(spending, "1400"), (savings, "100")],
        )


@pytest.mark.parametrize(
    ("nominal", "expected"),
    [
        (date(2026, 8, 3), date(2026, 8, 2)),
        (date(2026, 8, 4), date(2026, 8, 3)),
        (date(2026, 8, 5), date(2026, 8, 4)),
        (date(2026, 8, 6), date(2026, 8, 5)),
        (date(2026, 8, 7), date(2026, 8, 6)),
        (date(2026, 8, 8), date(2026, 8, 6)),
        (date(2026, 8, 9), date(2026, 8, 6)),
    ],
)
def test_payday_every_weekday(nominal, expected):
    assert expected_deposit_date(nominal) == expected


def test_prorating_rounds_to_cents():
    assert prorated_target(Decimal("700"), date(2026, 1, 1), date(2026, 1, 15)) == Decimal(
        "1500.00"
    )
