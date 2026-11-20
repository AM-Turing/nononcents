from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from budget.models import (
    Account,
    BillOccurrence,
    Category,
    Household,
    HouseholdMembership,
    PayPeriod,
    RecurringBill,
)
from budget.services import (
    bill_due_in_month,
    change_bill_frequency,
    create_expense,
    ensure_bill_occurrences,
)

TODAY = date(2026, 11, 18)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(timezone, "localdate", lambda *a, **k: TODAY)


@pytest.fixture
def setup(client, db, frozen):
    user = get_user_model().objects.create_user("freq-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    account = Account.objects.create(household=home, name="Bills", type="bills")
    category = Category.objects.create(household=home, name="Insurance", type="bill")
    bill = RecurringBill.objects.create(
        household=home,
        name="Car insurance",
        default_amount=Decimal("390.37"),
        due_day=15,
        category=category,
        account=account,
        effective_start=date(2026, 1, 1),
    )
    return client, user, home, account, category, bill


def months(bill):
    return [o.due_date.strftime("%Y-%m") for o in bill.occurrences.order_by("due_date")]


def test_schedule_rule():
    bill = RecurringBill(effective_start=date(2026, 1, 20), every_months=3)
    assert [m for m in range(1, 13) if bill_due_in_month(bill, date(2026, m, 1))] == [1, 4, 7, 10]
    bill.schedule_start = date(2026, 3, 1)
    bill.every_months = 12
    assert bill_due_in_month(bill, date(2027, 3, 1))
    assert not bill_due_in_month(bill, date(2026, 2, 1))  # before the schedule starts
    assert not bill_due_in_month(bill, date(2027, 4, 1))


@pytest.mark.django_db
def test_generation_follows_the_frequency(setup):
    *_, home, _, _, bill = setup
    bill.every_months = 2
    bill.schedule_start = date(2026, 11, 1)
    bill.save()
    ensure_bill_occurrences(home, through=date(2027, 4, 30), start=TODAY)
    assert months(bill) == ["2026-11", "2027-01", "2027-03"]


@pytest.mark.django_db
def test_changing_frequency_removes_only_unpaid_future_months(setup):
    _, user, home, account, category, bill = setup
    ensure_bill_occurrences(home, through=date(2027, 3, 31), start=date(2026, 10, 1))
    assert months(bill) == ["2026-10", "2026-11", "2026-12", "2027-01", "2027-02", "2027-03"]
    # December is already paid; January was skipped on purpose.
    december = bill.occurrences.get(due_date__month=12)
    create_expense(
        household=home,
        actor=user,
        entry_date=date(2026, 11, 18),
        account=account,
        category=category,
        amount=Decimal("390.37"),
        bill=bill,
        occurrence=december,
    )
    bill.occurrences.filter(due_date__year=2027, due_date__month=1).update(
        status=BillOccurrence.Status.SKIPPED
    )
    removed, kept = change_bill_frequency(bill, user, 12, date(2027, 2, 1))
    bill.refresh_from_db()
    assert (bill.every_months, bill.schedule_start) == (12, date(2027, 2, 1))
    # Oct and Nov are past due dates (history); Dec paid and Jan skipped are kept;
    # Feb starts the yearly schedule; March no longer fits and is removed.
    assert months(bill) == ["2026-10", "2026-11", "2026-12", "2027-01", "2027-02"]
    assert (removed, kept) == (1, 2)


@pytest.mark.django_db
def test_locked_months_are_kept(setup):
    _, user, home, *_, bill = setup
    ensure_bill_occurrences(home, through=date(2026, 12, 31), start=TODAY)
    PayPeriod.objects.create(
        household=home,
        start_date=date(2026, 12, 1),
        end_date=date(2026, 12, 31),
        nominal_payday=date(2026, 12, 1),
        expected_deposit_date=date(2026, 12, 1),
        is_locked=True,
    )
    removed, kept = change_bill_frequency(bill, user, 3, date(2027, 2, 1))
    assert "2026-12" in months(bill) and kept == 1


@pytest.mark.django_db
def test_edit_page_changes_frequency(setup):
    client, _, home, account, category, bill = setup
    ensure_bill_occurrences(home, through=date(2027, 2, 28), start=TODAY)
    november = bill.occurrences.get(due_date__month=11)
    response = client.post(
        f"/bills/{november.pk}/edit-amount/",
        {
            "bill_name": "Car insurance",
            "category": category.pk,
            "account": account.pk,
            "due_date": "2026-11-15",
            "confirmed_amount": "390.37",
            "notes": "",
            "bill_every_months": "12",
            "bill_first_month": "2026-11",
        },
        follow=True,
    )
    assert "Now once a year from November 2026" in response.content.decode()
    assert months(bill) == ["2026-11"]


@pytest.mark.django_db
def test_add_bill_with_frequency(setup):
    client, _, home, account, category, _ = setup
    client.post(
        "/bills/add/",
        {
            "name": "Vehicle registration",
            "default_amount": "60.00",
            "due_day": "5",
            "every_months": "12",
            "category": category.pk,
            "account": account.pk,
            "effective_start": "2026-12-01",
            "notes": "",
            "active": "on",
        },
    )
    bill = RecurringBill.objects.get(name="Vehicle registration")
    assert bill.every_months == 12
    assert months(bill) == ["2026-12"]
