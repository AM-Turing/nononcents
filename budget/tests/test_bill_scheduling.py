from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.urls import reverse

from budget.forms import ExpenseForm
from budget.models import (
    Account,
    AuditEvent,
    BillOccurrence,
    Category,
    Household,
    HouseholdMembership,
    PayPeriod,
    RecurringBill,
)
from budget.services import (
    choose_occurrence,
    create_expense,
    ensure_bill_occurrences,
)

PAID = BillOccurrence.Status.PAID
PARTIAL = BillOccurrence.Status.PARTIAL


@pytest.fixture
def setup(client, db):
    user = get_user_model().objects.create_user("sched-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    account = Account.objects.create(household=home, name="Bills", type="bills")
    category = Category.objects.create(household=home, name="Utilities", type="bill")
    bill = RecurringBill.objects.create(
        household=home,
        name="Electricity",
        default_amount=Decimal("100.00"),
        due_day=1,
        category=category,
        account=account,
        effective_start=date(2026, 1, 1),
    )
    return client, user, home, account, category, bill


def occurrence(bill, due, amount="100.00", **extra):
    return BillOccurrence.objects.create(
        bill=bill, due_date=due, expected_amount=Decimal(amount), **extra
    )


def pay(setup, when, amount="100.00", bill=None, **extra):
    _, user, home, account, category, default_bill = setup
    return create_expense(
        household=home,
        actor=user,
        entry_date=when,
        account=account,
        category=category,
        amount=Decimal(amount),
        bill=bill or default_bill,
        **extra,
    )


def test_early_payment_links_to_next_occurrence_when_this_month_is_paid(setup):
    bill = setup[-1]
    october = occurrence(bill, date(2026, 10, 1))
    november = occurrence(bill, date(2026, 11, 1))
    pay(setup, date(2026, 10, 2))
    october.refresh_from_db()
    assert october.status == PAID

    expense = pay(setup, date(2026, 10, 30))

    assert expense.bill_occurrence == november


def test_late_payment_links_to_the_unpaid_earlier_occurrence(setup):
    bill = setup[-1]
    bill.due_day = 28
    bill.save()
    october = occurrence(bill, date(2026, 10, 28))
    occurrence(bill, date(2026, 11, 28))

    expense = pay(setup, date(2026, 11, 3))

    assert expense.bill_occurrence == october


def test_tie_goes_to_the_earlier_due_date(setup):
    bill = setup[-1]
    earlier = occurrence(bill, date(2026, 10, 10))
    occurrence(bill, date(2026, 11, 20))

    assert choose_occurrence(bill, date(2026, 10, 30)) == earlier  # 20 days vs 21 days
    assert choose_occurrence(bill, date(2026, 10, 15)) == earlier


def test_falls_back_to_calendar_month_without_a_candidate(setup):
    bill = setup[-1]

    chosen = choose_occurrence(bill, date(2026, 6, 15))

    assert chosen.due_date == date(2026, 6, 1)


def test_explicit_occurrence_is_honoured(setup):
    bill = setup[-1]
    october = occurrence(bill, date(2026, 10, 1))
    occurrence(bill, date(2026, 11, 1))

    expense = pay(setup, date(2026, 10, 30), occurrence=october)

    assert expense.bill_occurrence == october


def test_another_bills_occurrence_is_rejected(setup):
    _, _, home, account, category, bill = setup
    other = RecurringBill.objects.create(
        household=home,
        name="Water",
        default_amount=Decimal("50.00"),
        due_day=1,
        category=category,
        account=account,
        effective_start=date(2026, 1, 1),
    )
    foreign = occurrence(other, date(2026, 10, 1), "50.00")

    with pytest.raises(ValidationError):
        pay(setup, date(2026, 10, 2), occurrence=foreign)


def test_skipped_occurrence_is_rejected(setup):
    bill = setup[-1]
    skipped = occurrence(bill, date(2026, 10, 1), status=BillOccurrence.Status.SKIPPED)

    with pytest.raises(ValidationError):
        pay(setup, date(2026, 10, 2), occurrence=skipped)


def test_expense_form_validates_the_bill_month_pairing(setup):
    _, _, home, account, category, bill = setup
    other = RecurringBill.objects.create(
        household=home,
        name="Water",
        default_amount=Decimal("50.00"),
        due_day=1,
        category=category,
        account=account,
        effective_start=date(2026, 1, 1),
    )
    foreign = occurrence(other, date(2026, 10, 1), "50.00")
    mine = occurrence(bill, date(2026, 10, 1))
    data = {
        "amount": "100.00",
        "date": "2026-10-02",
        "account": account.pk,
        "category": category.pk,
        "bill": bill.pk,
        "merchant": "Power",
        "notes": "",
    }

    bad = ExpenseForm(data={**data, "bill_occurrence": foreign.pk}, household=home)
    good = ExpenseForm(data={**data, "bill_occurrence": mine.pk}, household=home)
    blank = ExpenseForm(data=data, household=home)

    assert not bad.is_valid()
    assert good.is_valid(), good.errors
    assert blank.is_valid(), blank.errors
    assert "Electricity" in good.fields["bill_occurrence"].label_from_instance(mine)


def test_history_edit_keeps_occurrence_when_bill_unchanged(setup):
    client, _, home, account, category, bill = setup
    october = occurrence(bill, date(2026, 10, 1))
    occurrence(bill, date(2026, 11, 1))
    expense = pay(setup, date(2026, 10, 2))
    assert expense.bill_occurrence == october

    response = client.post(
        reverse("transaction_edit", args=["expense", expense.pk]),
        {
            "date": "2026-10-30",
            "description": "Power",
            "amount": "100.00",
            "category": category.pk,
            "bill": bill.pk,
            "notes": "",
        },
    )

    assert response.status_code == 302
    expense.refresh_from_db()
    assert expense.bill_occurrence == october


def test_confirm_on_fully_paid_occurrence_becomes_paid(setup):
    client, *_, bill = setup
    october = occurrence(bill, date(2026, 10, 1))
    pay(setup, date(2026, 10, 2))

    client.post(reverse("bill_confirm", args=[october.pk]))

    october.refresh_from_db()
    assert october.status == PAID


def test_edit_amount_below_paid_is_paid_and_above_is_partial(setup):
    client, *_, bill = setup
    october = occurrence(bill, date(2026, 10, 1))
    pay(setup, date(2026, 10, 2))
    url = reverse("bill_edit_amount", args=[october.pk])
    form = {"bill_name": "Electricity", "due_date": "2026-10-01", "notes": ""}

    client.post(url, {**form, "confirmed_amount": "80.00"})
    october.refresh_from_db()
    assert october.status == PAID

    client.post(url, {**form, "confirmed_amount": "150.00"})
    october.refresh_from_db()
    assert october.status == PARTIAL


def test_removing_a_bill_deletes_unpaid_future_and_keeps_paid(setup):
    client, *_, bill = setup
    october = occurrence(bill, date(2026, 10, 1))
    november = occurrence(bill, date(2026, 11, 1))
    december = occurrence(bill, date(2026, 12, 1))
    pay(setup, date(2026, 12, 1), occurrence=december)

    client.post(reverse("bill_delete", args=[november.pk]), {"confirmation": "Electricity"})

    bill.refresh_from_db()
    assert not bill.active
    assert bill.effective_end == date(2026, 10, 31)
    remaining = set(bill.occurrences.values_list("pk", flat=True))
    assert remaining == {october.pk, december.pk}
    event = AuditEvent.objects.get(
        record_type="budget.RecurringBill", record_id=str(bill.pk), action="delete"
    )
    assert event.changes["occurrences_deleted"] == 1
    assert event.changes["occurrences_kept"] == 1


def test_removing_a_bill_is_refused_when_a_deleted_month_is_locked(setup):
    client, _, home, _, _, bill = setup
    november = occurrence(bill, date(2026, 11, 1))
    PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 11, 1),
        expected_deposit_date=date(2026, 11, 1),
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 14),
        is_locked=True,
    )

    client.post(reverse("bill_delete", args=[november.pk]), {"confirmation": "Electricity"})

    bill.refresh_from_db()
    assert bill.active
    assert BillOccurrence.objects.filter(pk=november.pk).exists()


