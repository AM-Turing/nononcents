from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget.models import (
    Account,
    AuditEvent,
    BillOccurrence,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    Income,
    IncomeAllocation,
    PayPeriod,
    RecurringBill,
    Refund,
    Transfer,
)
from budget.services import account_balance


@pytest.fixture
def search_setup(client, db):
    user = get_user_model().objects.create_user("search-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    account = Account.objects.create(household=home, name="Spending", type="spending")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 1),
        amount="1234.45",
        account=account,
        category=groceries,
        merchant="Corner MARKET",
        notes="Weekly food run",
    )
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 2),
        amount="50.00",
        account=account,
        category=groceries,
        merchant="Fuel Stop",
    )
    return client


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("keyword", "expected_count"),
    [("market", 1), ("MARKET", 1), ("weekly FOOD", 1), ("groceries", 2)],
)
def test_transaction_keyword_search_is_case_insensitive(search_setup, keyword, expected_count):
    response = search_setup.get(reverse("transactions"), {"q": keyword})
    rows = response.context["rows"]
    assert len(rows) == expected_count
    assert any(row["amount"] == Decimal("1234.45") for row in rows)


@pytest.mark.django_db
@pytest.mark.parametrize("amount", ["1234.45", "$1234.45", "1,234.45"])
def test_transaction_search_recognizes_dollar_values(search_setup, amount):
    response = search_setup.get(reverse("transactions"), {"q": amount})
    rows = response.context["rows"]
    assert len(rows) == 1
    assert rows[0]["description"] == "Corner MARKET"


@pytest.mark.django_db
def test_transaction_history_displays_account_and_notes(search_setup):
    response = search_setup.get(reverse("transactions"), {"q": "Corner MARKET"})

    row = response.context["rows"][0]
    assert row["account"] == "Spending"
    assert row["notes"] == "Weekly food run"
    content = response.content.decode()
    assert "<th>Account</th>" in content
    # Notes sit under the description in the table, and have their own line on cards.
    assert '<small class="row-notes">Weekly food run</small>' in content
    assert "<dt>Notes</dt><dd>Weekly food run</dd>" in content


@pytest.mark.django_db
def test_history_describes_income_allocations_and_transfer_accounts(search_setup):
    expense = Expense.objects.get(merchant="Corner MARKET")
    bills = Account.objects.create(
        household=expense.household, name="Bills", type=Account.Type.BILLS
    )
    income = Income.objects.create(
        household=expense.household,
        created_by=expense.created_by,
        date=date(2026, 8, 3),
        amount=Decimal("150.00"),
        source="Paycheck",
        notes="Split deposit",
    )
    IncomeAllocation.objects.create(income=income, account=expense.account, amount=Decimal("50.00"))
    IncomeAllocation.objects.create(income=income, account=bills, amount=Decimal("100.00"))
    Transfer.objects.create(
        household=expense.household,
        created_by=expense.created_by,
        date=date(2026, 8, 4),
        amount=Decimal("25.00"),
        source=expense.account,
        destination=bills,
        notes="Fund bills",
    )

    response = search_setup.get(reverse("transactions"))
    rows = response.context["rows"]
    income_row = next(row for row in rows if row["type"] == "Income")
    transfer_row = next(row for row in rows if row["type"] == "Transfer")
    assert income_row["account"] == "Bills: $100.00; Spending: $50.00"
    assert income_row["notes"] == "Split deposit"
    assert transfer_row["account"] == "Spending → Bills"
    assert transfer_row["notes"] == "Fund bills"

    account_search = search_setup.get(reverse("transactions"), {"q": "Bills"})
    assert any(row["type"] == "Income" for row in account_search.context["rows"])


@pytest.mark.django_db
def test_appearance_picker_uses_accessible_icons(search_setup):
    response = search_setup.get(reverse("dashboard"))
    content = response.content.decode()
    assert 'aria-label="Use light appearance"' in content
    assert 'aria-label="Use dark appearance"' in content
    assert 'aria-label="Use system appearance"' in content
    assert "☀" in content and "☾" in content and "🖥" in content


@pytest.mark.django_db
def test_phone_more_menu_offers_display_mode(search_setup):
    content = search_setup.get(reverse("bills")).content.decode()
    more_menu = content[content.index('id="more-menu-panel"') :]
    more_menu = more_menu[: more_menu.index("</details>")]
    assert 'class="appearance-picker more-appearance"' in more_menu
    assert f'action="{reverse("appearance")}"' in more_menu
    assert 'name="next" value="/bills/"' in more_menu
    assert 'aria-label="Use dark appearance"' in more_menu


@pytest.mark.django_db
def test_appearance_returns_to_the_same_page(search_setup):
    response = search_setup.post(reverse("appearance"), {"appearance": "dark", "next": "/bills/"})
    assert response.status_code == 302
    assert response.url == "/bills/"


@pytest.mark.django_db
def test_appearance_refuses_offsite_next(search_setup):
    response = search_setup.post(
        reverse("appearance"), {"appearance": "dark", "next": "https://example.com/"}
    )
    assert response.status_code == 302
    assert response.url == reverse("dashboard")


