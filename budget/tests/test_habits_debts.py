from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from budget.debts import estimated_balance, monthly_payment, plan, project
from budget.habits import (
    build_report,
    category_budget_status,
    cleanup_queue,
    set_category_budget,
    unify_merchant,
)
from budget.models import (
    Account,
    BillOccurrence,
    Category,
    CategoryBudget,
    Debt,
    DebtBalance,
    Expense,
    HabitView,
    Household,
    HouseholdMembership,
    PayPeriod,
    RecurringBill,
)
from budget.services import create_expense, create_refund

D = Decimal


# --- Debt projections ------------------------------------------------------------------


def test_textbook_loan_pays_off_in_sixty_months():
    result = project(D("10000.00"), D("6"), D("193.33"), date(2026, 1, 1))
    assert result.months == 60
    assert result.payoff == date(2031, 1, 1)
    assert D("1595") < result.total_interest < D("1605")
    assert result.total_paid == D("10000.00") + result.total_interest


def test_payment_below_interest_never_pays_off():
    result = project(D("10000.00"), D("24"), D("200.00"), date(2026, 1, 1))
    assert result.months is None and result.first_month_interest == D("200.00")


def test_zero_rate_and_zero_balance():
    assert project(D("300.00"), D("0"), D("100.00"), date(2026, 1, 31)).months == 3
    assert project(D("0"), D("5"), D("10"), date(2026, 1, 1)).months == 0


def _debt(home, name, apr, minimum="25.00"):
    return Debt.objects.create(household=home, name=name, apr=D(apr), minimum_payment=D(minimum))


@pytest.mark.django_db
def test_avalanche_extra_beats_baseline_and_targets_highest_rate():
    home = Household.objects.create()
    card = _debt(home, "Card", "24")
    car = _debt(home, "Car", "5")
    items = [(card, D("2000.00"), D("100.00")), (car, D("5000.00"), D("200.00"))]
    base = plan(items, rollover=False)
    assert (
        base.debts[0].payoff_month == project(D("2000"), D("24"), D("100"), date(2026, 1, 1)).months
    )
    assert (
        base.debts[1].payoff_month == project(D("5000"), D("5"), D("200"), date(2026, 1, 1)).months
    )
    faster = plan(items, extra=D("100.00"))
    assert faster.debts[0].payoff_month < base.debts[0].payoff_month
    assert faster.months < base.months
    assert faster.total_interest < base.total_interest


@pytest.fixture
def home(db):
    user = get_user_model().objects.create_user("hd-user", password="a-secure-password-123")
    household = Household.objects.create()
    HouseholdMembership.objects.create(household=household, user=user)
    bills = Account.objects.create(household=household, name="Bills", type="bills")
    spending = Account.objects.create(household=household, name="Spending", type="spending")
    loans = Category.objects.create(household=household, name="Loans", type="bill")
    return {"user": user, "home": household, "bills": bills, "spending": spending, "loans": loans}


@pytest.mark.django_db
def test_estimated_balance_formula_and_bill_payments(home):
    debt = _debt(home["home"], "Sedan", "12", "100.00")
    bill = RecurringBill.objects.create(
        household=home["home"],
        name="Sedan Loan",
        default_amount=D("150.00"),
        due_day=10,
        category=home["loans"],
        account=home["bills"],
        effective_start=date(2026, 1, 1),
        debt=debt,
    )
    assert monthly_payment(debt) == (D("150.00"), "bills: Sedan Loan")
    DebtBalance.objects.create(debt=debt, as_of=date(2026, 1, 15), balance=D("1000.00"))
    for month in (2, 3):
        occurrence = BillOccurrence.objects.create(
            bill=bill, due_date=date(2026, month, 10), expected_amount=D("150.00")
        )
        create_expense(
            household=home["home"],
            actor=home["user"],
            entry_date=date(2026, month, 10),
            account=home["bills"],
            category=home["loans"],
            amount="150.00",
            bill=bill,
            occurrence=occurrence,
        )
    estimate = estimated_balance(debt, date(2026, 3, 20))
    assert (estimate.months, estimate.interest, estimate.payments, estimate.estimate) == (
        2,
        D("20.00"),
        D("300.00"),
        D("720.00"),
    )
    assert "= $720.00" in estimate.formula
    bill.active = False
    bill.save()
    assert monthly_payment(debt) == (D("100.00"), "minimum payment")
    assert estimated_balance(_debt(home["home"], "New", "5"), date(2026, 3, 1)).estimate is None


