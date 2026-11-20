import math
from calendar import monthrange
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db.models import Sum

from .forecast import expected_paycheck, figure, item, recent_completed_periods
from .models import (
    Account,
    ApplicationSetting,
    Category,
    Expense,
    Income,
    PayPeriod,
    PayPeriodBudget,
    Refund,
    SavingsGoal,
    SavingsGoalEntry,
)
from .services import (
    account_balance,
    bill_forecast,
    expected_deposit_date,
    live_bill_occurrences,
    money,
    net_spending,
    savings_account,
)


def _add_months(value, count):
    month_index = value.year * 12 + value.month - 1 + count
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return value.replace(year=year, month=month, day=min(value.day, monthrange(year, month)[1]))


def default_outlook_range(household, today):
    completed = (
        PayPeriod.objects.filter(household=household, end_date__lt=today)
        .order_by("-end_date")
        .first()
    )
    if not completed:
        start = expected_deposit_date(today.replace(day=1))
        return start, expected_deposit_date(_add_months(today.replace(day=1), 1)) - timedelta(
            days=1
        )
    start = completed.actual_deposit_date or completed.expected_deposit_date or completed.start_date
    following = list(
        PayPeriod.objects.filter(household=household, start_date__gt=completed.start_date).order_by(
            "start_date"
        )[:2]
    )
    if len(following) == 2:
        boundary = (
            following[1].actual_deposit_date
            or following[1].expected_deposit_date
            or following[1].start_date
        )
    else:
        boundary = expected_deposit_date(_add_months(completed.nominal_payday, 1))
    return start, boundary - timedelta(days=1)


def _sum(queryset):
    return queryset.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def _income_by_date(home, start, end):
    if end < start:
        return []
    return [
        {"date": row["date"], "amount": money(row["total"])}
        for row in Income.objects.filter(household=home, date__range=(start, end))
        .values("date")
        .annotate(total=Sum("amount"))
        .order_by("date")
    ]


def _paycheck_rows(home, start, end, actual_end):
    """Recorded paychecks in start..end; each is one real deposit, counted once."""
    return [
        {
            "deposit_date": income.date,
            "date_source": "recorded",
            "amount": money(income.amount),
            "timing": "elapsed" if income.date <= actual_end else "future",
        }
        for income in Income.objects.filter(
            household=home, is_paycheck=True, date__range=(start, end)
        ).order_by("date")
    ]


def _expected_paycheck_rows(home, actual_end, end):
    """Paychecks still to come: one per period starting in (actual_end, end].

    Pay periods start on the day their paycheck lands, so a period with no paycheck yet is an
    expected deposit on its start date. A period whose paycheck is already recorded inside it
    contributes that recorded amount instead of a second, expected one.
    """
    rows = []
    for period in PayPeriod.objects.filter(
        household=home, start_date__gt=actual_end, start_date__lte=end
    ).order_by("start_date"):
        amount, basis = expected_paycheck(home, period)
        rows.append(
            {
                "deposit_date": period.start_date,
                "date_source": "expected",
                "amount": amount,
                "basis": basis,
                "timing": "future",
            }
        )
    return rows


def _balance_total(home, through):
    return money(
        sum(
            (
                account_balance(account, through)
                for account in Account.objects.filter(
                    household=home, type__in=[Account.Type.SPENDING, Account.Type.BILLS]
                )
            ),
            Decimal("0.00"),
        )
    )


def _expense_parts(home, start, end) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    expenses = (
        Expense.objects.filter(household=home, date__range=(start, end))
        .select_related("category")
        .prefetch_related("splits__category")
    )
    for expense in expenses:
        splits = list(expense.splits.all())
        if splits:
            parts: list[Any] = list(splits)
        else:
            parts = [expense]
        for part in parts:
            category = part.category
            essential = (
                expense.is_essential_override
                if expense.is_essential_override is not None
                else category.is_essential
            )
            rows.append(
                {
                    "date": expense.date,
                    "category": category,
                    "merchant": expense.merchant.strip() or "Unspecified merchant",
                    "amount": part.amount,
                    "essential": essential,
                }
            )
    for refund in Refund.objects.filter(household=home, date__range=(start, end)).select_related(
        "category"
    ):
        rows.append(
            {
                "date": refund.date,
                "category": refund.category,
                "merchant": refund.merchant.strip() or "Refund",
                "amount": -refund.amount,
                "essential": refund.category.is_essential,
            }
        )
    return rows


