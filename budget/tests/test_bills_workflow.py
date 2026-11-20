from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from budget.models import (
    Account,
    AuditEvent,
    BillOccurrence,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    PayPeriod,
    RecurringBill,
)
from budget.services import occurrence_for_expense_date


@pytest.fixture
def bill_setup(client, db):
    user = get_user_model().objects.create_user("bill-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    account = Account.objects.create(household=home, name="Bills", type="bills")
    category = Category.objects.create(household=home, name="Utilities", type="bill")
    bill = RecurringBill.objects.create(
        household=home,
        name="Internet",
        default_amount=Decimal("100.00"),
        due_day=29,
        category=category,
        account=account,
        effective_start=date(2026, 1, 1),
    )
    return client, home, account, category, bill


@pytest.mark.django_db
def test_bills_page_shows_only_selected_month(bill_setup):
    client, _, _, _, bill = bill_setup
    august = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )
    BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 9, 29), expected_amount=Decimal("100.00")
    )

    response = client.get(reverse("bills"), {"month": "2026-08"})

    assert response.status_code == 200
    assert list(response.context["occurrences"]) == [august]
    assert response.context["selected_month"] == date(2026, 8, 1)
    assert "Edit bill" in response.content.decode()


@pytest.mark.django_db
def test_edit_and_remove_live_in_the_per_bill_more_menu(bill_setup):
    client, _, _, _, bill = bill_setup
    BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )

    html = client.get(reverse("bills"), {"month": "2026-08"}).content.decode()

    menu = html.split('<details class="bill-more">')[1].split("</details>")[0]
    assert "More<span" in menu and f"for {bill.name}" in menu
    assert "Edit bill" in menu and ">Remove<" in menu
    for label in ("Paid $", ">Confirm<", ">Skip<"):
        assert label not in menu
        assert label in html.split('<details class="bill-more">')[0]


@pytest.mark.django_db
def test_add_bill_creates_its_first_month_obligation(bill_setup):
    client, _, account, category, _ = bill_setup
    today = timezone.localdate()
    response = client.post(
        reverse("bill_add"),
        {
            "name": "Water",
            "default_amount": "75.25",
            "due_day": "20",
            "category": category.pk,
            "account": account.pk,
            "effective_start": f"{today:%Y-%m}-01",
            "notes": "",
            "active": "on",
        },
    )

    assert response.status_code == 302
    added = RecurringBill.objects.get(name="Water")
    occurrence = BillOccurrence.objects.get(bill=added, due_date__month=today.month)
    assert occurrence.due_date.day == 20
    assert BillOccurrence.objects.filter(bill=added).count() == 4
    assert occurrence.expected_amount == Decimal("75.25")


@pytest.mark.django_db
def test_add_bill_can_use_spending_but_not_savings_account(bill_setup):
    client, home, bills_account, category, _ = bill_setup
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    Account.objects.create(household=home, name="Savings", type="savings")

    page = client.get(reverse("bill_add"))
    assert list(page.context["form"].fields["account"].queryset) == [
        bills_account,
        spending,
    ]

    response = client.post(
        reverse("bill_add"),
        {
            "name": "Video subscription",
            "default_amount": "12.57",
            "due_day": "3",
            "category": category.pk,
            "account": spending.pk,
            "effective_start": "2026-08-01",
            "notes": "",
            "active": "on",
        },
    )

    assert response.status_code == 302
    added = RecurringBill.objects.get(name="Video subscription")
    assert added.account == spending


@pytest.mark.django_db
def test_subscription_payment_can_use_spending_account(bill_setup):
    client, home, _, category, bill = bill_setup
    spending = Account.objects.create(
        household=home,
        name="Spending",
        type=Account.Type.SPENDING,
        opening_balance=Decimal("150.00"),
    )

    response = client.post(
        reverse("expense_add"),
        {
            "amount": "40.00",
            "date": "2026-08-10",
            "account": spending.pk,
            "category": category.pk,
            "bill": bill.pk,
            "merchant": "Internet",
            "notes": "Paid from Spending",
            "form_token": str(uuid4()),
        },
    )

    assert response.status_code == 302
    payment = Expense.objects.get(merchant="Internet")
    assert payment.account == spending
    assert payment.bill_occurrence.bill == bill
    assert payment.bill_occurrence.amount_paid == Decimal("40.00")
    assert payment.bill_occurrence.status == BillOccurrence.Status.PARTIAL


