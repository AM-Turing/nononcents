"""Transparent "until next payday" forecast.

Everything here is a pure read. Every headline figure is returned as a dict with a label, an
amount, the formula in words, and the inputs that fed it, so templates can show their work.
Headline figures are never clamped: a negative number means "short by".
"""

from datetime import date, timedelta
from decimal import Decimal

from .models import Account, Income, PayPeriod, PayPeriodBudget
from .services import (
    account_balance,
    bill_forecast,
    live_bill_occurrences,
    money,
    net_spending,
)

ZERO = Decimal("0.00")
NEEDS_REVIEW_TEXT = "Older unpaid bills — mark paid or skip them on the Bills page"
RECENT_PERIODS = 3


def figure(label, amount, formula, inputs):
    return {"label": label, "amount": money(amount), "formula": formula, "inputs": inputs}


def item(label, amount, basis=""):
    return {"label": label, "amount": money(amount), "basis": basis}


def _day_before(value: date) -> date:
    return value - timedelta(days=1)


def _type_balance(household, account_type, today):
    return money(
        sum(
            (
                account_balance(account, today)
                for account in Account.objects.filter(household=household, type=account_type)
            ),
            ZERO,
        )
    )


def _bill_rows(occurrences):
    rows = []
    for occurrence in occurrences:
        forecast = bill_forecast(occurrence)
        if forecast["remaining"] <= 0:
            continue
        account = occurrence.effective_account
        rows.append(
            {
                "name": occurrence.bill.name,
                "due_date": occurrence.due_date,
                "remaining": forecast["remaining"],
                "basis": forecast["basis"],
                "account": account.name,
                "from_spending": account.type == Account.Type.SPENDING,
            }
        )
    return sorted(rows, key=lambda row: (row["due_date"], row["name"]))


def bills_in_window(household, start, end):
    """Unpaid live bills due start..end, split by the account that pays them."""
    occurrences = (
        live_bill_occurrences(household)
        .filter(due_date__range=(start, end))
        .select_related("bill", "account", "bill__account")
    )
    rows = _bill_rows(occurrences)
    spending = [row for row in rows if row["from_spending"]]
    bills = [row for row in rows if not row["from_spending"]]
    return {
        "items": rows,
        "spending_items": spending,
        "bills_items": bills,
        "total": money(sum((row["remaining"] for row in rows), ZERO)),
        "spending_total": money(sum((row["remaining"] for row in spending), ZERO)),
        "bills_total": money(sum((row["remaining"] for row in bills), ZERO)),
    }


def needs_review(household, before):
    """Unpaid live occurrences due before ``before``: reported, never counted."""
    occurrences = (
        live_bill_occurrences(household)
        .filter(due_date__lt=before)
        .select_related("bill", "account", "bill__account")
    )
    rows = _bill_rows(occurrences)
    return {
        "items": rows,
        "count": len(rows),
        "total": money(sum((row["remaining"] for row in rows), ZERO)),
        "message": NEEDS_REVIEW_TEXT,
    }


def expected_paycheck(household, period):
    """Expected paycheck for a future ``period``: budget, else last paycheck, else zero.

    A paycheck already recorded inside the period is the paycheck itself (counted once),
    never an expectation added on top of it.
    """
    recorded = (
        Income.objects.filter(
            household=household,
            is_paycheck=True,
            date__range=(period.start_date, period.end_date),
        )
        .order_by("date")
        .first()
    )
    if recorded:
        return money(recorded.amount), f"recorded paycheck {recorded.date}"
    budget = PayPeriodBudget.objects.filter(pay_period=period).first()
    if budget and budget.expected_paycheck > 0:
        return money(budget.expected_paycheck), "budget"
    last = Income.objects.filter(household=household, is_paycheck=True).order_by("-date").first()
    if last:
        return money(last.amount), f"last paycheck {last.date}"
    return ZERO, "no paycheck recorded"


def recent_completed_periods(household, today):
    return list(
        PayPeriod.objects.filter(household=household, end_date__lt=today).order_by("-end_date")[
            :RECENT_PERIODS
        ]
    )


def anticipated_period_spending(household, today, next_period):
    """Return (amount, basis, inputs) for next period's spending.

    The owner's spending target for the next pay period is used (new periods copy the
    previous target, so it is set once). One-off purchases therefore never change the
    estimate. Only when no target is set does the average of the last three completed
    periods stand in. The average is always listed for comparison.
    """
    recent = recent_completed_periods(household, today)
    history = [
        item(f"{p.start_date} to {p.end_date}", net_spending(household, p.start_date, p.end_date))
        for p in recent
    ]
    average = (
        money(sum((row["amount"] for row in history), ZERO) / len(history)) if history else None
    )
    budget = PayPeriodBudget.objects.filter(pay_period=next_period).first() if next_period else None
    if budget and budget.spending_target > 0:
        inputs = [item("Spending target for next period", money(budget.spending_target), "budget")]
        if average is not None:
            inputs.append(
                item(
                    f"For comparison only: average of last {len(history)} periods",
                    average,
                    "not used",
                )
            )
        return money(budget.spending_target), "next period spending target", inputs
    if average is not None:
        return average, f"average of last {len(history)} completed periods (no target set)", history
    return ZERO, "no target or history", []


