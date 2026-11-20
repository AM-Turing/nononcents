from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from budget import undo
from budget.models import (
    Account,
    AuditEvent,
    BillOccurrence,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    Income,
    PayPeriod,
    RecurringBill,
    Refund,
    Transfer,
)
from budget.services import (
    account_balance,
    create_expense,
    create_income,
    create_refund,
    create_transfer,
)


@pytest.fixture
def setup(client, db):
    user = get_user_model().objects.create_user("undo-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    session = client.session
    session["active_household_id"] = home.pk
    session.save()
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    savings = Account.objects.create(household=home, name="Savings", type="savings")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    household_goods = Category.objects.create(household=home, name="Home", type="spending")
    utilities = Category.objects.create(household=home, name="Utilities", type="bill")
    period = PayPeriod.objects.create(
        household=home,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 14),
        nominal_payday=date(2026, 11, 1),
        expected_deposit_date=date(2026, 11, 1),
    )
    PayPeriod.objects.create(
        household=home,
        start_date=date(2026, 11, 15),
        end_date=date(2026, 11, 30),
        nominal_payday=date(2026, 11, 15),
        expected_deposit_date=date(2026, 11, 14),
    )
    return {
        "client": client,
        "user": user,
        "home": home,
        "spending": spending,
        "bills": bills,
        "savings": savings,
        "groceries": groceries,
        "home_goods": household_goods,
        "utilities": utilities,
        "period": period,
    }


def quick_delete(client, kind, pk):
    return client.post(
        reverse("transaction_delete", args=[kind, pk]),
        {"quick": "1", "next": "/transactions/?q=x"},
    )


def do_undo(client):
    return client.post(reverse("transaction_undo"), {"next": "/transactions/"}, follow=True)


@pytest.mark.django_db
def test_quick_delete_and_undo_expense(setup):
    s = setup
    expense = create_expense(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 11, 5),
        account=s["spending"],
        category=s["groceries"],
        amount="42.10",
        merchant="Grocer",
        notes="weekly",
        splits=[(s["groceries"], Decimal("30.00")), (s["home_goods"], Decimal("12.10"))],
    )
    Expense.objects.filter(pk=expense.pk).update(cleared_at=timezone.now())
    before = account_balance(s["spending"])

    response = quick_delete(s["client"], "expense", expense.pk)
    assert response.status_code == 302
    assert response["Location"] == "/transactions/?q=x"
    assert not Expense.objects.filter(pk=expense.pk).exists()
    assert account_balance(s["spending"]) == before + Decimal("42.10")

    page = s["client"].get(reverse("transactions"))
    assert b"Undo" in page.content
    assert b"Grocer" in page.content

    response = do_undo(s["client"])
    restored = Expense.objects.get(household=s["home"])
    assert restored.pk != expense.pk
    assert (restored.date, restored.amount, restored.merchant, restored.notes) == (
        date(2026, 11, 5),
        Decimal("42.10"),
        "Grocer",
        "weekly",
    )
    assert restored.cleared_at is not None
    assert sorted((sp.category_id, sp.amount) for sp in restored.splits.all()) == sorted(
        [(s["groceries"].pk, Decimal("30.00")), (s["home_goods"].pk, Decimal("12.10"))]
    )
    assert account_balance(s["spending"]) == before
    assert AuditEvent.objects.filter(
        action="restore", record_id=str(restored.pk), changes__restored_from_deleted_id=expense.pk
    ).exists()
    assert undo.SESSION_KEY not in s["client"].session
    # A second undo does nothing.
    do_undo(s["client"])
    assert Expense.objects.filter(household=s["home"]).count() == 1


@pytest.mark.django_db
def test_undo_relinks_bill_month_and_refund(setup):
    s = setup
    bill = RecurringBill.objects.create(
        household=s["home"],
        name="Internet",
        default_amount=Decimal("102.80"),
        due_day=10,
        category=s["utilities"],
        account=s["bills"],
        effective_start=date(2026, 1, 1),
    )
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 11, 10), expected_amount=Decimal("102.80")
    )
    expense = create_expense(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 11, 9),
        account=s["bills"],
        category=s["utilities"],
        amount="102.80",
        merchant="Internet",
        bill=bill,
        occurrence=occurrence,
    )
    occurrence.refresh_from_db()
    assert occurrence.status == BillOccurrence.Status.PAID
    refund = create_refund(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 11, 12),
        account=s["bills"],
        category=s["utilities"],
        amount="2.80",
        original_expense=expense,
    )

    quick_delete(s["client"], "expense", expense.pk)
    occurrence.refresh_from_db()
    refund.refresh_from_db()
    assert occurrence.status != BillOccurrence.Status.PAID
    assert refund.original_expense_id is None

    do_undo(s["client"])
    restored = Expense.objects.get(household=s["home"])
    occurrence.refresh_from_db()
    refund.refresh_from_db()
    assert restored.bill_occurrence_id == occurrence.pk
    assert occurrence.status == BillOccurrence.Status.PAID
    assert refund.original_expense_id == restored.pk