# --- Habits ----------------------------------------------------------------------------


@pytest.fixture
def shop(home):
    h = home["home"]
    food = Category.objects.create(
        household=h, name="Groceries", type="spending", is_essential=True
    )
    fun = Category.objects.create(
        household=h, name="Eating Out", type="spending", is_essential=False
    )
    return {**home, "food": food, "fun": fun}


def buy(s, day, amount, merchant, category=None, splits=None, month=10):
    return create_expense(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, month, day),
        account=s["spending"],
        category=category or s["fun"],
        amount=amount,
        merchant=merchant,
        splits=splits,
    )


def view(s, **kwargs):
    defaults = {"household": s["home"], "name": "v", "basis": HabitView.Basis.MONTH, "periods": 2}
    defaults.update(kwargs)
    return HabitView.objects.create(**defaults)


@pytest.mark.django_db
def test_small_purchases_habits_and_trend(shop):
    s = shop
    for day in (1, 8, 15):
        buy(s, day, "4.50", "Snackbar")
    buy(s, 2, "4.50", "Snackbar")  # same day/amount twice is still two purchases
    buy(s, 2, "4.50", "  snackbar ")
    buy(s, 3, "60.00", "Grocer", s["food"])
    buy(
        s,
        4,
        "18.00",
        "Hypermart",
        s["food"],
        splits=[(s["food"], D("10.00")), (s["fun"], D("8.00"))],
    )
    buy(s, 5, "9.00", "Snackbar", month=9)
    create_refund(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 10, 9),
        account=s["spending"],
        category=s["fun"],
        amount="4.50",
        merchant="Snackbar",
    )
    report = build_report(view(s), date(2026, 10, 20))
    assert [c.label for c in report.columns] == ["Sep 2026", "Oct 2026"]
    assert report.total == D("9.00") + D("22.50") + D("60.00") + D("18.00") - D("4.50")
    assert report.small_count == 7  # 6 Snackbar + 1 split Hypermart purchase
    snackbar = next(r for r in report.small_merchants if r.name == "Snackbar")
    assert (snackbar.count, snackbar.total) == (6, D("31.50"))
    hypermart = next(r for r in report.merchants if r.name == "Hypermart")
    assert hypermart.count == 1 and hypermart.total == D("18.00")
    habit = next(h for h in report.habits if h.merchant.name == "Snackbar")
    assert habit.per_month == (D("31.50") * D("30.44") / D("50")).quantize(D("0.01"))  # to Oct 20
    assert "Hypermart" not in {h.merchant.name for h in report.habits}
    rows = {row.category.name: row.amounts for row in report.trend}
    assert rows["Eating Out"] == [D("9.00"), D("22.50") + D("8.00") - D("4.50")]
    assert rows["Groceries"] == [D("0.00"), D("70.00")]
    assert sum(report.column_totals) == report.total


@pytest.mark.django_db
def test_view_filters(shop):
    s = shop
    buy(s, 1, "5.00", "Kiosk")
    buy(s, 2, "6.00", "Market", s["food"])
    essential_off = build_report(view(s, include_essential=False), date(2026, 10, 20))
    assert essential_off.total == D("5.00")
    only_food = view(s, name="food")
    only_food.categories.add(s["food"])
    assert build_report(only_food, date(2026, 10, 20)).total == D("6.00")
    expense = Expense.objects.get(merchant="Market")
    expense.is_essential_override = False
    expense.save()
    assert build_report(view(s, name="o", include_essential=False), date(2026, 10, 20)).total == D(
        "11.00"
    )


