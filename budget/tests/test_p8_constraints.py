"""P8: database constraints, the audit index, debt history protection, analysis-run failures."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import ProtectedError

from budget.analysis_jobs import _work
from budget.forms import PayPeriodForm
from budget.models import (
    Account,
    BalanceAdjustment,
    Category,
    Debt,
    DebtBalance,
    Expense,
    Household,
    Income,
    IncomeAllocation,
    PayPeriod,
    PayPeriodBudget,
    ReportAnalysisRun,
    SavingsGoal,
    SavingsGoalEntry,
)


@pytest.fixture
def home(db):
    return Household.objects.create()


def test_statement_ending_is_unique_per_household_but_blank_may_repeat(home):
    other = Household.objects.create()
    Account.objects.create(household=home, name="A", statement_suffix="")
    Account.objects.create(household=home, name="B", statement_suffix="")  # blanks may repeat
    Account.objects.create(household=home, name="C", statement_suffix="1234")
    Account.objects.create(household=other, name="C", statement_suffix="1234")  # other household
    with pytest.raises(IntegrityError), transaction.atomic():
        Account.objects.create(household=home, name="D", statement_suffix="1234")


@pytest.fixture
def source_rows(home):
    account = Account.objects.create(household=home, name="Spending")
    category = Category.objects.create(household=home, name="Food", type="spending")
    goal = SavingsGoal.objects.create(household=home, name="Goal", target_amount="100.00")
    income = Income.objects.create(
        household=home, date=date(2026, 8, 1), amount="10.00", source="Pay"
    )
    allocation = IncomeAllocation.objects.create(income=income, account=account, amount="10.00")
    expense = Expense.objects.create(
        household=home, date=date(2026, 8, 1), amount="5.00", account=account, category=category
    )
    adjustment = BalanceAdjustment.objects.create(
        household=home, date=date(2026, 8, 1), amount="1.00", account=account, is_increase=True
    )
    return goal, allocation, expense, adjustment


def entry(goal, **links):
    return SavingsGoalEntry(goal=goal, amount=Decimal("1.00"), date=date(2026, 8, 1), **links)


def test_savings_entry_allows_one_source_or_none(source_rows):
    goal, allocation, expense, adjustment = source_rows
    entry(goal).save()
    entry(goal, income_allocation=allocation).save()
    entry(goal, expense=expense).save()
    entry(goal, adjustment=adjustment).save()
    assert SavingsGoalEntry.objects.count() == 4


def test_savings_entry_with_two_sources_is_refused(source_rows):
    goal, allocation, expense, adjustment = source_rows
    with pytest.raises(IntegrityError), transaction.atomic():
        entry(goal, income_allocation=allocation, expense=expense).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        entry(goal, expense=expense, adjustment=adjustment).save()


@pytest.mark.parametrize(
    "field",
    ["weekly_target", "spending_target", "expected_paycheck", "bills_target", "bills_cushion"],
)
def test_period_budget_money_cannot_be_negative(home, field):
    period = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        PayPeriodBudget.objects.create(pay_period=period, **{field: Decimal("-0.01")})
    PayPeriodBudget.objects.create(pay_period=period, **{field: Decimal("0.00")})


@pytest.mark.django_db
def test_audit_event_has_a_household_time_index():
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, "budget_auditevent")
    assert any(
        info["index"] and info["columns"] == ["household_id", "occurred_at"]
        for info in constraints.values()
    )


def test_pay_period_form_still_validates_on_sqlite(home):
    """The exclusion constraint is Postgres only; the app's own form check is what runs here."""
    PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
    )
    form = PayPeriodForm(
        {
            "nominal_payday": "2026-08-15",
            "expected_deposit_date": "2026-08-15",
            "start_date": "2026-08-15",
            "end_date": "2026-08-28",
        },
        household=home,
    )
    assert form.is_valid(), form.errors


def test_a_debt_with_balance_history_cannot_be_deleted(home):
    debt = Debt.objects.create(household=home, name="Car", apr="5.000", minimum_payment="100.00")
    DebtBalance.objects.create(debt=debt, as_of=date(2026, 8, 1), balance="9000.00")
    with pytest.raises(ProtectedError):
        debt.delete()
    assert DebtBalance.objects.count() == 1
    DebtBalance.objects.all().delete()
    debt.delete()  # no history: allowed


@pytest.fixture
def run(home):
    return ReportAnalysisRun.objects.create(
        household=home, start=date(2026, 8, 1), end=date(2026, 8, 31)
    )


def test_database_error_on_final_save_marks_the_run_failed(run):
    calls = []

    def failing_save(self, *args, **kwargs):
        calls.append(1)
        raise OperationalError("injected")

    with (
        patch("budget.outlook.outlook_data", side_effect=RuntimeError("model unavailable")),
        patch.object(ReportAnalysisRun, "save", failing_save),
    ):
        _work(run.pk)
    assert calls  # the save was attempted and failed
    run.refresh_from_db()
    assert run.status == ReportAnalysisRun.Status.FAILED
    assert run.finished_at is not None


def test_database_error_on_first_read_marks_the_run_failed(run):
    with patch.object(
        ReportAnalysisRun.objects, "select_related", side_effect=OperationalError("injected")
    ):
        _work(run.pk)
    run.refresh_from_db()
    assert run.status == ReportAnalysisRun.Status.FAILED
    assert run.finished_at is not None
