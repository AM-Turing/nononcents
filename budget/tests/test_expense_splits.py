from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget.models import (
    Account,
    AuditEvent,
    Category,
    Expense,
    Household,
    HouseholdMembership,
)
from budget.services import account_balance
from budget.views import report_data


@pytest.fixture
def split_setup(client, db):
    user = get_user_model().objects.create_user("split-user", password="safe-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spending = Account.objects.create(
        household=home,
        name="Spending",
        type=Account.Type.SPENDING,
        opening_balance=Decimal("500.00"),
    )
    groceries = Category.objects.create(
        household=home, name="Groceries", type=Category.Type.SPENDING
    )
    household = Category.objects.create(
        household=home, name="Household", type=Category.Type.SPENDING
    )
    utilities = Category.objects.create(household=home, name="Utilities", type=Category.Type.BILL)
    return client, home, spending, groceries, household, utilities


def split_post_data(account, first_category, second_category, first="60.00", second="40.00"):
    return {
        "amount": "100.00",
        "date": "2026-08-10",
        "account": account.pk,
        "category": "",
        "split_transaction": "on",
        "bill": "",
        "merchant": "Superstore",
        "notes": "One receipt",
        "form_token": str(uuid4()),
        "splits-TOTAL_FORMS": "2",
        "splits-INITIAL_FORMS": "0",
        "splits-MIN_NUM_FORMS": "0",
        "splits-MAX_NUM_FORMS": "10",
        "splits-0-category": first_category.pk,
        "splits-0-amount": first,
        "splits-1-category": second_category.pk,
        "splits-1-amount": second,
    }


@pytest.mark.django_db
def test_add_expense_can_split_one_ledger_entry_across_categories(split_setup):
    client, home, spending, groceries, household, _ = split_setup

    response = client.post(reverse("expense_add"), split_post_data(spending, groceries, household))

    assert response.status_code == 302
    expense = Expense.objects.get()
    assert expense.amount == Decimal("100.00")
    assert expense.category == groceries
    assert list(expense.splits.values_list("category__name", "amount")) == [
        ("Groceries", Decimal("60.00")),
        ("Household", Decimal("40.00")),
    ]
    assert account_balance(spending) == Decimal("400.00")
    event = AuditEvent.objects.get(record_type="budget.Expense", record_id=str(expense.pk))
    assert event.changes["splits"][1] == {"category": "Household", "amount": "40.00"}

    history = client.get(reverse("transactions"))
    row = history.context["rows"][0]
    assert row["category"] == "Split — Groceries: $60.00; Household: $40.00"
    assert row["amount"] == Decimal("100.00")
    assert "Split — Groceries: $60.00; Household: $40.00" in history.content.decode()


@pytest.mark.django_db
def test_split_amounts_must_equal_total_and_use_two_categories(split_setup):
    client, _, spending, groceries, household, _ = split_setup
    data = split_post_data(spending, groceries, household, second="39.99")

    response = client.post(reverse("expense_add"), data)

    assert response.status_code == 200
    assert "Split amounts must add up exactly" in response.content.decode()
    assert not Expense.objects.exists()


@pytest.mark.django_db
def test_utilities_category_is_allowed_on_spending_account(split_setup):
    client, _, spending, _, _, utilities = split_setup

    response = client.post(
        reverse("expense_add"),
        {
            "amount": "75.00",
            "date": "2026-08-10",
            "account": spending.pk,
            "category": utilities.pk,
            "bill": "",
            "merchant": "Power company",
            "notes": "",
            "form_token": str(uuid4()),
        },
    )

    assert response.status_code == 302
    assert Expense.objects.get().category == utilities


@pytest.mark.django_db
def test_split_reports_each_category_type_without_double_counting(split_setup):
    client, home, spending, groceries, _, utilities = split_setup
    response = client.post(reverse("expense_add"), split_post_data(spending, groceries, utilities))
    assert response.status_code == 302

    report = report_data(home, date(2026, 8, 1), date(2026, 8, 31))
    assert report["spending"] == Decimal("60.00")
    assert report["bills"] == Decimal("40.00")


