from datetime import date, timedelta
from decimal import Decimal

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
from budget.services import account_balance, create_expense


@pytest.fixture
def setup(client, db):
    user = get_user_model().objects.create_user("pay-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    account = Account.objects.create(household=home, name="Bills", type="bills")
    category = Category.objects.create(household=home, name="Utilities", type="bill")
    bill = RecurringBill.objects.create(
        household=home,
        name="Internet",
        default_amount=Decimal("102.80"),
        due_day=20,
        category=category,
        account=account,
        effective_start=date(2026, 1, 1),
    )
    return client, home, account, category, bill, user


def occ(bill, due, amount="102.80", **kwargs):
    return BillOccurrence.objects.create(
        bill=bill, due_date=due, expected_amount=Decimal(amount), **kwargs
    )


def post_pay(client, item):
    return client.post(
        reverse("bill_mark_paid", args=[item.pk]),
        {"month": f"{item.due_date:%Y-%m}"},
        follow=True,
    )


def messages_of(response):
    return [str(m) for m in response.context["messages"]]


@pytest.mark.django_db
def test_mark_paid_creates_one_expense_and_sets_paid(setup):
    client, _, account, category, bill, _ = setup
    item = occ(bill, date(2026, 8, 20))
    response = post_pay(client, item)

    expense = Expense.objects.get()
    assert (expense.account, expense.category) == (account, category)
    assert expense.amount == Decimal("102.80")
    assert expense.bill_occurrence == item
    assert expense.date == timezone.localdate()
    item.refresh_from_db()
    assert item.status == BillOccurrence.Status.PAID
    assert item.confirmed_amount == Decimal("102.80")
    assert "Internet marked paid: $102.80 from Bills" in response.content.decode()
    assert "Undo works within 10 minutes." in response.content.decode()
    assert response.redirect_chain[-1][0].startswith("/bills/?month=2026-08")


@pytest.mark.django_db
def test_future_occurrence_is_dated_today(setup):
    client, _, _, _, bill, _ = setup
    item = occ(bill, timezone.localdate() + timedelta(days=40))
    post_pay(client, item)
    assert Expense.objects.get().date == timezone.localdate()


@pytest.mark.django_db
def test_partial_payment_then_mark_paid_pays_remainder(setup):
    client, home, account, category, bill, user = setup
    item = occ(bill, date(2026, 8, 20), confirmed_amount=Decimal("100.00"))
    create_expense(
        household=home,
        actor=user,
        entry_date=date(2026, 8, 5),
        account=account,
        category=category,
        amount=Decimal("40.00"),
        bill=bill,
        occurrence=item,
    )
    post_pay(client, item)
    amounts = sorted(Expense.objects.values_list("amount", flat=True))
    assert amounts == [Decimal("40.00"), Decimal("60.00")]
    item.refresh_from_db()
    assert item.status == BillOccurrence.Status.PAID


@pytest.mark.django_db
def test_double_post_creates_one_expense(setup):
    client, _, _, _, bill, _ = setup
    item = occ(bill, date(2026, 8, 20))
    post_pay(client, item)
    response = post_pay(client, item)
    assert Expense.objects.count() == 1
    assert "Internet is already paid." in messages_of(response)


@pytest.mark.django_db
def test_skipped_refuses(setup):
    client, _, _, _, bill, _ = setup
    item = occ(bill, date(2026, 8, 20), status=BillOccurrence.Status.SKIPPED)
    post_pay(client, item)
    assert not Expense.objects.exists()
    page = client.get(reverse("bills"), {"month": "2026-08"}).content.decode()
    assert "mark-paid" not in page


@pytest.mark.django_db
def test_locked_period_refuses(setup):
    client, home, _, _, bill, _ = setup
    today = timezone.localdate()
    PayPeriod.objects.create(
        household=home,
        nominal_payday=today,
        expected_deposit_date=today,
        start_date=today - timedelta(days=5),
        end_date=today + timedelta(days=5),
        is_locked=True,
    )
    item = occ(bill, date(2026, 8, 20))
    response = post_pay(client, item)
    assert not Expense.objects.exists()
    assert any("locked" in m for m in messages_of(response))
    item.refresh_from_db()
    assert item.confirmed_amount is None


@pytest.mark.django_db
def test_other_household_404_and_get_not_allowed(setup):
    client, _, _, _, _, _ = setup
    other = Household.objects.create()
    acct = Account.objects.create(household=other, name="Bills", type="bills")
    cat = Category.objects.create(household=other, name="Utilities", type="bill")
    other_bill = RecurringBill.objects.create(
        household=other,
        name="Water",
        default_amount=Decimal("10.00"),
        due_day=5,
        category=cat,
        account=acct,
        effective_start=date(2026, 1, 1),
    )
    item = occ(other_bill, date(2026, 8, 5), "10.00")
    assert client.post(reverse("bill_mark_paid", args=[item.pk])).status_code == 404
    assert client.get(reverse("bill_mark_paid", args=[item.pk])).status_code == 405
    assert not Expense.objects.exists()


@pytest.mark.django_db
def test_confirm_all_scope(setup):
    client, home, _, _, bill, _ = setup
    other_bill = RecurringBill.objects.create(
        household=home,
        name="Phone",
        default_amount=Decimal("50.00"),
        due_day=3,
        category=bill.category,
        account=bill.account,
        effective_start=date(2026, 1, 1),
    )
    removed = RecurringBill.objects.create(
        household=home,
        name="Old",
        default_amount=Decimal("5.00"),
        due_day=3,
        category=bill.category,
        account=bill.account,
        effective_start=date(2026, 1, 1),
        effective_end=date(2026, 7, 31),
        active=False,
    )
    aug = occ(bill, date(2026, 8, 20))
    aug_phone = occ(other_bill, date(2026, 8, 3), "50.00")
    sep = occ(bill, date(2026, 9, 20))
    gone = occ(removed, date(2026, 8, 3), "5.00")
    PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 10),
        is_locked=True,
    )
    response = client.post(reverse("bills_confirm_all"), {"month": "2026-08"}, follow=True)
    for item in (aug, aug_phone, sep, gone):
        item.refresh_from_db()
    assert aug.status == BillOccurrence.Status.CONFIRMED
    assert aug.confirmed_amount == Decimal("102.80")
    assert aug_phone.status == BillOccurrence.Status.UNCONFIRMED  # locked
    assert sep.status == BillOccurrence.Status.UNCONFIRMED
    assert gone.status == BillOccurrence.Status.UNCONFIRMED
    assert "Confirmed 1 bills (1 skipped: locked)" in messages_of(response)