@pytest.mark.django_db
def test_history_edit_can_apply_an_existing_expense_to_a_bill(bill_setup):
    client, home, _, bill_category, bill = bill_setup
    spending = Account.objects.create(
        household=home,
        name="Spending",
        type=Account.Type.SPENDING,
        opening_balance=Decimal("150.00"),
    )
    groceries = Category.objects.create(
        household=home, name="Groceries", type=Category.Type.SPENDING
    )
    expense = Expense.objects.create(
        household=home,
        date=date(2026, 8, 10),
        amount=Decimal("40.00"),
        account=spending,
        category=groceries,
        merchant="Internet",
    )
    url = reverse("transaction_edit", args=["expense", expense.pk])

    edit_page = client.get(url)
    assert list(edit_page.context["form"].fields["bill"].queryset) == [bill]

    response = client.post(
        url,
        {
            "date": "2026-08-10",
            "description": "Internet",
            "amount": "40.00",
            "category": bill_category.pk,
            "notes": "Applied after review",
            "bill": bill.pk,
        },
    )

    assert response.status_code == 302
    expense.refresh_from_db()
    assert expense.category == bill_category
    assert expense.notes == "Applied after review"
    assert expense.bill_occurrence.bill == bill
    assert expense.bill_occurrence.amount_paid == Decimal("40.00")
    assert expense.bill_occurrence.status == BillOccurrence.Status.PARTIAL
    event = AuditEvent.objects.filter(
        record_type="budget.Expense", record_id=str(expense.pk), action="update"
    ).latest("pk")
    assert event.changes["bill"] == "Internet"


@pytest.mark.django_db
def test_confirm_returns_to_the_confirmed_bill(bill_setup):
    client, _, _, _, bill = bill_setup
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )

    response = client.post(
        reverse("bill_confirm", args=[occurrence.pk]),
        {"month": "2026-08"},
    )

    assert response.status_code == 302
    assert response.url == f"/bills/?month=2026-08#bill-{occurrence.pk}"


@pytest.mark.django_db
def test_edit_month_can_preserve_or_update_future_default(bill_setup):
    client, _, _, _, bill = bill_setup
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )
    response = client.post(
        reverse("bill_edit_amount", args=[occurrence.pk]),
        {
            "bill_name": "Internet",
            "due_date": "2026-08-29",
            "confirmed_amount": "125.00",
            "notes": "August only",
        },
    )
    assert response.status_code == 302
    occurrence.refresh_from_db()
    bill.refresh_from_db()
    assert occurrence.confirmed_amount == Decimal("125.00")
    assert bill.default_amount == Decimal("100.00")

    response = client.post(
        reverse("bill_edit_amount", args=[occurrence.pk]),
        {
            "bill_name": "Internet",
            "due_date": "2026-08-29",
            "confirmed_amount": "130.00",
            "notes": "",
            "update_future_default": "on",
        },
    )
    assert response.status_code == 302
    bill.refresh_from_db()
    assert bill.default_amount == Decimal("130.00")


@pytest.mark.django_db
def test_edit_bill_name_updates_the_recurring_bill_and_audits_it(bill_setup):
    client, _, _, _, bill = bill_setup
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )

    edit_page = client.get(reverse("bill_edit_amount", args=[occurrence.pk]))
    assert list(edit_page.context["form"].fields)[0] == "bill_name"

    response = client.post(
        reverse("bill_edit_amount", args=[occurrence.pk]),
        {
            "bill_name": "Home Internet",
            "due_date": "2026-08-29",
            "confirmed_amount": "100.00",
            "notes": "",
        },
    )

    assert response.status_code == 302
    bill.refresh_from_db()
    assert bill.name == "Home Internet"
    assert AuditEvent.objects.filter(
        record_type="budget.RecurringBill",
        record_id=str(bill.pk),
        changes={"name": "Home Internet"},
    ).exists()


