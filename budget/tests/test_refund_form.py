from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from budget.forms import RefundForm
from budget.models import Account, Category, Expense, Household


@pytest.mark.django_db
def test_original_expense_choices_show_transaction_details_and_stay_in_household():
    home = Household.objects.create(name="Test Household")
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    subscriptions = Category.objects.create(household=home, name="Subscriptions", type="spending")
    expense = Expense.objects.create(
        household=home,
        date=date(2026, 8, 3),
        amount=Decimal("12.57"),
        account=spending,
        category=subscriptions,
        merchant="Orchard",
    )
    other_home = Household.objects.create(name="Other Budget")
    other_account = Account.objects.create(household=other_home, name="Spending", type="spending")
    other_category = Category.objects.create(household=other_home, name="General", type="spending")
    Expense.objects.create(
        household=other_home,
        date=timezone.localdate(),
        amount=Decimal("99.00"),
        account=other_account,
        category=other_category,
        merchant="Private transaction",
    )

    expense.date = timezone.localdate() - timedelta(days=3)
    expense.save()
    field = RefundForm(household=home).fields["original_expense"]

    assert list(field.queryset) == [expense]
    assert field.label_from_instance(expense) == (f"{expense.date:%Y-%m-%d} — Orchard — $12.57")