def test_ensure_bill_occurrences_is_idempotent_and_respects_bounds(setup):
    _, _, home, account, category, bill = setup
    bill.effective_start = date(2026, 3, 15)
    bill.due_day = 20
    bill.save()
    ended = RecurringBill.objects.create(
        household=home,
        name="Old",
        default_amount=Decimal("10.00"),
        due_day=5,
        category=category,
        account=account,
        active=False,
        effective_start=date(2026, 1, 1),
        effective_end=date(2026, 4, 30),
    )
    dormant = RecurringBill.objects.create(
        household=home,
        name="Dormant",
        default_amount=Decimal("10.00"),
        due_day=5,
        category=category,
        account=account,
        active=False,
        effective_start=date(2026, 1, 1),
    )

    first = ensure_bill_occurrences(home, through=date(2026, 6, 30), start=date(2026, 1, 1))
    again = ensure_bill_occurrences(home, through=date(2026, 6, 30), start=date(2026, 1, 1))

    assert again == []
    assert len(first) == 4 + 4 + 0
    assert sorted(bill.occurrences.values_list("due_date", flat=True))[0] == date(2026, 3, 20)
    assert max(ended.occurrences.values_list("due_date", flat=True)) == date(2026, 4, 5)
    assert not dormant.occurrences.exists()


