"""Debt balances and payoff projections.

Balances come only from what the owner enters from lender statements (``DebtBalance``).
Between statements the app shows a labelled estimate:

    estimated balance = last entered balance
                        + simple interest since then (balance × APR ÷ 12 × whole months)
                        − payments recorded since then (expenses paying the debt's bills)

Projections compound monthly: each month interest = balance × APR ÷ 12 (rounded to the
cent), then the monthly payment is applied. The monthly payment is the sum of the linked
active bills' amounts, or the minimum payment when no bill is linked. A payment that does
not exceed the first month's interest never pays the debt off, and the projection says so.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Sum

from .models import Debt, DebtBalance, Expense

CENT = Decimal("0.01")
MAX_MONTHS = 600  # 50 years: anything longer is reported as "not paid off"


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def monthly_rate(apr: Decimal) -> Decimal:
    return Decimal(apr) / Decimal(1200)


def add_months(start: date, months: int) -> date:
    index = start.year * 12 + start.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    days = [
        31,
        29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][month - 1]
    return date(year, month, min(start.day, days))


def whole_months_between(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return max(months, 0)


def monthly_payment(debt: Debt) -> tuple[Decimal, str]:
    """(payment, basis) — linked active bills' amounts, else the minimum payment."""
    bills = [bill for bill in debt.bills.all() if bill.active and bill.effective_end is None]
    if bills:
        total = sum((bill.default_amount for bill in bills), Decimal("0.00"))
        names = ", ".join(bill.name for bill in bills)
        return _cents(total), f"bills: {names}"
    return _cents(debt.minimum_payment), "minimum payment"


def payments_since(debt: Debt, after: date, through: date) -> Decimal:
    """Expenses paying this debt's bills dated after ``after`` up to ``through``."""
    total = Expense.objects.filter(
        bill_occurrence__bill__debt=debt, date__gt=after, date__lte=through
    ).aggregate(v=Sum("amount"))["v"]
    return _cents(total or Decimal("0.00"))


@dataclass
class BalanceEstimate:
    entered: DebtBalance | None
    months: int = 0
    interest: Decimal = Decimal("0.00")
    payments: Decimal = Decimal("0.00")
    estimate: Decimal | None = None

    @property
    def formula(self) -> str:
        if self.entered is None:
            return "Enter a balance from the lender's statement to start."
        return (
            f"${self.entered.balance:,.2f} entered {self.entered.as_of:%b %-d, %Y}"
            f" + ${self.interest:,.2f} interest ({self.months} month"
            f"{'s' if self.months != 1 else ''})"
            f" − ${self.payments:,.2f} paid since = ${self.estimate:,.2f}"
        )


def estimated_balance(debt: Debt, today: date) -> BalanceEstimate:
    entered = debt.balances.filter(as_of__lte=today).order_by("-as_of", "-pk").first()
    if entered is None:
        return BalanceEstimate(None)
    months = whole_months_between(entered.as_of, today)
    interest = _cents(entered.balance * monthly_rate(debt.apr) * months)
    paid = payments_since(debt, entered.as_of, today)
    estimate = max(_cents(entered.balance + interest - paid), Decimal("0.00"))
    return BalanceEstimate(entered, months, interest, paid, estimate)


@dataclass
class Projection:
    months: int | None  # None: never paid off at this payment
    payoff: date | None
    total_interest: Decimal
    total_paid: Decimal
    first_month_interest: Decimal


def project(balance: Decimal, apr: Decimal, payment: Decimal, start: date) -> Projection:
    """Month-by-month payoff of one debt at a fixed payment."""
    rate = monthly_rate(apr)
    remaining = _cents(Decimal(balance))
    first_interest = _cents(remaining * rate)
    if remaining <= 0:
        return Projection(0, start, Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
    if payment <= first_interest:
        return Projection(None, None, Decimal("0.00"), Decimal("0.00"), first_interest)
    interest_total = paid_total = Decimal("0.00")
    for month in range(1, MAX_MONTHS + 1):
        interest = _cents(remaining * rate)
        due = remaining + interest
        pay = min(payment, due)
        remaining = due - pay
        interest_total += interest
        paid_total += pay
        if remaining <= 0:
            return Projection(
                month, add_months(start, month), interest_total, paid_total, first_interest
            )
    return Projection(None, None, interest_total, paid_total, first_interest)


@dataclass
class PlanDebt:
    debt: Debt
    balance: Decimal
    payment: Decimal
    payoff_month: int | None = None
    interest: Decimal = Decimal("0.00")


@dataclass
class Plan:
    extra: Decimal
    debts: list[PlanDebt] = field(default_factory=list)
    months: int | None = None
    total_interest: Decimal = Decimal("0.00")

    def payoff_date(self, start: date) -> date | None:
        return add_months(start, self.months) if self.months is not None else None


def plan(
    items: list[tuple[Debt, Decimal, Decimal]],
    extra: Decimal = Decimal("0"),
    rollover: bool = True,
) -> Plan:
    """Pay every debt its payment each month; ``extra`` goes to the highest-APR debt still
    owed (ties: smaller balance). With ``rollover``, a paid-off debt's payment joins the
    extra ("avalanche"). ``rollover=False, extra=0`` is the baseline: every debt paid on its
    own schedule.

    ``items`` is (debt, starting balance, monthly payment).
    """
    rows = [PlanDebt(debt, _cents(balance), _cents(payment)) for debt, balance, payment in items]
    result = Plan(_cents(Decimal(extra)), rows)
    owed = [row for row in rows if row.balance > 0]
    for row in rows:
        if row.balance <= 0:
            row.payoff_month = 0
    month = 0
    while owed and month < MAX_MONTHS:
        month += 1
        # Only debts paid off during the plan free a payment; one already at zero has none.
        freed = sum((row.payment for row in rows if row.payoff_month), Decimal("0.00"))
        pool = result.extra + (freed if rollover else Decimal("0.00"))
        for row in owed:
            interest = _cents(row.balance * monthly_rate(row.debt.apr))
            row.interest += interest
            row.balance += interest
            pay = min(row.payment, row.balance)
            row.balance -= pay
            if rollover:
                pool += row.payment - pay  # the final month's unused payment moves on
        for row in sorted(owed, key=lambda r: (-r.debt.apr, r.balance, r.debt.pk)):
            if pool <= 0:
                break
            if row.balance <= 0:
                continue
            pay = min(pool, row.balance)
            row.balance -= pay
            pool -= pay
        for row in owed:
            if row.balance <= 0:
                row.payoff_month = month
        owed = [row for row in owed if row.payoff_month is None]
    result.total_interest = sum((row.interest for row in rows), Decimal("0.00"))
    result.months = None if owed else month
    return result