def _budget_target(home, start, end):
    target = Decimal("0.00")
    budgets = PayPeriodBudget.objects.filter(
        pay_period__household=home,
        pay_period__start_date__lte=end,
        pay_period__end_date__gte=start,
    ).select_related("pay_period")
    for budget in budgets:
        period = budget.pay_period
        overlap_start = max(start, period.start_date)
        overlap_end = min(end, period.end_date)
        overlap_days = Decimal((overlap_end - overlap_start).days + 1)
        period_days = Decimal((period.end_date - period.start_date).days + 1)
        target += budget.spending_target * overlap_days / period_days
    return money(target)


def _chart_points(values, width=760, height=220, peak=None):
    return " ".join(
        f"{row['x']},{row['y']}" for row in _chart_point_rows(values, width, height, peak)
    )


CHART_HEADROOM = Decimal("1.10")


def chart_scale_max(values, peak=None):
    """Top of a chart's scale: the largest plotted value (or shared peak) plus headroom.

    Never smaller than any plotted value, so no point or bar is cut off at the top.
    """
    largest = max([*values, peak or Decimal("0.00")])
    if largest <= 0:
        return Decimal("1.00")
    return largest * CHART_HEADROOM


def pie_slices(rows, total, cx=100.0, cy=100.0, radius=96.0):
    """SVG path data for each category with an amount, clockwise from 12 o'clock.

    Geometry only (floats); the amounts and shares shown come from the Decimal rows.
    """
    if not total:
        return []
    slices = []
    angle = -math.pi / 2
    positive = [row for row in rows if row["amount"] > 0]
    for row in positive:
        fraction = float(row["amount"] / total)
        sweep = 2 * math.pi * fraction
        if len(positive) == 1 or fraction >= 0.9999:
            # A full circle cannot be one arc: draw it as two half circles.
            d = (
                f"M {cx:.3f} {cy - radius:.3f} "
                f"A {radius} {radius} 0 1 1 {cx:.3f} {cy + radius:.3f} "
                f"A {radius} {radius} 0 1 1 {cx:.3f} {cy - radius:.3f} Z"
            )
        else:
            x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
            x2 = cx + radius * math.cos(angle + sweep)
            y2 = cy + radius * math.sin(angle + sweep)
            large = 1 if sweep > math.pi else 0
            d = (
                f"M {cx:.3f} {cy:.3f} L {x1:.3f} {y1:.3f} "
                f"A {radius} {radius} 0 {large} 1 {x2:.3f} {y2:.3f} Z"
            )
        slices.append(
            {
                "d": d,
                "color": row["color"],
                "name": row["name"],
                "amount": row["amount"],
                "share": row["share"],
            }
        )
        angle += sweep
    return slices


def _chart_point_rows(values, width=760, height=220, peak=None):
    if not values:
        return []
    peak = chart_scale_max(values, peak)
    span = max(len(values) - 1, 1)
    points: list[dict[str, str | Decimal]] = []
    for index, value in enumerate(values):
        x = (Decimal(index) * Decimal(width) / Decimal(span)).quantize(Decimal("0.1"))
        y = (Decimal(height) - value * Decimal(height) / peak).quantize(Decimal("0.1"))
        points.append({"x": str(x), "y": str(y), "amount": money(value)})
    return points


def _setting_rates(home):
    setting = ApplicationSetting.objects.filter(household=home, key="outlook_cut_rates").first()
    saved = setting.value if setting and isinstance(setting.value, dict) else {}
    rates = {}
    for key, default in (("limited", "10"), ("moderate", "25"), ("significant", "50")):
        try:
            rates[key] = max(
                Decimal("0"), min(Decimal("100"), Decimal(str(saved.get(key, default))))
            )
        except Exception:
            rates[key] = Decimal(default)
    return rates