@pytest.mark.django_db
def test_page_legend_labels_and_confirm_all_button(setup):
    client, _, _, _, bill, _ = setup
    occ(bill, date(2026, 8, 20))
    html = client.get(reverse("bills"), {"month": "2026-08"}).content.decode()
    assert "<details" in html and "amount not checked yet" in html
    assert "Partially paid" in html
    for label in ("Bill", "Due", "Status", "Amount", "Paid", "Actions"):
        assert f'data-label="{label}"' in html
    assert "Confirm all unchanged" in html
    assert "Paid $102.80" in html
    assert 'aria-label="Mark Internet paid' in html

    BillOccurrence.objects.update(status=BillOccurrence.Status.CONFIRMED)
    html = client.get(reverse("bills"), {"month": "2026-08"}).content.decode()
    assert "Confirm all unchanged" not in html


# --- Undo and "Change date" after Paid ----------------------------------------------------
# The URL is written out (not reverse()) so the tests fail on their assertions, not on a
# missing URL name, if the feature is absent.
UNDO_PAID_URL = "/bills/paid/undo/"


def post_undo(client, item):
    return client.post(UNDO_PAID_URL, {"month": f"{item.due_date:%Y-%m}"}, follow=True)


@pytest.mark.django_db
def test_paid_banner_offers_undo_and_change_date(setup):
    client, _, _, _, bill, _ = setup
    item = occ(bill, date(2026, 8, 20))
    response = post_pay(client, item)

    expense = Expense.objects.get()
    page = response.content.decode()
    assert "Internet marked paid: $102.80 from Bills" in page
    assert f"dated {expense.date.isoformat()}" in page
    assert ">Undo</button>" in page
    assert f'href="/transactions/expense/{expense.pk}/edit/">Change date' in page


@pytest.mark.django_db
def test_undo_returns_an_unconfirmed_month_to_exactly_unpaid(setup):
    client, home, account, _, bill, _ = setup
    item = occ(bill, date(2026, 8, 20))  # expected 102.80, never confirmed
    assert item.confirmed_amount is None
    post_pay(client, item)
    # Bills opens at 0.00; paying 102.80 leaves -102.80.
    assert account_balance(account) == Decimal("-102.80")

    response = post_undo(client, item)

    assert Expense.objects.count() == 0
    item.refresh_from_db()
    assert item.status == BillOccurrence.Status.UNCONFIRMED
    assert item.confirmed_amount is None
    assert item.amount_paid == Decimal("0.00")
    assert account_balance(account) == Decimal("0.00")  # -102.80 + 102.80
    assert "Internet is unpaid again." in messages_of(response)
    assert ">Undo</button>" not in response.content.decode()
    # The delete is on the audit trail.
    assert AuditEvent.objects.filter(household=home, action="delete").exists()