@pytest.mark.django_db
def test_edit_bill_changes_category_and_account_without_rewriting_prior_month(bill_setup):
    client, home, bills_account, category, bill = bill_setup
    subscriptions = Category.objects.create(
        household=home, name="Subscriptions", type=Category.Type.BILL
    )
    spending = Account.objects.create(household=home, name="Spending", type=Account.Type.SPENDING)
    july = BillOccurrence.objects.create(
        bill=bill,
        category=category,
        account=bills_account,
        due_date=date(2026, 7, 29),
        expected_amount=Decimal("100.00"),
    )
    august = BillOccurrence.objects.create(
        bill=bill,
        category=category,
        account=bills_account,
        due_date=date(2026, 8, 29),
        expected_amount=Decimal("100.00"),
    )
    september = BillOccurrence.objects.create(
        bill=bill,
        category=category,
        account=bills_account,
        due_date=date(2026, 9, 29),
        expected_amount=Decimal("100.00"),
    )

    response = client.post(
        reverse("bill_edit_amount", args=[august.pk]),
        {
            "bill_name": "Internet",
            "category": subscriptions.pk,
            "account": spending.pk,
            "due_date": "2026-08-29",
            "confirmed_amount": "100.00",
            "notes": "",
        },
    )

    assert response.status_code == 302
    bill.refresh_from_db()
    july.refresh_from_db()
    august.refresh_from_db()
    september.refresh_from_db()
    assert (bill.category, bill.account) == (subscriptions, spending)
    assert (july.category, july.account) == (category, bills_account)
    assert (august.category, august.account) == (subscriptions, spending)
    assert (september.category, september.account) == (subscriptions, spending)


@pytest.mark.django_db
def test_edit_bill_name_must_be_unique_within_the_profile(bill_setup):
    client, home, account, category, bill = bill_setup
    RecurringBill.objects.create(
        household=home,
        name="Electricity",
        default_amount=Decimal("80.00"),
        due_day=10,
        category=category,
        account=account,
        effective_start=date(2026, 1, 1),
    )
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )

    response = client.post(
        reverse("bill_edit_amount", args=[occurrence.pk]),
        {
            "bill_name": "Electricity",
            "due_date": "2026-08-29",
            "confirmed_amount": "100.00",
            "notes": "",
        },
    )

    assert response.status_code == 200
    assert "A bill with this name already exists" in response.content.decode()
    bill.refresh_from_db()
    assert bill.name == "Internet"


@pytest.mark.django_db
def test_edit_month_accepts_zero_without_changing_future_default(bill_setup):
    client, _, _, _, bill = bill_setup
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )

    response = client.post(
        reverse("bill_edit_amount", args=[occurrence.pk]),
        {
            "bill_name": "Internet",
            "due_date": "2026-08-29",
            "confirmed_amount": "0.00",
            "notes": "Nothing due",
        },
    )

    assert response.status_code == 302
    occurrence.refresh_from_db()
    bill.refresh_from_db()
    assert occurrence.confirmed_amount == Decimal("0.00")
    assert occurrence.remaining_amount == Decimal("0.00")
    # A zero amount is settled by definition, so the recomputed status is paid.
    assert occurrence.status == BillOccurrence.Status.PAID
    assert bill.default_amount == Decimal("100.00")


@pytest.mark.django_db
def test_zero_monthly_amount_cannot_become_future_default(bill_setup):
    client, _, _, _, bill = bill_setup
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )

    response = client.post(
        reverse("bill_edit_amount", args=[occurrence.pk]),
        {
            "bill_name": "Internet",
            "due_date": "2026-08-29",
            "confirmed_amount": "0.00",
            "notes": "",
            "update_future_default": "on",
        },
    )

    assert response.status_code == 200
    assert "A zero amount can apply only to this month" in response.content.decode()
    occurrence.refresh_from_db()
    bill.refresh_from_db()
    assert occurrence.confirmed_amount is None
    assert bill.default_amount == Decimal("100.00")


@pytest.mark.django_db
def test_edit_month_can_change_due_date_and_reassign_pay_period(bill_setup):
    client, home, _, _, bill = bill_setup
    first_period = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 7, 31),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 15),
    )
    second_period = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 16),
        expected_deposit_date=date(2026, 8, 15),
        start_date=date(2026, 8, 16),
        end_date=date(2026, 8, 31),
    )
    occurrence = BillOccurrence.objects.create(
        bill=bill,
        due_date=date(2026, 8, 10),
        expected_amount=Decimal("100.00"),
        pay_period=first_period,
    )

    response = client.post(
        reverse("bill_edit_amount", args=[occurrence.pk]),
        {
            "bill_name": "Internet",
            "due_date": "2026-08-20",
            "confirmed_amount": "125.00",
            "notes": "Moved",
        },
    )

    assert response.status_code == 302
    occurrence.refresh_from_db()
    bill.refresh_from_db()
    assert occurrence.due_date == date(2026, 8, 20)
    assert occurrence.pay_period == second_period
    assert bill.due_day == 29
    event = AuditEvent.objects.get(
        record_type="budget.BillOccurrence", record_id=str(occurrence.pk)
    )
    assert event.changes["due_date"] == "2026-08-20"