@pytest.mark.django_db
def test_split_history_edit_keeps_allocations_equal_to_total(split_setup):
    client, _, spending, groceries, household, _ = split_setup
    client.post(reverse("expense_add"), split_post_data(spending, groceries, household))
    expense = Expense.objects.get()
    url = reverse("transaction_edit", args=["expense", expense.pk])

    invalid = client.post(
        url,
        {
            "date": "2026-08-10",
            "description": "Superstore",
            "amount": "110.00",
            "split_transaction": "on",
            "notes": "One receipt",
            "splits-TOTAL_FORMS": "2",
            "splits-INITIAL_FORMS": "2",
            "splits-MIN_NUM_FORMS": "0",
            "splits-MAX_NUM_FORMS": "10",
            "splits-0-category": groceries.pk,
            "splits-0-amount": "60.00",
            "splits-1-category": household.pk,
            "splits-1-amount": "40.00",
        },
    )
    assert invalid.status_code == 200
    assert "Split amounts must add up exactly" in invalid.content.decode()

    valid = client.post(
        url,
        {
            "date": "2026-08-10",
            "description": "Superstore",
            "amount": "110.00",
            "split_transaction": "on",
            "notes": "One receipt",
            "splits-TOTAL_FORMS": "2",
            "splits-INITIAL_FORMS": "2",
            "splits-MIN_NUM_FORMS": "0",
            "splits-MAX_NUM_FORMS": "10",
            "splits-0-category": groceries.pk,
            "splits-0-amount": "65.00",
            "splits-1-category": household.pk,
            "splits-1-amount": "45.00",
        },
    )
    assert valid.status_code == 302
    expense.refresh_from_db()
    assert expense.amount == Decimal("110.00")
    assert sum(expense.splits.values_list("amount", flat=True), Decimal("0.00")) == Decimal(
        "110.00"
    )


@pytest.mark.django_db
def test_history_edit_can_convert_an_expense_to_a_split(split_setup):
    client, home, spending, groceries, household, _ = split_setup
    expense = Expense.objects.create(
        household=home,
        date=date(2026, 8, 10),
        amount=Decimal("100.00"),
        account=spending,
        category=groceries,
        merchant="Superstore",
    )
    url = reverse("transaction_edit", args=["expense", expense.pk])

    edit_page = client.get(url)
    assert edit_page.context["expense_split_formset"] is not None
    assert len(edit_page.context["expense_split_formset"].forms) == 2
    assert not edit_page.context["form"]["split_transaction"].value()

    response = client.post(
        url,
        {
            "date": "2026-08-10",
            "description": "Superstore",
            "amount": "100.00",
            "split_transaction": "on",
            "notes": "Split after review",
            "splits-TOTAL_FORMS": "2",
            "splits-INITIAL_FORMS": "2",
            "splits-MIN_NUM_FORMS": "0",
            "splits-MAX_NUM_FORMS": "10",
            "splits-0-category": groceries.pk,
            "splits-0-amount": "70.00",
            "splits-1-category": household.pk,
            "splits-1-amount": "30.00",
        },
    )

    assert response.status_code == 302
    expense.refresh_from_db()
    assert expense.notes == "Split after review"
    assert list(expense.splits.values_list("category", "amount")) == [
        (groceries.pk, Decimal("70.00")),
        (household.pk, Decimal("30.00")),
    ]
    event = AuditEvent.objects.filter(
        record_type="budget.Expense", record_id=str(expense.pk), action="update"
    ).latest("pk")
    assert event.changes["splits"] == [
        {"category": "Groceries", "amount": "70.00"},
        {"category": "Household", "amount": "30.00"},
    ]


@pytest.mark.django_db
def test_history_edit_can_remove_an_expense_split(split_setup):
    client, _, spending, groceries, household, _ = split_setup
    client.post(reverse("expense_add"), split_post_data(spending, groceries, household))
    expense = Expense.objects.get()

    response = client.post(
        reverse("transaction_edit", args=["expense", expense.pk]),
        {
            "date": "2026-08-10",
            "description": "Superstore",
            "amount": "100.00",
            "category": household.pk,
            "notes": "Use one category",
        },
    )

    assert response.status_code == 302
    expense.refresh_from_db()
    assert expense.category == household
    assert expense.notes == "Use one category"
    assert not expense.splits.exists()
