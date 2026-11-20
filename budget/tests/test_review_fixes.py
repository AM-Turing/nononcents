"""Money tests for review fixes F1, F2 and F5 (paid bills, one-cent amounts, income edits)."""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse

from budget.forecast import payday_forecast
from budget.forms import (
    ExpenseSplitForm,
    HistoryTransactionEditForm,
    IncomeForm,
    MoneyDateMixin,
    RefundForm,
    SavingsMoveForm,
    SavingsSourceAllocationForm,
    TransferForm,
)
from budget.habit_forms import AdjustForm
from budget.models import (
    Account,
    BillOccurrence,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    Income,
    IncomeAllocation,
    StatementLine,
)
from budget.money import MoneyField
from budget.outlook import outlook_data
from budget.services import bill_forecast, create_income
from budget.statements import ParsedLine, ParsedSection, confirm_match, stage_statement
from budget.tests.test_forecast import TODAY, make_bill, make_period, occurrence

D = Decimal


# --- F1: a paid bill is settled at what was paid ------------------------------------------


@pytest.fixture
def paid_bill(db):
    """Electricity: three paid months (120, 130, 140) then a Nov 20 bill that is PAID at 100."""
    user = get_user_model().objects.create_user("f1-user", password="safe-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    bills = Account.objects.create(household=home, name="Bills", type=Account.Type.BILLS)
    utilities = Category.objects.create(household=home, name="Utilities", type="bill")
    make_period(home, date(2026, 11, 10), date(2026, 11, 29), expected_paycheck="1000.00")
    make_period(home, date(2026, 11, 30), date(2026, 12, 14), expected_paycheck="1000.00")
    electric = make_bill(home, "Electricity", "100.00", bills, utilities)

    def pay(due, amount, **extra):
        paid = occurrence(electric, due, "100.00", status=BillOccurrence.Status.PAID, **extra)
        Expense.objects.create(
            household=home,
            created_by=user,
            date=due,
            amount=D(amount),
            account=bills,
            category=utilities,
            bill_occurrence=paid,
        )
        return paid

    for due, amount in (
        (date(2026, 8, 15), "120.00"),
        (date(2026, 9, 15), "130.00"),
        (date(2026, 10, 15), "140.00"),
    ):
        pay(due, amount)
    return {"home": home, "pay": pay}


@pytest.mark.django_db
def test_paid_bill_settles_at_amount_paid_not_the_recent_average(paid_bill):
    nov = paid_bill["pay"](date(2026, 11, 20), "100.00")

    forecast = bill_forecast(nov)

    # avg(120,130,140)=130 > paid 100; the bill is settled at what was paid, 100.
    assert forecast["amount"] == D("100.00")
    assert forecast["paid"] == D("100.00")
    assert forecast["remaining"] == D("0.00")  # 100 - 100
    assert forecast["basis"] == "paid"


@pytest.mark.django_db
def test_paid_bill_adds_nothing_to_home_or_outlook_bills_remaining(paid_bill):
    paid_bill["pay"](date(2026, 11, 20), "100.00")
    home = paid_bill["home"]

    due = payday_forecast(home, TODAY)["due_this_period"]
    # Only bill in the period is settled: remaining 100 - 100 = 0 (before the fix 130 - 100 = 30).
    assert due["bills_total"] == D("0.00")
    assert due["spending_total"] == D("0.00")
    assert due["items"] == []

    data = outlook_data(home, date(2026, 11, 10), date(2026, 11, 29), TODAY)
    assert data["projected_bills_remaining"] == D("0.00")  # 100 - 100
    assert data["bill_rows"] == []


@pytest.mark.django_db
def test_bill_paid_more_than_expected_forecasts_at_the_paid_amount(paid_bill):
    # Expected 100, no confirmed amount, paid 150.
    nov = paid_bill["pay"](date(2026, 11, 20), "150.00")

    forecast = bill_forecast(nov)

    # avg(120,130,140)=130 is also below 150; the settled amount is what was paid, 150.
    assert forecast["amount"] == D("150.00")
    assert forecast["paid"] == D("150.00")
    assert forecast["remaining"] == D("0.00")  # max(150 - 150, 0)
    assert forecast["basis"] == "paid"


@pytest.mark.django_db
def test_paid_bill_forecast_reads_amount_paid_once(paid_bill, django_assert_num_queries):
    nov = paid_bill["pay"](date(2026, 11, 20), "100.00")

    with django_assert_num_queries(1):  # one aggregate for amount_paid, reused for amount and paid
        bill_forecast(nov)


# --- F2: exactly one cent is a valid amount -----------------------------------------------

ONE_CENT_FIELDS = [
    pytest.param(MoneyDateMixin.declared_fields["amount"], id="expense-refund-transfer-base"),
    pytest.param(RefundForm.declared_fields["amount"], id="refund"),
    pytest.param(TransferForm.declared_fields["amount"], id="transfer"),
    pytest.param(IncomeForm.base_fields["amount"], id="income"),
    pytest.param(ExpenseSplitForm.base_fields["amount"], id="split"),
    pytest.param(SavingsSourceAllocationForm.base_fields["amount"], id="savings-source"),
    pytest.param(SavingsMoveForm.base_fields["amount"], id="savings-move"),
    pytest.param(HistoryTransactionEditForm.base_fields["amount"], id="history-edit"),
    pytest.param(AdjustForm.base_fields["small_threshold"], id="habit-threshold"),
]


@pytest.mark.parametrize("field", ONE_CENT_FIELDS)
def test_one_cent_is_accepted_and_zero_is_refused(field):
    assert field.clean("0.01") == D("0.01")
    with pytest.raises(ValidationError):
        field.clean("0.00")


def test_money_field_stores_a_float_bound_as_decimal():
    field = MoneyField(min_value=0.01)

    assert field.min_value == D("0.01")
    assert isinstance(field.min_value, Decimal)
    assert field.clean("0.01") == D("0.01")


# --- F5: editing an income keeps or releases its statement matches ---------------------------


@pytest.fixture
def income_world(client, db):
    user = get_user_model().objects.create_user("f5-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    accounts = {
        name: Account.objects.create(household=home, name=name, type=kind)
        for name, kind in (
            ("Spending", "spending"),
            ("Bills", "bills"),
            ("Joint", "spending"),
        )
    }
    # 60 + 30 + 10 = 100.00
    income = create_income(
        household=home,
        actor=user,
        income_date=date(2026, 10, 1),
        source="Pay",
        amount=D("100.00"),
        allocations=[
            (accounts["Spending"], D("60.00")),
            (accounts["Bills"], D("30.00")),
            (accounts["Joint"], D("10.00")),
        ],
    )
    allocation = {item.account.name: item for item in income.allocations.select_related("account")}
    lines = {}
    for index, (name, amount) in enumerate(
        (("Spending", "60.00"), ("Bills", "30.00"), ("Joint", "10.00"))
    ):
        staged = stage_statement(
            household=home,
            actor=user,
            account=accounts[name],
            section=ParsedSection(
                account_label=name,
                period_start=date(2026, 10, 1),
                period_end=date(2026, 10, 31),
                lines=[ParsedLine(date(2026, 10, 1), D(amount), "DEPOSIT PAYROLL")],
            ),
            file_sha256=str(index) * 64,
        )
        line = StatementLine.objects.get(statement=staged.statement)
        confirm_match(
            household=home, actor=user, line_id=line.pk, key=f"allocation:{allocation[name].pk}"
        )
        lines[name] = line
    return {
        "client": client,
        "home": home,
        "income": income,
        "accounts": accounts,
        "allocation": allocation,
        "lines": lines,
    }


def edit_income(w, spending="60.00", bills="30.00", joint="10.00", notes="", total="100.00"):
    accounts = w["accounts"]
    return w["client"].post(
        reverse("transaction_edit", args=["income", w["income"].pk]),
        {
            "date": "2026-10-01",
            "description": "Pay",
            "amount": total,
            "notes": notes,
            f"allocation_{accounts['Spending'].pk}": spending,
            f"allocation_{accounts['Bills'].pk}": bills,
            f"allocation_{accounts['Joint'].pk}": joint,
        },
    )


def line_state(w, name):
    line = StatementLine.objects.get(pk=w["lines"][name].pk)
    return line.status, line.matched_key


def test_matched_fixture_is_matched_and_cleared(income_world):
    w = income_world
    for name, item in w["allocation"].items():
        assert line_state(w, name) == (StatementLine.Status.MATCHED, f"allocation:{item.pk}")
        assert IncomeAllocation.objects.get(pk=item.pk).cleared_at is not None


def test_notes_only_edit_keeps_allocations_and_matches(income_world):
    w = income_world
    before = {item.account.name: item.pk for item in w["income"].allocations.all()}
    cleared = {i.account.name: i.cleared_at for i in w["income"].allocations.all()}
    assert all(cleared.values())  # the confirmed matches marked every row cleared

    response = edit_income(w, notes="Bonus included")

    assert response.status_code == 302
    after = {item.account.name: item for item in Income.objects.get().allocations.all()}
    assert {name: item.pk for name, item in after.items()} == before  # same row ids
    assert sum(item.amount for item in after.values()) == D("100.00")  # 60 + 30 + 10
    for name, item in after.items():
        assert line_state(w, name) == (StatementLine.Status.MATCHED, f"allocation:{item.pk}")
        assert item.cleared_at == cleared[name]


def test_changing_one_allocation_releases_only_its_match(income_world):
    w = income_world
    # Spending 60 -> 65 and Joint 10 -> 5 keep the total at 65 + 30 + 5 = 100; Bills is unchanged.
    response = edit_income(w, spending="65.00", joint="5.00")

    assert response.status_code == 302
    after = {item.account.name: item for item in Income.objects.get().allocations.all()}
    assert after["Spending"].amount == D("65.00")
    assert after["Spending"].cleared_at is None
    assert line_state(w, "Spending") == (StatementLine.Status.PENDING, "")
    assert after["Joint"].amount == D("5.00")
    assert after["Joint"].cleared_at is None
    assert line_state(w, "Joint") == (StatementLine.Status.PENDING, "")
    assert after["Bills"].amount == D("30.00")  # unchanged: 30
    assert after["Bills"].cleared_at is not None
    assert line_state(w, "Bills") == (
        StatementLine.Status.MATCHED,
        f"allocation:{after['Bills'].pk}",
    )


def test_setting_an_allocation_to_zero_deletes_it_and_releases_its_line(income_world):
    w = income_world
    joint_pk = w["allocation"]["Joint"].pk
    # Joint 10 -> 0 (row removed); Spending 60 -> 70 keeps the total at 70 + 30 = 100.
    response = edit_income(w, spending="70.00", joint="0.00")

    assert response.status_code == 302
    assert not IncomeAllocation.objects.filter(pk=joint_pk).exists()
    assert line_state(w, "Joint") == (StatementLine.Status.PENDING, "")
    after = {item.account.name: item for item in Income.objects.get().allocations.all()}
    assert sorted(after) == ["Bills", "Spending"]
    assert after["Spending"].amount == D("70.00")
    assert line_state(w, "Spending") == (StatementLine.Status.PENDING, "")
    assert after["Bills"].amount == D("30.00")  # unchanged: 30
    assert line_state(w, "Bills") == (
        StatementLine.Status.MATCHED,
        f"allocation:{after['Bills'].pk}",
    )