@pytest.mark.django_db
def test_undo_keeps_an_earlier_confirmed_amount(setup):
    client, _, account, _, bill, _ = setup
    item = occ(bill, date(2026, 8, 20), confirmed_amount=Decimal("99.50"))
    post_pay(client, item)
    assert Expense.objects.get().amount == Decimal("99.50")  # pays the confirmed amount

    post_undo(client, item)

    item.refresh_from_db()
    assert Expense.objects.count() == 0
    assert item.status == BillOccurrence.Status.CONFIRMED
    assert item.confirmed_amount == Decimal("99.50")
    assert account_balance(account) == Decimal("0.00")


@pytest.mark.django_db
def test_undo_after_a_partial_payment_leaves_the_earlier_payment(setup):
    client, home, account, category, bill, user = setup
    item = occ(bill, date(2026, 8, 20), confirmed_amount=Decimal("100.00"))
    create_expense(
        household=home,
        actor=user,
        entry_date=date(2026, 8, 5),
        account=account,
        category=category,
        amount=Decimal("40.00"),
        bill=bill,
        occurrence=item,
    )
    post_pay(client, item)
    paid_today = Expense.objects.get(date=timezone.localdate())
    assert paid_today.amount == Decimal("60.00")  # 100.00 - 40.00 already paid
    item.refresh_from_db()
    assert item.status == BillOccurrence.Status.PAID

    post_undo(client, item)

    item.refresh_from_db()
    assert list(Expense.objects.values_list("amount", flat=True)) == [Decimal("40.00")]
    assert item.status == BillOccurrence.Status.PARTIAL
    assert item.amount_paid == Decimal("40.00")
    assert item.confirmed_amount == Decimal("100.00")
    assert account_balance(account) == Decimal("-40.00")


@pytest.mark.django_db
def test_undo_twice_does_nothing_the_second_time(setup):
    client, _, account, _, bill, _ = setup
    item = occ(bill, date(2026, 8, 20))
    post_pay(client, item)
    post_undo(client, item)
    response = post_undo(client, item)

    assert "There is nothing to undo, or the undo time has passed." in messages_of(response)
    assert account_balance(account) == Decimal("0.00")
    item.refresh_from_db()
    assert item.status == BillOccurrence.Status.UNCONFIRMED


@pytest.mark.django_db
def test_undo_is_refused_after_the_pay_period_is_locked(setup):
    client, home, account, _, bill, _ = setup
    item = occ(bill, date(2026, 8, 20))
    post_pay(client, item)
    today = timezone.localdate()
    PayPeriod.objects.create(
        household=home,
        nominal_payday=today,
        expected_deposit_date=today,
        start_date=today - timedelta(days=3),
        end_date=today + timedelta(days=3),
        is_locked=True,
    )

    response = post_undo(client, item)

    assert Expense.objects.count() == 1  # the payment stays
    assert account_balance(account) == Decimal("-102.80")
    item.refresh_from_db()
    assert item.status == BillOccurrence.Status.PAID
    assert any("Could not undo the payment" in m for m in messages_of(response))


@pytest.mark.django_db
def test_undo_expires_after_ten_minutes(setup):
    client, _, account, _, bill, _ = setup
    item = occ(bill, date(2026, 8, 20))
    post_pay(client, item)
    session = client.session
    snapshot = session["undo_paid"]
    snapshot["deleted_at"] = (timezone.now() - timedelta(minutes=11)).isoformat()
    session["undo_paid"] = snapshot
    session.save()

    response = post_undo(client, item)

    assert Expense.objects.count() == 1
    assert account_balance(account) == Decimal("-102.80")
    assert "There is nothing to undo, or the undo time has passed." in messages_of(response)


@pytest.mark.django_db
def test_undo_is_refused_once_the_payment_amount_was_edited(setup):
    client, _, account, _, bill, _ = setup
    item = occ(bill, date(2026, 8, 20))
    post_pay(client, item)
    Expense.objects.update(amount=Decimal("90.00"))

    response = post_undo(client, item)

    assert Expense.objects.get().amount == Decimal("90.00")
    assert "There is nothing to undo, or the undo time has passed." in messages_of(response)