@pytest.mark.django_db
def test_add_transaction_chooser_links_every_ledger_entry_type(search_setup):
    response = search_setup.get(reverse("transaction_add"))

    assert response.status_code == 200
    content = response.content.decode()
    for view_name in ("expense_add", "income_add", "refund_add", "transfer_add"):
        assert f'href="{reverse(view_name)}"' in content


@pytest.mark.django_db
def test_add_transaction_buttons_open_the_expense_form_directly(search_setup):
    for name in ("dashboard", "transactions"):
        content = search_setup.get(reverse(name)).content.decode()
        assert f'href="{reverse("expense_add")}">＋ Add transaction' in content
        assert f'href="{reverse("transaction_add")}"' not in content


@pytest.mark.django_db
def test_saved_expense_goes_home_or_back_to_the_form(search_setup):
    account = Account.objects.get(name="Spending")
    category = Category.objects.get(name="Groceries")
    data = {
        "amount": "12.57",
        "date": "2026-08-03",
        "account": account.pk,
        "category": category.pk,
        "merchant": "Orchard",
        "notes": "",
        "bill": "",
    }

    response = search_setup.post(reverse("expense_add"), data | {"form_token": str(uuid4())})
    assert response.status_code == 302
    assert response.url == reverse("dashboard")

    another = search_setup.post(
        reverse("expense_add"), data | {"form_token": str(uuid4()), "add_another": "1"}
    )
    assert another.url == reverse("expense_add")
    page = search_setup.get(another.url).content.decode()
    assert "Save and add another" in page


@pytest.mark.django_db
def test_history_edit_updates_expense_fields_without_changing_type(search_setup):
    expense = Expense.objects.get(merchant="Corner MARKET")
    dining = Category.objects.create(
        household=expense.household, name="Dining", type=Category.Type.SPENDING
    )
    edit_page = search_setup.get(reverse("transaction_edit", args=["expense", expense.pk]))
    assert edit_page.context["form"]["category"].value() == expense.category_id
    assert edit_page.context["form"]["notes"].value() == "Weekly food run"

    response = search_setup.post(
        reverse("transaction_edit", args=["expense", expense.pk]),
        {
            "transaction_type": "Income",
            "date": "2026-08-03",
            "description": "Corrected market",
            "amount": "1200.25",
            "category": dining.pk,
            "notes": "Check loyalty credit",
        },
    )

    assert response.status_code == 302
    expense.refresh_from_db()
    assert expense.date == date(2026, 8, 3)
    assert expense.merchant == "Corrected market"
    assert expense.amount == Decimal("1200.25")
    assert expense.category == dining
    assert expense.notes == "Check loyalty credit"
    assert not Income.objects.filter(pk=expense.pk, source="Corrected market").exists()
    event = AuditEvent.objects.get(
        record_type="budget.Expense", record_id=str(expense.pk), action="update"
    )
    assert event.changes["category"] == "Dining"


@pytest.mark.django_db
def test_history_edit_rejects_a_locked_destination_period(search_setup):
    expense = Expense.objects.get(merchant="Corner MARKET")
    PayPeriod.objects.create(
        household=expense.household,
        nominal_payday=date(2026, 8, 15),
        expected_deposit_date=date(2026, 8, 14),
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 20),
        is_locked=True,
    )

    response = search_setup.post(
        reverse("transaction_edit", args=["expense", expense.pk]),
        {
            "date": "2026-08-12",
            "description": "Must not save",
            "amount": "10.00",
            "category": expense.category_id,
            "notes": expense.notes,
        },
    )

    assert response.status_code == 200
    assert "This pay period is locked" in response.content.decode()
    expense.refresh_from_db()
    assert expense.merchant == "Corner MARKET"


@pytest.mark.django_db
def test_history_edit_income_requires_allocations_to_match(search_setup):
    expense = Expense.objects.get(merchant="Corner MARKET")
    account = expense.account
    income = Income.objects.create(
        household=expense.household,
        created_by=expense.created_by,
        date=date(2026, 8, 1),
        amount=Decimal("100.00"),
        source="Paycheck",
    )
    IncomeAllocation.objects.create(income=income, account=account, amount=Decimal("100.00"))
    url = reverse("transaction_edit", args=["income", income.pk])

    invalid = search_setup.post(
        url,
        {
            "date": "2026-08-02",
            "description": "Corrected paycheck",
            "amount": "125.00",
            f"allocation_{account.pk}": "100.00",
        },
    )
    assert invalid.status_code == 200
    assert "allocations must add up exactly" in invalid.content.decode()

    response = search_setup.post(
        url,
        {
            "date": "2026-08-02",
            "description": "Corrected paycheck",
            "amount": "125.00",
            "notes": "Verify bonus",
            f"allocation_{account.pk}": "125.00",
        },
    )
    assert response.status_code == 302
    income.refresh_from_db()
    assert income.source == "Corrected paycheck"
    assert income.amount == Decimal("125.00")
    assert income.notes == "Verify bonus"
    assert income.allocations.get().amount == Decimal("125.00")