@pytest.mark.django_db
def test_edit_month_can_update_future_due_day_without_rewriting_existing_months(bill_setup):
    client, _, _, _, bill = bill_setup
    august = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )
    september = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 9, 29), expected_amount=Decimal("100.00")
    )

    response = client.post(
        reverse("bill_edit_amount", args=[august.pk]),
        {
            "bill_name": "Internet",
            "due_date": "2026-08-20",
            "confirmed_amount": "100.00",
            "notes": "",
            "update_future_due_day": "on",
        },
    )

    assert response.status_code == 302
    bill.refresh_from_db()
    august.refresh_from_db()
    september.refresh_from_db()
    assert bill.due_day == 20
    assert august.due_date == date(2026, 8, 20)
    assert september.due_date == date(2026, 9, 29)
    assert occurrence_for_expense_date(bill, date(2026, 9, 1)) == september
    assert AuditEvent.objects.filter(
        record_type="budget.RecurringBill",
        record_id=str(bill.pk),
        changes={"due_day": 20},
    ).exists()


@pytest.mark.django_db
def test_edit_due_date_must_remain_in_same_month(bill_setup):
    client, _, _, _, bill = bill_setup
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )

    response = client.post(
        reverse("bill_edit_amount", args=[occurrence.pk]),
        {
            "bill_name": "Internet",
            "due_date": "2026-09-01",
            "confirmed_amount": "125.00",
            "notes": "",
        },
    )

    assert response.status_code == 200
    assert "Choose a date in the same month as this bill" in response.content.decode()
    occurrence.refresh_from_db()
    assert occurrence.due_date == date(2026, 8, 29)


@pytest.mark.django_db
def test_edit_due_date_cannot_move_into_locked_period(bill_setup):
    client, home, _, _, bill = bill_setup
    open_period = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 7, 31),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 15),
    )
    PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 16),
        expected_deposit_date=date(2026, 8, 15),
        start_date=date(2026, 8, 16),
        end_date=date(2026, 8, 31),
        is_locked=True,
    )
    occurrence = BillOccurrence.objects.create(
        bill=bill,
        due_date=date(2026, 8, 10),
        expected_amount=Decimal("100.00"),
        pay_period=open_period,
    )

    response = client.post(
        reverse("bill_edit_amount", args=[occurrence.pk]),
        {
            "bill_name": "Internet",
            "due_date": "2026-08-20",
            "confirmed_amount": "125.00",
            "notes": "",
        },
    )

    assert response.status_code == 200
    assert "This pay period is locked" in response.content.decode()
    occurrence.refresh_from_db()
    assert occurrence.due_date == date(2026, 8, 10)
    assert occurrence.confirmed_amount is None


@pytest.mark.django_db
def test_edit_back_link_returns_to_referring_month(bill_setup):
    client, _, _, _, bill = bill_setup
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )
    previous_page = "http://testserver/bills/?month=2026-08"

    response = client.get(
        reverse("bill_edit_amount", args=[occurrence.pk]), HTTP_REFERER=previous_page
    )

    assert response.context["return_url"] == previous_page
    assert f'href="{previous_page}"' in response.content.decode()


@pytest.mark.django_db
def test_back_link_rejects_an_external_referrer(bill_setup):
    client, _, _, _, bill = bill_setup
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )

    response = client.get(
        reverse("bill_edit_amount", args=[occurrence.pk]),
        HTTP_REFERER="https://malicious.example/leave",
    )

    assert response.context["return_url"] == "/bills/?month=2026-08"