@pytest.mark.django_db
def test_undo_income_keeps_allocations_and_paycheck(setup):
    s = setup
    income = create_income(
        household=s["home"],
        actor=s["user"],
        income_date=date(2026, 11, 13),
        source="Employer",
        amount="1000.00",
        allocations=[(s["bills"], "700.00"), (s["spending"], "300.00")],
        is_paycheck=True,
    )
    quick_delete(s["client"], "income", income.pk)
    assert not Income.objects.filter(household=s["home"]).exists()
    do_undo(s["client"])
    restored = Income.objects.get(household=s["home"])
    assert restored.is_paycheck and restored.source == "Employer"
    assert sorted((a.account_id, a.amount) for a in restored.allocations.all()) == sorted(
        [(s["bills"].pk, Decimal("700.00")), (s["spending"].pk, Decimal("300.00"))]
    )
    assert account_balance(s["bills"]) == Decimal("700.00")
    # The anchored period starts on the paycheck again.
    assert PayPeriod.objects.get(nominal_payday=date(2026, 11, 15)).start_date == date(2026, 11, 13)


@pytest.mark.django_db
def test_undo_transfer_keeps_cleared_flags(setup):
    s = setup
    transfer = create_transfer(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 11, 3),
        source=s["bills"],
        destination=s["spending"],
        amount="50.00",
    )
    Transfer.objects.filter(pk=transfer.pk).update(source_cleared_at=timezone.now())
    quick_delete(s["client"], "transfer", transfer.pk)
    do_undo(s["client"])
    restored = Transfer.objects.get(household=s["home"])
    assert restored.source_cleared_at is not None
    assert restored.destination_cleared_at is None
    assert account_balance(s["spending"]) == Decimal("50.00")


@pytest.mark.django_db
def test_savings_rows_keep_the_confirmation_page(setup):
    s = setup
    income = create_income(
        household=s["home"],
        actor=s["user"],
        income_date=date(2026, 11, 3),
        source="Gift",
        amount="20.00",
        allocations=[(s["savings"], "20.00")],
    )
    response = quick_delete(s["client"], "income", income.pk)
    assert response.status_code == 302
    assert response["Location"] == reverse("transaction_delete", args=["income", income.pk])
    assert Income.objects.filter(pk=income.pk).exists()
    page = s["client"].get(reverse("transactions"))
    assert b'name="quick"' in page.content  # the button is offered; the view decides


@pytest.mark.django_db
def test_undo_expires(setup):
    s = setup
    expense = create_expense(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 11, 5),
        account=s["spending"],
        category=s["groceries"],
        amount="5.00",
    )
    quick_delete(s["client"], "expense", expense.pk)
    session = s["client"].session
    data = session[undo.SESSION_KEY]
    data["deleted_at"] = (timezone.now() - undo.UNDO_WINDOW - timedelta(seconds=1)).isoformat()
    session[undo.SESSION_KEY] = data
    session.save()
    assert b"Undo</button>" not in s["client"].get(reverse("transactions")).content
    response = do_undo(s["client"])
    assert b"nothing to undo" in response.content
    assert not Expense.objects.filter(household=s["home"]).exists()


@pytest.mark.django_db
def test_undo_refused_when_period_locked_meanwhile(setup):
    s = setup
    expense = create_expense(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 11, 5),
        account=s["spending"],
        category=s["groceries"],
        amount="5.00",
    )
    quick_delete(s["client"], "expense", expense.pk)
    PayPeriod.objects.filter(pk=s["period"].pk).update(is_locked=True)
    response = do_undo(s["client"])
    assert b"Could not restore" in response.content
    assert not Expense.objects.filter(household=s["home"]).exists()


@pytest.mark.django_db
def test_undo_ignores_snapshot_from_another_profile(setup):
    s = setup
    other = Household.objects.create()
    HouseholdMembership.objects.create(household=other, user=s["user"])
    expense = create_expense(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 11, 5),
        account=s["spending"],
        category=s["groceries"],
        amount="5.00",
    )
    quick_delete(s["client"], "expense", expense.pk)
    session = s["client"].session
    session["active_household_id"] = other.pk
    session.save()
    do_undo(s["client"])
    assert not Expense.objects.filter(household=s["home"]).exists()
    assert not Expense.objects.filter(household=other).exists()


@pytest.mark.django_db
def test_quick_delete_rejects_foreign_next(setup):
    s = setup
    expense = create_expense(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 11, 5),
        account=s["spending"],
        category=s["groceries"],
        amount="5.00",
    )
    response = s["client"].post(
        reverse("transaction_delete", args=["expense", expense.pk]),
        {"quick": "1", "next": "https://evil.example/transactions/"},
    )
    assert response["Location"] == reverse("transactions")


@pytest.mark.django_db
def test_refund_quick_delete_and_undo(setup):
    s = setup
    refund = create_refund(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 11, 6),
        account=s["spending"],
        category=s["groceries"],
        amount="3.00",
        merchant="Hypermart",
    )
    quick_delete(s["client"], "refund", refund.pk)
    assert not Refund.objects.filter(household=s["home"]).exists()
    do_undo(s["client"])
    assert Refund.objects.get(household=s["home"]).merchant == "Hypermart"