def outlook_data(home, start, end, today):
    if end < start:
        start, end = end, start
    actual_end = min(today, end)
    has_actual_days = actual_end >= start
    # Every actual figure below uses this one cutoff: nothing dated after it is counted.
    parts = _expense_parts(home, start, actual_end) if has_actual_days else []
    selected_parts = parts
    period_days = Decimal((end - start).days + 1)
    elapsed_days = Decimal((actual_end - start).days + 1) if has_actual_days else Decimal("0")
    remaining_days = max(period_days - elapsed_days, Decimal("0"))

    zero = Decimal("0.00")
    actual_spending = (
        net_spending(home, start, actual_end, Category.Type.SPENDING) if has_actual_days else zero
    )
    actual_bills = (
        net_spending(home, start, actual_end, Category.Type.BILL) if has_actual_days else zero
    )
    budget_target = _budget_target(home, start, end)
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=int(period_days) - 1)
    history_start = prior_start - timedelta(days=int(period_days) * 5)
    prior_bills = net_spending(home, prior_start, prior_end, Category.Type.BILL)

    # Spending still to come: recent daily pace (last three completed pay periods).
    recent = recent_completed_periods(home, today)
    recent_inputs = [
        item(
            f"{period.start_date} to {period.end_date}",
            net_spending(home, period.start_date, period.end_date),
            f"{(period.end_date - period.start_date).days + 1} days",
        )
        for period in recent
    ]
    recent_days = sum(((p.end_date - p.start_date).days + 1 for p in recent), 0)
    if recent_days:
        daily_pace = sum((row["amount"] for row in recent_inputs), zero) / Decimal(recent_days)
        pace_basis = f"last {len(recent)} completed pay periods ({recent_days} days)"
    else:
        daily_pace = budget_target / period_days
        pace_basis = "selected-range spending target (no completed pay periods yet)"
        recent_inputs = [item("Spending target for range", budget_target)]
    projected_spending_remaining = money(daily_pace * remaining_days)

    # Bills still to come: live obligations in the current pay period or later, unpaid.
    current_period = PayPeriod.objects.filter(
        household=home, start_date__lte=today, end_date__gte=today
    ).first()
    bills_from = max(start, current_period.start_date) if current_period else start
    live = list(
        live_bill_occurrences(home)
        .filter(due_date__range=(start, end))
        .select_related("bill", "category", "bill__category", "account", "bill__account")
    )
    bill_rows = []
    for occurrence in live:
        if occurrence.due_date < bills_from:
            continue
        forecast = bill_forecast(occurrence)
        if forecast["remaining"] > 0:
            bill_rows.append(
                {
                    "name": occurrence.bill.name,
                    "due_date": occurrence.due_date,
                    "remaining": forecast["remaining"],
                    "basis": forecast["basis"],
                }
            )
    bill_rows.sort(key=lambda row: (row["due_date"], row["name"]))
    projected_bills_remaining = sum((row["remaining"] for row in bill_rows), zero)
    recurring_total = sum((bill_forecast(item_)["amount"] for item_ in live), zero)
    subscription_total = sum(
        (
            bill_forecast(item_)["amount"]
            for item_ in live
            if item_.effective_category.name.casefold() == "subscriptions"
        ),
        zero,
    )

    # A range that starts after today: the days between today and the range still spend
    # money and still have bills due, and paychecks landing there are already counted below.
    gap_days = (start - actual_end).days - 1 if not has_actual_days else 0
    gap_spending = money(daily_pace * Decimal(gap_days))
    gap_bills = zero
    if gap_days > 0:
        gap_from = current_period.start_date if current_period else actual_end + timedelta(days=1)
        for occurrence in (
            live_bill_occurrences(home)
            .filter(due_date__range=(gap_from, start - timedelta(days=1)))
            .select_related("bill", "category", "bill__category", "account", "bill__account")
        ):
            gap_bills += bill_forecast(occurrence)["remaining"]

    # Income: recorded through the cutoff plus each paycheck still to come, once.
    actual_income = (
        _sum(Income.objects.filter(household=home, date__range=(start, actual_end)))
        if has_actual_days
        else zero
    )
    selected_pay_schedule = _paycheck_rows(home, start, actual_end, actual_end)
    expected_rows = _expected_paycheck_rows(home, actual_end, end)
    selected_pay_schedule = selected_pay_schedule + expected_rows
    comparison_pay_schedule = _paycheck_rows(home, prior_start, prior_end, prior_end)
    future_pay = sum((row["amount"] for row in expected_rows), zero)
    projected_income = actual_income + future_pay
    projected_total = (
        actual_spending + actual_bills + projected_spending_remaining + projected_bills_remaining
    )

    # Projected end balance: Spending + Bills balance at the cutoff, then what is still to come.
    opening_balance = _balance_total(home, start - timedelta(days=1))
    base_date = actual_end if has_actual_days else start - timedelta(days=1)
    base_balance = _balance_total(home, base_date)
    projected_end_balance = money(
        base_balance
        + future_pay
        - projected_spending_remaining
        - projected_bills_remaining
        - gap_spending
        - gap_bills
    )
    if gap_days > 0:
        # Nothing is recorded yet for the days before the range, so the balance going into
        # it is the balance today plus the paychecks, spending and bills expected in between.
        gap_paychecks = sum(
            (row["amount"] for row in expected_rows if row["deposit_date"] < start), zero
        )
        opening_balance = money(opening_balance + gap_paychecks - gap_spending - gap_bills)
    forecast_surplus = money(projected_end_balance - opening_balance)
    projection = {
        "spending_remaining": figure(
            "Spending still to come",
            projected_spending_remaining,
            f"Daily spending pace x {int(remaining_days)} remaining days",
            [
                *recent_inputs,
                item("Daily pace", daily_pace, pace_basis),
                item("Remaining days", remaining_days),
            ],
        ),
        "bills_remaining": figure(
            "Bills still to come",
            projected_bills_remaining,
            f"Unpaid live bills due {bills_from} to {end} (nothing before the current pay period)",
            [
                item(f"{row['name']} due {row['due_date']}", row["remaining"], row["basis"])
                for row in bill_rows
            ],
        ),
        "income_remaining": figure(
            "Paychecks still to come",
            future_pay,
            "One expected paycheck per pay period starting after the cutoff",
            [
                item(f"Paycheck {row['deposit_date']}", row["amount"], row["basis"])
                for row in expected_rows
            ],
        ),
        "end_balance": figure(
            "Projected end balance (Spending + Bills)",
            projected_end_balance,
            "Balance at cutoff + paychecks to come - spending to come - bills to come",
            [
                item(
                    f"Balance through {base_date}",
                    base_balance,
                ),
                item("Paychecks to come", future_pay),
                item("Spending to come", -projected_spending_remaining),
                item("Bills to come", -projected_bills_remaining),
                *(
                    [
                        item("Spending before the range", -gap_spending),
                        item("Bills before the range", -gap_bills),
                    ]
                    if gap_days > 0
                    else []
                ),
            ],
        ),
        "balance_change": figure(
            "Projected balance change over the range",
            forecast_surplus,
            "Projected end balance - balance before the range",
            [
                item("Projected end balance", projected_end_balance),
                item("Balance before range", -opening_balance),
            ],
        ),
    }

    category_map: dict[tuple[int, str, str], Decimal] = {}
    merchant_map: dict[tuple[int, str], Decimal] = {}
    merchant_labels: dict[tuple[int, str], str] = {}
    for row in selected_parts:
        category_key = (
            row["category"].pk,
            row["category"].name,
            row["category"].get_type_display(),
        )
        category_map[category_key] = category_map.get(category_key, Decimal("0.00")) + row["amount"]
        merchant_key = (row["category"].pk, row["merchant"].casefold())
        merchant_map[merchant_key] = merchant_map.get(merchant_key, Decimal("0.00")) + row["amount"]
        merchant_labels.setdefault(merchant_key, row["merchant"].title())
    category_rows: list[dict[str, Any]] = []
    for (category_id, name, type_label), amount in sorted(
        category_map.items(), key=lambda item: item[1], reverse=True
    ):
        merchant_pairs = sorted(
            (
                (merchant_labels[(merchant_category_id, merchant_key)], merchant_amount)
                for (merchant_category_id, merchant_key), merchant_amount in merchant_map.items()
                if merchant_category_id == category_id and merchant_amount > 0
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:3]
        merchants = [
            {"name": merchant, "amount": merchant_amount}
            for merchant, merchant_amount in merchant_pairs
        ]
        category_rows.append(
            {"name": name, "type": type_label, "amount": amount, "merchants": merchants}
        )
    chart_total = sum(
        (row["amount"] for row in category_rows if row["amount"] > 0), Decimal("0.00")
    )
    palette = ["#4477aa", "#ee6677", "#228833", "#ccbb44", "#66ccee", "#aa3377"]
    cursor = Decimal("0.00")
    segments = []
    for index, row in enumerate(category_rows):
        row["color"] = palette[index % len(palette)]
        row["share"] = (
            money(row["amount"] / chart_total * Decimal("100"))
            if row["amount"] > 0 and chart_total
            else Decimal("0.00")
        )
        if row["amount"] > 0 and chart_total:
            next_cursor = cursor + row["amount"] / chart_total * Decimal("100")
            segments.append(f"{row['color']} {cursor:.2f}% {next_cursor:.2f}%")
            cursor = next_cursor

    discretionary = sum(
        (row["amount"] for row in selected_parts if not row["essential"]), Decimal("0.00")
    )
    monthly_discretionary = money(discretionary / period_days * Decimal("30.44"))
    deficit = max(-forecast_surplus, Decimal("0.00"))
    rates = _setting_rates(home)
    cut_scenarios = []
    for scenario_key in ("limited", "moderate", "significant"):
        potential = money(monthly_discretionary * rates[scenario_key] / Decimal("100"))
        cut_scenarios.append(
            {
                "name": scenario_key.title(),
                "rate": rates[scenario_key],
                "potential": potential,
                "available": max(potential - deficit, Decimal("0.00")),
            }
        )
    discretionary_categories: dict[str, Decimal] = {}
    for row in selected_parts:
        if not row["essential"]:
            name = row["category"].name
            discretionary_categories[name] = (
                discretionary_categories.get(name, Decimal("0.00")) + row["amount"]
            )
    category_cut_rows = []
    for name, amount in sorted(
        discretionary_categories.items(), key=lambda item: item[1], reverse=True
    ):
        monthly_amount = money(max(amount, Decimal("0.00")) / period_days * Decimal("30.44"))
        if monthly_amount <= 0:
            continue
        category_cut_rows.append(
            {
                "name": name,
                "monthly_amount": monthly_amount,
                "scenarios": [
                    {
                        "name": scenario_key.title(),
                        "rate": rates[scenario_key],
                        "amount": money(monthly_amount * rates[scenario_key] / Decimal("100")),
                    }
                    for scenario_key in ("limited", "moderate", "significant")
                ],
            }
        )

    savings = savings_account(home)
    savings_start = (
        account_balance(savings, start - timedelta(days=1)) if savings else Decimal("0.00")
    )
    savings_end = (
        account_balance(savings, actual_end) if savings and has_actual_days else savings_start
    )
    fulfilled = (
        -sum(
            (
                entry.amount
                for entry in SavingsGoalEntry.objects.filter(
                    goal__household=home,
                    date__range=(start, actual_end),
                    fulfills_goal=True,
                    amount__lt=0,
                )
            ),
            Decimal("0.00"),
        )
        if has_actual_days
        else Decimal("0.00")
    )
    savings_change = savings_end - savings_start
    adjusted_savings_change = savings_change + fulfilled
    savings_health = (
        "grew"
        if adjusted_savings_change > 0
        else "declined"
        if adjusted_savings_change < 0
        else "held steady"
    )

    top_goal = (
        SavingsGoal.objects.filter(
            household=home, status=SavingsGoal.Status.ACTIVE, is_system=False
        )
        .order_by("priority")
        .first()
    )
    goal_summary = None
    if top_goal:
        current = top_goal.current_amount
        net_contributions = (
            _sum(top_goal.entries.filter(date__range=(start, actual_end)))
            if has_actual_days
            else Decimal("0.00")
        )
        progress = (
            min(current / top_goal.target_amount * Decimal("100"), Decimal("100"))
            if top_goal.target_amount
            else Decimal("100")
        )
        completion = "No completion date yet"
        if net_contributions > 0 and elapsed_days:
            remaining = max(top_goal.target_amount - current, Decimal("0.00"))
            days_needed = (remaining / (net_contributions / elapsed_days)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            completion = f"About {days_needed} days at the current contribution pace"
        goal_summary = {
            "name": top_goal.name,
            "current": current,
            "target": top_goal.target_amount,
            "progress": progress.quantize(Decimal("0.1")),
            "net_contributions": net_contributions,
            "completion": completion,
        }

    dates = [start + timedelta(days=index) for index in range(int(period_days))]
    daily_map = {value: Decimal("0.00") for value in dates}
    for row in selected_parts:
        daily_map[row["date"]] += row["amount"]
    daily_values = [max(daily_map[value], Decimal("0.00")) for value in dates]
    daily_chart_points = _chart_point_rows(daily_values)
    for point, day in zip(daily_chart_points, dates, strict=True):
        point["date"] = day
    cumulative_actual = []
    running = Decimal("0.00")
    for index, value in enumerate(daily_values):
        if dates[index] <= actual_end:
            running += value
        cumulative_actual.append(running)
    forecast_daily = (
        (projected_spending_remaining + projected_bills_remaining) / remaining_days
        if remaining_days
        else Decimal("0.00")
    )
    cumulative_projected = []
    running_projected = Decimal("0.00")
    for index, value in enumerate(daily_values):
        running_projected += value if dates[index] <= actual_end else forecast_daily
        cumulative_projected.append(running_projected)
    cumulative_budget = [
        (budget_target + recurring_total) * Decimal(index + 1) / period_days
        for index in range(len(dates))
    ]
    shared_peak = max(
        cumulative_actual + cumulative_projected + cumulative_budget + [Decimal("1.00")]
    )
    largest_nonbill = next(
        (
            row
            for row in category_rows
            if row["type"] == Category.Type(Category.Type.SPENDING).label
        ),
        None,
    )
    pace_status = (
        "over" if actual_spending + projected_spending_remaining > budget_target else "within"
    )
    overview = (
        f"Through {actual_end}, non-bill spending is projected to finish {pace_status} "
        f"the ${budget_target:,.2f} target. ${projected_bills_remaining:,.2f} in unpaid bills "
        f"remains due in this range. The Spending and Bills balance is projected to "
        f"{'grow' if forecast_surplus >= 0 else 'fall'} by ${abs(forecast_surplus):,.2f} "
        # A minus sign joined to the amount (word joiner), so it never sits alone on a line.
        f"and end at {'\u2212\u2060' if projected_end_balance < 0 else ''}"
        f"${abs(projected_end_balance):,.2f}."
    )
    return {
        "start": start,
        "end": end,
        "actual_end": actual_end,
        "period_days": int(period_days),
        "elapsed_days": int(elapsed_days),
        "remaining_days": int(remaining_days),
        "actual_spending": money(actual_spending),
        "actual_bills": money(actual_bills),
        "prior_bills": money(prior_bills),
        "actual_income": money(actual_income),
        "as_of": actual_end,
        "scheduled_income_remaining": money(future_pay),
        "selected_income_by_date": _income_by_date(home, start, actual_end),
        "comparison_income_by_date": _income_by_date(home, prior_start, prior_end),
        "recent_income_by_date": _income_by_date(home, history_start, actual_end)[-24:],
        "selected_pay_schedule": selected_pay_schedule,
        "comparison_pay_schedule": comparison_pay_schedule,
        "budget_target": budget_target,
        "projected_spending_remaining": projected_spending_remaining,
        # Shown with the projected end balance: the pace behind "spending to come".
        "daily_pace": money(daily_pace),
        "pace_periods": len(recent) if recent_days else 0,
        "projected_bills_remaining": money(projected_bills_remaining),
        "bill_rows": bill_rows,
        "expected_paycheck_rows": expected_rows,
        "projection": projection,
        "projected_end_balance": projected_end_balance,
        "opening_balance": opening_balance,
        "projected_income": money(projected_income),
        "projected_total": money(projected_total),
        "forecast_surplus": forecast_surplus,
        "forecast_surplus_abs": abs(forecast_surplus),
        "recurring_total": money(recurring_total),
        "subscription_total": money(subscription_total),
        "category_rows": category_rows,
        "category_slices": pie_slices(category_rows, chart_total),
        "daily_series": [
            {"date": day.isoformat(), "amount": str(value)}
            for day, value in zip(dates, daily_values, strict=True)
        ],
        "category_chart_total": money(chart_total),
        "category_chart_gradient": ", ".join(segments) or "var(--muted) 0% 100%",
        "monthly_discretionary": monthly_discretionary,
        "cut_scenarios": cut_scenarios,
        "category_cut_rows": category_cut_rows,
        "cut_rates": rates,
        "savings_start": money(savings_start),
        "savings_end": money(savings_end),
        "savings_change": money(savings_change),
        "savings_change_abs": abs(money(savings_change)),
        "fulfilled_savings": money(fulfilled),
        "savings_health": savings_health,
        "top_goal": goal_summary,
        "largest_nonbill": largest_nonbill,
        "overview": overview,
        "daily_rows": [{"date": day, "amount": money(daily_map[day])} for day in dates],
        "daily_points": _chart_points(daily_values),
        "daily_chart_points": daily_chart_points,
        "cumulative_actual_points": _chart_points(cumulative_actual, peak=shared_peak),
        "cumulative_projected_points": _chart_points(cumulative_projected, peak=shared_peak),
        "cumulative_budget_points": _chart_points(cumulative_budget, peak=shared_peak),
    }