@pytest.mark.django_db
def test_history_edit_updates_refund_category_and_notes(search_setup):
    expense = Expense.objects.get(merchant="Corner MARKET")
    dining = Category.objects.create(
        household=expense.household, name="Dining", type=Category.Type.SPENDING
    )
    refund = Refund.objects.create(
        household=expense.household,
        created_by=expense.created_by,
        date=date(2026, 8, 5),
        amount=Decimal("12.00"),
        account=expense.account,
        category=expense.category,
        merchant="Corner MARKET",
        notes="Partial credit",
    )

    response = search_setup.post(
        reverse("transaction_edit", args=["refund", refund.pk]),
        {
            "date": "2026-08-05",
            "description": "Corner Market",
            "amount": "12.00",
            "category": dining.pk,
            "notes": "Credit completed",
        },
    )

    assert response.status_code == 302
    refund.refresh_from_db()
    assert refund.category == dining
    assert refund.notes == "Credit completed"


@pytest.mark.django_db
def test_history_edit_labels_transfer_description_as_notes(search_setup):
    expense = Expense.objects.get(merchant="Corner MARKET")
    bills = Account.objects.create(
        household=expense.household, name="Bills", type=Account.Type.BILLS
    )
    transfer = Transfer.objects.create(
        household=expense.household,
        created_by=expense.created_by,
        date=date(2026, 8, 5),
        amount=Decimal("25.00"),
        source=expense.account,
        destination=bills,
        notes="Fund bills",
    )

    response = search_setup.get(reverse("transaction_edit", args=["transfer", transfer.pk]))

    form = response.context["form"]
    assert form.fields["description"].label == "Notes"
    assert "notes" not in form.fields


@pytest.mark.django_db
def test_history_lists_edit_and_delete_actions(search_setup):
    response = search_setup.get(reverse("transactions"))

    for row in response.context["rows"]:
        assert row["edit_url"]
        assert row["delete_url"]
    content = response.content.decode()
    assert content.count(">Edit<") == 4
    assert content.count(">Delete<") == 4


@pytest.mark.django_db
def test_delete_expense_updates_bill_refund_balance_and_audit(search_setup):
    expense = Expense.objects.get(merchant="Corner MARKET")
    expense_id = expense.pk
    bill_category = Category.objects.create(
        household=expense.household, name="Mortgage", type=Category.Type.BILL
    )
    bill = RecurringBill.objects.create(
        household=expense.household,
        name="Mortgage",
        default_amount=expense.amount,
        due_day=1,
        category=bill_category,
        account=expense.account,
        effective_start=date(2026, 8, 1),
    )
    occurrence = BillOccurrence.objects.create(
        bill=bill,
        due_date=date(2026, 8, 1),
        expected_amount=expense.amount,
        status=BillOccurrence.Status.PAID,
    )
    expense.bill_occurrence = occurrence
    expense.save(update_fields=["bill_occurrence"])
    refund = Refund.objects.create(
        household=expense.household,
        created_by=expense.created_by,
        date=date(2026, 8, 2),
        amount=Decimal("10.00"),
        account=expense.account,
        category=expense.category,
        merchant="Corner MARKET",
        original_expense=expense,
    )

    response = search_setup.post(
        reverse("transaction_delete", args=["expense", expense.pk]),
        {"confirmation": "Expense: Corner MARKET"},
    )

    assert response.status_code == 302
    assert not Expense.objects.filter(pk=expense_id).exists()
    refund.refresh_from_db()
    occurrence.refresh_from_db()
    assert refund.original_expense is None
    assert occurrence.status == BillOccurrence.Status.UNCONFIRMED
    assert account_balance(expense.account) == Decimal("-40.00")
    event = AuditEvent.objects.get(
        record_type="budget.Expense", record_id=str(expense_id), action="delete"
    )
    assert event.changes["detached_refunds"] == 1


@pytest.mark.django_db
def test_delete_income_removes_allocations(search_setup):
    expense = Expense.objects.get(merchant="Corner MARKET")
    income = Income.objects.create(
        household=expense.household,
        created_by=expense.created_by,
        date=date(2026, 8, 1),
        amount=Decimal("100.00"),
        source="Paycheck",
    )
    allocation = IncomeAllocation.objects.create(
        income=income, account=expense.account, amount=income.amount
    )

    response = search_setup.post(
        reverse("transaction_delete", args=["income", income.pk]),
        {"confirmation": "Income: Paycheck"},
    )

    assert response.status_code == 302
    assert not Income.objects.filter(pk=income.pk).exists()
    assert not IncomeAllocation.objects.filter(pk=allocation.pk).exists()


@pytest.mark.django_db
def test_delete_transaction_rejects_locked_period(search_setup):
    expense = Expense.objects.get(merchant="Corner MARKET")
    PayPeriod.objects.create(
        household=expense.household,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 7, 31),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 9),
        is_locked=True,
    )

    response = search_setup.post(
        reverse("transaction_delete", args=["expense", expense.pk]),
        {"confirmation": "Expense: Corner MARKET"},
    )

    assert response.status_code == 200
    assert "This pay period is locked" in response.content.decode()
    assert Expense.objects.filter(pk=expense.pk).exists()