def test_generate_command_creates_counts_only(setup):
    out = StringIO()

    call_command("generate_bill_occurrences", "--months", "2", stdout=out)

    assert BillOccurrence.objects.count() >= 3
    assert "Electricity" not in out.getvalue()
    assert "Total created" in out.getvalue()


def test_cleanup_dry_run_changes_nothing_and_apply_fixes(setup):
    _, _, home, _, _, bill = setup
    paid = occurrence(bill, date(2026, 7, 1), status=BillOccurrence.Status.CONFIRMED)
    pay(setup, date(2026, 7, 2), occurrence=paid)
    BillOccurrence.objects.filter(pk=paid.pk).update(status=BillOccurrence.Status.CONFIRMED)
    ended = RecurringBill.objects.create(
        household=home,
        name="Gone",
        default_amount=Decimal("10.00"),
        due_day=5,
        category=bill.category,
        account=bill.account,
        active=False,
        effective_start=date(2026, 1, 1),
        effective_end=date(2026, 5, 31),
    )
    ghost = occurrence(ended, date(2026, 8, 5), "10.00")
    locked_ghost = occurrence(ended, date(2026, 9, 5), "10.00")
    PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 9, 1),
        expected_deposit_date=date(2026, 9, 1),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 14),
        is_locked=True,
    )

    out = StringIO()
    call_command("cleanup_bill_occurrences", stdout=out)

    paid.refresh_from_db()
    assert paid.status == BillOccurrence.Status.CONFIRMED
    assert BillOccurrence.objects.filter(pk=ghost.pk).exists()
    assert "Dry run" in out.getvalue()

    call_command("cleanup_bill_occurrences", "--apply", stdout=StringIO())

    paid.refresh_from_db()
    assert paid.status == PAID
    assert not BillOccurrence.objects.filter(pk=ghost.pk).exists()
    assert BillOccurrence.objects.filter(pk=locked_ghost.pk).exists()
    assert AuditEvent.objects.filter(actor=None, action="delete").exists()


def test_bills_page_shows_forecast_for_variable_bills(setup):
    client, *_, bill = setup
    for month in (7, 8, 9):
        paid = occurrence(bill, date(2026, month, 1))
        paid.confirmed_amount = Decimal("500.00")
        paid.save()
        pay(setup, date(2026, month, 2), "500.00", occurrence=paid)
    target = occurrence(bill, date(2026, 10, 1))

    response = client.get(reverse("bills"), {"month": "2026-10"})

    assert target.pk
    assert b"Forecast $500.00" in response.content
    assert b"average of last 3 payments" in response.content