def payday_forecast(household, today: date) -> dict:
    period = PayPeriod.objects.filter(
        household=household, start_date__lte=today, end_date__gte=today
    ).first()
    next_period = (
        PayPeriod.objects.filter(household=household, start_date__gt=period.end_date)
        .order_by("start_date")
        .first()
        if period
        else None
    )
    if period is None or next_period is None:
        result = {
            "available": False,
            "reason": "Add pay periods for the current and next pay cycle to see the forecast.",
            "period": period,
            "next_period": next_period,
        }
        if period is not None:
            # Bills coverage for the current period needs no next period.
            bills_balance = _type_balance(household, Account.Type.BILLS, today)
            due_now = bills_in_window(household, period.start_date, period.end_date)
            result["bills_surplus"] = _bills_surplus(bills_balance, due_now)
            result["needs_review"] = needs_review(household, period.start_date)
        return result
    after_next = (
        PayPeriod.objects.filter(household=household, start_date__gt=next_period.end_date)
        .order_by("start_date")
        .first()
    )
    payday = next_period.start_date
    days = max((payday - today).days, 1)
    spending_balance = _type_balance(household, Account.Type.SPENDING, today)
    bills_balance = _type_balance(household, Account.Type.BILLS, today)
    due_now = bills_in_window(household, period.start_date, _day_before(payday))
    next_end = _day_before(after_next.start_date) if after_next else next_period.end_date
    due_next = bills_in_window(household, next_period.start_date, next_end)
    paycheck, paycheck_basis = expected_paycheck(household, next_period)
    anticipated, anticipated_basis, anticipated_inputs = anticipated_period_spending(
        household, today, next_period
    )
    next_budget = PayPeriodBudget.objects.filter(pay_period=next_period).first()
    target = money(next_budget.spending_target) if next_budget else ZERO

    surplus_figure = _bills_surplus(bills_balance, due_now)
    bills_surplus = surplus_figure["amount"]
    hold_back = max(ZERO, due_next["total"] + anticipated - paycheck)
    uncovered = max(ZERO, hold_back - bills_surplus)
    safe = spending_balance - due_now["spending_total"] - uncovered
    next_covered = bills_surplus + paycheck - due_next["total"] - anticipated
    # The carry-over (hold_back) comes first from what Bills has left after this period's
    # bills, then from Spending. When Bills is short this period, Spending also covers that
    # shortfall, so held_for_next can exceed carry_from_spending by exactly the shortfall.
    carry_from_bills = min(hold_back, max(bills_surplus, ZERO))
    carry_from_spending = hold_back - carry_from_bills

    return {
        "available": True,
        "reason": "",
        "today": today,
        "period": period,
        "next_period": next_period,
        "payday": payday,
        "days_until_payday": days,
        "spending_balance": spending_balance,
        "bills_balance": bills_balance,
        "due_this_period": due_now,
        "next_period_bills": due_next,
        "needs_review": needs_review(household, period.start_date),
        "expected_paycheck": figure(
            "Expected next paycheck",
            paycheck,
            f"Basis: {paycheck_basis}",
            [item("Paycheck", paycheck, paycheck_basis)],
        ),
        "anticipated_spending": figure(
            "Anticipated next-period spending",
            anticipated,
            f"Basis: {anticipated_basis}",
            anticipated_inputs or [item("Spending", anticipated, anticipated_basis)],
        ),
        "spending_target": target,
        "anticipated_from_target": anticipated_basis == "next period spending target",
        "bills_surplus": surplus_figure,
        "hold_back": figure(
            "Hold back for next period",
            hold_back,
            "Next-period bills + anticipated spending - expected paycheck (not below zero)",
            [
                item("Next-period bills", due_next["total"]),
                item("Anticipated spending", anticipated, anticipated_basis),
                item("Expected paycheck", -paycheck, paycheck_basis),
            ],
        ),
        "safe_to_spend": figure(
            "Safe to spend until payday",
            safe,
            "Spending balance - bills due from Spending - hold-back not covered by Bills surplus",
            [
                item("Spending balance today", spending_balance),
                item("Bills due from Spending", -due_now["spending_total"]),
                item("Hold-back not covered by Bills surplus", -uncovered),
            ],
        ),
        "held_for_next": uncovered,
        "carry_over": {
            "needed": hold_back,
            "from_bills": carry_from_bills,
            "from_spending": carry_from_spending,
            "spare": max(ZERO, paycheck - due_next["total"] - anticipated),
            "bills_shortfall_from_spending": uncovered - carry_from_spending,
        },
        "per_day": figure(
            "Per day until payday",
            safe / Decimal(days),
            f"Safe to spend / {days} day{'s' if days != 1 else ''} until {payday}",
            [item("Safe to spend", safe), item("Days until payday", Decimal(days))],
        ),
        "next_bills_covered": figure(
            "Next period covered",
            next_covered,
            "Bills surplus + expected paycheck - next-period bills - anticipated spending",
            [
                item("Bills surplus", bills_surplus),
                item("Expected paycheck", paycheck, paycheck_basis),
                item("Next-period bills", -due_next["total"]),
                item("Anticipated spending", -anticipated, anticipated_basis),
            ],
        ),
    }


def _bills_surplus(bills_balance, due_now):
    return figure(
        "Bills covered this period",
        bills_balance - due_now["bills_total"],
        "Bills balance - unpaid bills due from the Bills account before payday",
        [
            item("Bills balance today", bills_balance),
            item("Bills due before payday", -due_now["bills_total"], "unpaid, live bills"),
        ],
    )