@pytest.mark.django_db
def test_skip_requires_reason_and_removes_bill_from_remaining_total(bill_setup):
    client, _, _, _, bill = bill_setup
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )

    response = client.post(
        reverse("bill_skip", args=[occurrence.pk]),
        {"month": "2026-08", "reason": "  Not due this month  "},
        follow=True,
    )

    occurrence.refresh_from_db()
    assert occurrence.status == BillOccurrence.Status.SKIPPED
    assert occurrence.skipped_reason == "Not due this month"
    assert response.context["month_remaining"] == Decimal("0")
    assert AuditEvent.objects.filter(
        record_type="budget.BillOccurrence",
        record_id=str(occurrence.pk),
        changes__status=BillOccurrence.Status.SKIPPED,
    ).exists()

    occurrence.status = BillOccurrence.Status.UNCONFIRMED
    occurrence.skipped_reason = ""
    occurrence.save()
    client.post(reverse("bill_skip", args=[occurrence.pk]), {"month": "2026-08", "reason": " "})
    occurrence.refresh_from_db()
    assert occurrence.status == BillOccurrence.Status.UNCONFIRMED


@pytest.mark.django_db
def test_remove_bill_stops_schedule_and_preserves_history(bill_setup):
    client, _, account, category, bill = bill_setup
    july = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 7, 29), expected_amount=Decimal("100.00")
    )
    august = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )
    payment = Expense.objects.create(
        household=bill.household,
        created_by=bill.household.memberships.first().user,
        date=date(2026, 7, 29),
        amount=Decimal("100.00"),
        account=account,
        category=category,
        merchant="Internet",
        bill_occurrence=july,
    )

    response = client.post(
        reverse("bill_delete", args=[august.pk]),
        {"confirmation": "Internet"},
    )

    assert response.status_code == 302
    bill.refresh_from_db()
    assert bill.active is False
    assert bill.effective_end == date(2026, 7, 31)
    assert payment.refresh_from_db() is None
    assert client.get(reverse("bills"), {"month": "2026-08"}).context["occurrences"].count() == 0
    assert list(client.get(reverse("bills"), {"month": "2026-07"}).context["occurrences"]) == [july]
    assert AuditEvent.objects.filter(
        record_type="budget.RecurringBill",
        record_id=str(bill.pk),
        action="delete",
        changes__history_preserved=True,
    ).exists()


@pytest.mark.django_db
def test_bill_with_linked_payment_cannot_be_skipped(bill_setup):
    client, home, account, category, bill = bill_setup
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 29), expected_amount=Decimal("100.00")
    )
    Expense.objects.create(
        household=home,
        date=date(2026, 8, 20),
        amount=Decimal("25.00"),
        account=account,
        category=category,
        bill_occurrence=occurrence,
    )

    client.post(
        reverse("bill_skip", args=[occurrence.pk]),
        {"month": "2026-08", "reason": "Not due"},
    )

    occurrence.refresh_from_db()
    assert occurrence.status == BillOccurrence.Status.UNCONFIRMED
    assert occurrence.skipped_reason == ""


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["bill_confirm", "bill_edit_amount", "bill_skip", "bill_delete"])
def test_locked_period_rejects_bill_occurrence_changes(bill_setup, action):
    client, home, _, _, bill = bill_setup
    period = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 15),
        expected_deposit_date=date(2026, 8, 14),
        start_date=date(2026, 8, 14),
        end_date=date(2026, 8, 31),
        is_locked=True,
    )
    occurrence = BillOccurrence.objects.create(
        bill=bill,
        due_date=date(2026, 8, 29),
        expected_amount=Decimal("100.00"),
        pay_period=period,
    )
    payload = {
        "bill_confirm": {"month": "2026-08"},
        "bill_edit_amount": {
            "bill_name": "Internet",
            "due_date": "2026-08-29",
            "confirmed_amount": "125.00",
            "notes": "",
        },
        "bill_skip": {"month": "2026-08", "reason": "Not due"},
        "bill_delete": {"confirmation": "Internet"},
    }[action]

    client.post(reverse(action, args=[occurrence.pk]), payload)

    occurrence.refresh_from_db()
    bill.refresh_from_db()
    assert occurrence.status == BillOccurrence.Status.UNCONFIRMED
    assert occurrence.confirmed_amount is None
    assert occurrence.skipped_reason == ""
    assert bill.default_amount == Decimal("100.00")
    assert bill.active is True
    assert not AuditEvent.objects.filter(
        record_type="budget.BillOccurrence", record_id=str(occurrence.pk)
    ).exists()