@pytest.mark.django_db
def test_pay_period_columns(shop):
    s = shop
    for start, end in (
        (date(2026, 9, 28), date(2026, 10, 13)),
        (date(2026, 10, 14), date(2026, 10, 27)),
    ):
        PayPeriod.objects.create(
            household=s["home"],
            start_date=start,
            end_date=end,
            nominal_payday=start,
            expected_deposit_date=start,
        )
    buy(s, 13, "3.00", "A")
    buy(s, 14, "4.00", "B")
    report = build_report(view(s, basis=HabitView.Basis.PAY_PERIOD, periods=6), date(2026, 10, 20))
    assert len(report.columns) == 2 and report.column_totals == [D("3.00"), D("4.00")]


@pytest.mark.django_db
def test_cleanup_queue_and_unify_skips_locked(shop):
    s = shop
    PayPeriod.objects.create(
        household=s["home"],
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        nominal_payday=date(2026, 9, 1),
        expected_deposit_date=date(2026, 9, 1),
    )
    buy(s, 1, "5.00", "Snackbar")
    buy(s, 2, "5.00", "snackbar")
    buy(s, 3, "5.00", "SNACKBAR ")
    old = buy(s, 5, "5.00", "snackbar", month=9)
    buy(s, 4, "5.00", "")
    PayPeriod.objects.update(is_locked=True)
    blank, groups = cleanup_queue(s["home"])
    assert [e.amount for e in blank] == [D("5.00")]
    group = next(g for g in groups if g.key == "snackbar")
    assert group.suggested == "snackbar"
    changed, skipped = unify_merchant(s["home"], s["user"], "snackbar", "Snackbar")
    assert (changed, skipped) == (2, 1)
    old.refresh_from_db()
    assert old.merchant == "snackbar"
    with pytest.raises(ValidationError):
        unify_merchant(s["home"], s["user"], "snackbar", "Fuelco")


@pytest.mark.django_db
def test_category_budget_history_and_status(shop):
    s = shop
    period = PayPeriod.objects.create(
        household=s["home"],
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 14),
        nominal_payday=date(2026, 10, 1),
        expected_deposit_date=date(2026, 10, 1),
    )
    set_category_budget(s["home"], s["user"], s["fun"], "50.00", date(2026, 9, 1))
    set_category_budget(s["home"], s["user"], s["fun"], "40.00", date(2026, 10, 1))
    first = CategoryBudget.objects.get(effective_from=date(2026, 9, 1))
    assert first.effective_to == date(2026, 9, 30) and first.amount == D("50.00")
    with pytest.raises(ValidationError, match="later budget"):
        set_category_budget(s["home"], s["user"], s["fun"], "10.00", date(2026, 9, 15))
    with pytest.raises(ValidationError):
        set_category_budget(s["home"], s["user"], s["loans"], "10.00", date(2026, 10, 1))
    buy(s, 2, "45.50", "Diner")
    (line,) = category_budget_status(s["home"], period)
    assert (line.budget, line.spent, line.remaining, line.percent) == (
        D("40.00"),
        D("45.50"),
        D("-5.50"),
        114,
    )


@pytest.mark.django_db
def test_debt_already_at_zero_frees_no_payment():
    home = Household.objects.create()
    done = _debt(home, "Done", "10")
    card = _debt(home, "Card", "20")
    with_zero = plan([(done, D("0"), D("500.00")), (card, D("1000.00"), D("100.00"))])
    alone = plan([(card, D("1000.00"), D("100.00"))])
    assert with_zero.debts[0].payoff_month == 0
    assert with_zero.debts[1].payoff_month == alone.debts[0].payoff_month
