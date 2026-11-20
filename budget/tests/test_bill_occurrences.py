from datetime import date
from decimal import Decimal

import pytest

from budget.forms import ExpenseForm
from budget.models import Account, BillOccurrence, Category, Household, RecurringBill
from budget.services import occurrence_for_expense_date


@pytest.fixture
def bill(db):
    home = Household.objects.create()
    account = Account.objects.create(household=home, name="Bills", type="bills")
    category = Category.objects.create(household=home, name="Utilities", type="bill")
    return RecurringBill.objects.create(
        household=home,
        name="Internet",
        default_amount=Decimal("100.00"),
        due_day=29,
        category=category,
        account=account,
        effective_start=date(2026, 1, 1),
    )


@pytest.mark.django_db
def test_form_lists_each_recurring_bill_once(bill):
    form = ExpenseForm()
    assert list(form.fields["bill"].queryset) == [bill]
    assert list(form.fields["bill_occurrence"].queryset) == []


@pytest.mark.django_db
def test_expense_form_lists_categories_alphabetically(bill):
    Category.objects.create(household=bill.household, name="Zebra", type=Category.Type.SPENDING)
    Category.objects.create(
        household=bill.household, name="Entertainment", type=Category.Type.SPENDING
    )

    form = ExpenseForm(household=bill.household)

    assert list(form.fields["category"].queryset.values_list("name", flat=True)) == [
        "Entertainment",
        "Utilities",
        "Zebra",
    ]


@pytest.mark.django_db
def test_subscription_can_be_paid_from_spending_account(bill):
    spending = Account.objects.create(
        household=bill.household, name="Spending", type=Account.Type.SPENDING
    )
    form = ExpenseForm(
        data={
            "amount": "25.00",
            "date": "2026-08-04",
            "account": spending.pk,
            "category": bill.category_id,
            "bill": bill.pk,
            "merchant": bill.name,
            "notes": "",
        },
        household=bill.household,
    )

    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_bill_payment_accepts_a_different_bill_category(bill):
    spending = Account.objects.create(
        household=bill.household, name="Spending", type=Account.Type.SPENDING
    )
    other = Category.objects.create(
        household=bill.household, name="Subscriptions", type=Category.Type.BILL
    )
    form = ExpenseForm(
        data={
            "amount": "25.00",
            "date": "2026-08-04",
            "account": spending.pk,
            "category": other.pk,
            "bill": bill.pk,
            "merchant": bill.name,
            "notes": "",
        },
        household=bill.household,
    )

    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_bill_payment_rejects_a_spending_category(bill):
    spending = Account.objects.create(
        household=bill.household, name="Spending", type=Account.Type.SPENDING
    )
    groceries = Category.objects.create(
        household=bill.household, name="Groceries", type=Category.Type.SPENDING
    )
    form = ExpenseForm(
        data={
            "amount": "25.00",
            "date": "2026-08-04",
            "account": spending.pk,
            "category": groceries.pk,
            "bill": bill.pk,
            "merchant": bill.name,
            "notes": "",
        },
        household=bill.household,
    )

    assert not form.is_valid()
    assert form.errors["category"] == ["Choose a Bills category for the selected bill."]


@pytest.mark.django_db
def test_expense_date_resolves_the_monthly_occurrence(bill):
    occurrence = occurrence_for_expense_date(bill, date(2026, 8, 4))
    assert occurrence.due_date == date(2026, 8, 29)
    assert occurrence.expected_amount == Decimal("100.00")


@pytest.mark.django_db
def test_existing_monthly_occurrence_is_not_overwritten(bill):
    existing = BillOccurrence.objects.create(
        bill=bill,
        due_date=date(2026, 8, 29),
        expected_amount=Decimal("125.00"),
    )
    resolved = occurrence_for_expense_date(bill, date(2026, 8, 10))
    assert resolved == existing
    assert resolved.expected_amount == Decimal("125.00")
