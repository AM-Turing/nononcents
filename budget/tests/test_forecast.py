"""Acceptance fixture reproducing the 2026-11-17 live-data situation.

Pay periods are built directly with anchored dates: paychecks land on the first day of the
period they fund (10-28, 11-10, then an expected 11-30).
"""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from budget.forecast import NEEDS_REVIEW_TEXT, payday_forecast
from budget.models import (
    Account,
    BillOccurrence,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    Income,
    PayPeriod,
    PayPeriodBudget,
    RecurringBill,
    Refund,
)
from budget.outlook import outlook_data
from budget.report_analysis import ReportAnalysisError
from budget.services import bill_forecast
from budget.views import report_period_totals

TODAY = date(2026, 11, 17)
D = Decimal


def make_period(home, start, end, **budget):
    period = PayPeriod.objects.create(
        household=home,
        nominal_payday=start,
        expected_deposit_date=start,
        start_date=start,
        end_date=end,
    )
    PayPeriodBudget.objects.create(pay_period=period, **budget)
    return period


def make_bill(home, name, amount, account, category, **extra):
    return RecurringBill.objects.create(
        household=home,
        name=name,
        default_amount=D(amount),
        due_day=extra.pop("due_day", 15),
        category=category,
        account=account,
        effective_start=extra.pop("effective_start", date(2026, 1, 1)),
        **extra,
    )


def occurrence(bill, due, amount, **extra):
    return BillOccurrence.objects.create(
        bill=bill, due_date=due, expected_amount=D(amount), **extra
    )


@pytest.fixture
def world(db):
    user = get_user_model().objects.create_user("forecast-user", password="safe-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    spending = Account.objects.create(
        household=home, name="Spending", type=Account.Type.SPENDING, opening_balance="1000.00"
    )
    bills = Account.objects.create(
        household=home, name="Bills", type=Account.Type.BILLS, opening_balance="3200.00"
    )
    Account.objects.create(household=home, name="Savings", type=Account.Type.SAVINGS)
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    utilities = Category.objects.create(household=home, name="Utilities", type="bill")

    # Completed periods feed the spending history: 100, 200 (250 - 50 refund), 300.
    p11 = make_period(home, date(2026, 9, 28), date(2026, 10, 12))
    p12 = make_period(home, date(2026, 10, 13), date(2026, 10, 27))
    p13 = make_period(home, date(2026, 10, 28), date(2026, 11, 9), expected_paycheck="4000.00")
    p14 = make_period(
        home,
        date(2026, 11, 10),
        date(2026, 11, 29),
        spending_target="50.00",
        expected_paycheck="4000.00",
    )
    p15 = make_period(home, date(2026, 11, 30), date(2026, 12, 14), expected_paycheck="100.00")

    def spend(day, amount, account=spending, category=groceries, **extra):
        return Expense.objects.create(
            household=home,
            created_by=user,
            date=day,
            amount=D(amount),
            account=account,
            category=category,
            **extra,
        )

    spend(date(2026, 10, 1), "100.00")
    spend(date(2026, 10, 14), "250.00")
    Refund.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 10, 20),
        amount=D("50.00"),
        account=spending,
        category=groceries,
    )
    spend(date(2026, 11, 1), "300.00")
    # Current period: $100 groceries, plus a Spending-category purchase on the Bills account
    # and a refund; all of it is Spending, on any account.
    spend(date(2026, 11, 12), "100.00")
    spend(date(2026, 11, 13), "30.00", account=bills)
    Refund.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 11, 14),
        amount=D("10.00"),
        account=spending,
        category=groceries,
    )
    for day in (date(2026, 10, 28), date(2026, 11, 10)):
        Income.objects.create(
            household=home,
            created_by=user,
            date=day,
            amount=D("4000.00"),
            source="Pay",
            is_paycheck=True,
        )

    # Removed bill: inactive, ended 08-31, still has unpaid occurrences into 2027.
    gym = make_bill(
        home,
        "Gym",
        "50.00",
        bills,
        utilities,
        active=False,
        effective_end=date(2026, 8, 31),
    )
    for year, month in [(2026, m) for m in range(9, 13)] + [(2027, m) for m in range(1, 13)]:
        occurrence(gym, date(year, month, 20), "50.00")

    # Active bill with an unpaid occurrence from August and one due before payday.
    rent = make_bill(home, "Rent", "800.00", bills, utilities)
    occurrence(rent, date(2026, 8, 20), "800.00")
    occurrence(rent, date(2026, 11, 20), "800.00")

    # Variable bill: default 285, three paid occurrences averaging 550.
    electric = make_bill(home, "Electricity", "285.00", bills, utilities)
    for due, paid in (
        (date(2026, 8, 15), "540.00"),
        (date(2026, 9, 15), "550.00"),
        (date(2026, 10, 15), "560.00"),
    ):
        paid_occurrence = occurrence(electric, due, "285.00", status=BillOccurrence.Status.PAID)
        spend(due, paid, account=bills, category=utilities, bill_occurrence=paid_occurrence)
    occurrence(electric, date(2026, 11, 25), "285.00")

    # Small bill paid from Spending, due before payday; two bills next period.
    streaming = make_bill(home, "Streaming", "15.00", spending, utilities)
    occurrence(streaming, date(2026, 11, 22), "15.00")
    phone = make_bill(home, "Phone", "100.00", spending, utilities)
    occurrence(phone, date(2026, 12, 5), "100.00")
    internet = make_bill(home, "Internet", "60.00", bills, utilities)
    occurrence(internet, date(2026, 12, 2), "60.00")
    return {
        "home": home,
        "user": user,
        "spending": spending,
        "bills": bills,
        "groceries": groceries,
        "periods": (p11, p12, p13, p14, p15),
    }


def amounts(rows):
    return {row["name"]: row["remaining"] for row in rows}


@pytest.mark.django_db
def test_forecast_matches_hand_computed_formula(world):
    forecast = payday_forecast(world["home"], TODAY)

    assert forecast["available"]
    assert forecast["payday"] == date(2026, 11, 30)
    assert forecast["days_until_payday"] == 13
    # Spending: 1000 - 100 - 30(bills acct, not here) - refund 10 ... ledger-derived.
    spending_balance = D("1000.00") - D("100.00") - D("100.00") - D("250.00") - D("300.00")
    spending_balance += D("50.00") + D("10.00")
    assert forecast["spending_balance"] == spending_balance == D("310.00")
    bills_balance = D("3200.00") - D("30.00") - D("540.00") - D("550.00") - D("560.00")
    assert forecast["bills_balance"] == bills_balance == D("1520.00")

    due = forecast["due_this_period"]
    assert amounts(due["bills_items"]) == {"Rent": D("800.00"), "Electricity": D("550.00")}
    assert amounts(due["spending_items"]) == {"Streaming": D("15.00")}
    assert due["bills_total"] == D("1350.00")
    assert due["spending_total"] == D("15.00")
    assert forecast["next_period_bills"]["total"] == D("160.00")

    assert forecast["expected_paycheck"]["amount"] == D("100.00")
    assert forecast["anticipated_spending"]["amount"] == D("200.00")
    bills_surplus = D("1520.00") - D("1350.00")
    hold_back = max(D("0"), D("160.00") + D("200.00") - D("100.00"))
    safe = D("310.00") - D("15.00") - max(D("0"), hold_back - bills_surplus)
    assert forecast["bills_surplus"]["amount"] == bills_surplus == D("170.00")
    assert forecast["hold_back"]["amount"] == hold_back == D("260.00")
    assert forecast["safe_to_spend"]["amount"] == safe == D("205.00")
    assert forecast["per_day"]["amount"] == (safe / 13).quantize(D("0.01"))
    assert forecast["next_bills_covered"]["amount"] == D("-90.00")
    assert forecast["safe_to_spend"]["inputs"] and forecast["safe_to_spend"]["formula"]


@pytest.mark.django_db
def test_removed_bills_and_august_occurrence_never_enter_totals(world):
    forecast = payday_forecast(world["home"], TODAY)
    review = forecast["needs_review"]

    assert review["count"] == 1
    assert review["total"] == D("800.00")
    assert review["items"][0]["due_date"] == date(2026, 8, 20)
    assert review["message"] == NEEDS_REVIEW_TEXT
    counted = forecast["due_this_period"]["items"] + forecast["next_period_bills"]["items"]
    assert all(row["name"] != "Gym" for row in review["items"] + counted)
    assert all(row["due_date"] != date(2026, 8, 20) for row in counted)

    data = outlook_data(world["home"], date(2026, 10, 28), date(2026, 11, 29), TODAY)
    assert all(row["name"] != "Gym" for row in data["bill_rows"])
    assert all(row["due_date"] >= date(2026, 10, 28) for row in data["bill_rows"])
    # Rent 11-20 + Electricity 11-25 (average) + Streaming 11-22; Gym excluded.
    assert data["projected_bills_remaining"] == D("1365.00")


@pytest.mark.django_db
def test_variable_bill_forecast_uses_recent_average(world):
    electric = BillOccurrence.objects.get(bill__name="Electricity", due_date=date(2026, 11, 25))
    forecast = bill_forecast(electric)

    assert forecast["amount"] == D("550.00")
    assert forecast["basis"] == "average of last 3 payments"


@pytest.mark.django_db
@pytest.mark.parametrize("start", [date(2026, 10, 29), date(2026, 10, 28)])
def test_each_paycheck_is_counted_once_in_the_outlook_range(world, start):
    data = outlook_data(world["home"], start, date(2026, 11, 29), TODAY)

    expected_actual = D("4000.00") if start.day == 29 else D("8000.00")
    assert data["actual_income"] == expected_actual
    assert data["scheduled_income_remaining"] == D("0.00")
    assert data["projected_income"] == expected_actual
    assert len(data["selected_pay_schedule"]) == (1 if start.day == 29 else 2)


@pytest.mark.django_db
def test_future_paycheck_is_expected_once_even_when_already_recorded(world):
    # On 11-05 the 11-10 paycheck has not landed; it is expected once (not twice).
    data = outlook_data(world["home"], date(2026, 10, 28), date(2026, 11, 29), date(2026, 11, 5))

    assert data["actual_income"] == D("4000.00")
    assert data["scheduled_income_remaining"] == D("4000.00")
    assert data["projected_income"] == D("8000.00")
    assert [row["timing"] for row in data["selected_pay_schedule"]] == ["elapsed", "future"]


@pytest.fixture
def frozen_today(monkeypatch):
    monkeypatch.setattr(timezone, "localdate", lambda: TODAY)


@pytest.mark.django_db
def test_dashboard_shows_negative_remaining_and_short_by(world, client, frozen_today):
    client.force_login(world["user"])
    response = client.get(reverse("dashboard"))
    content = response.content.decode()

    # Target 50; spent 100 + 30 - 10 = 120 -> over by 70, never clamped to zero.
    assert response.context["spent"] == D("120.00")
    assert response.context["spending_remaining"] == D("-70.00")
    assert "over by $70.00" in content
    assert "Until payday" in content
    assert "How this is worked out" in content
    assert "Next period is short by $90.00" in content
    assert NEEDS_REVIEW_TEXT in content
    assert "Gym" not in content
    assert "4688.24" not in content


@pytest.mark.django_db
def test_dashboard_and_reports_agree_on_spending(world, client, frozen_today):
    client.force_login(world["user"])
    period = world["periods"][3]
    dashboard_spent = client.get(reverse("dashboard")).context["spent"]
    totals = report_period_totals(world["home"], period.start_date, period.end_date)
    outlook = outlook_data(world["home"], period.start_date, period.end_date, TODAY)

    assert dashboard_spent == totals["spending"] == outlook["actual_spending"] == D("120.00")


@pytest.mark.django_db
def test_csv_and_payload_carry_one_as_of_cutoff(world, client, frozen_today, monkeypatch):
    # A future-dated expense must not leak into any figure.
    Expense.objects.create(
        household=world["home"],
        created_by=world["user"],
        date=date(2026, 11, 28),
        amount=D("999.00"),
        account=world["spending"],
        category=world["groceries"],
    )
    client.force_login(world["user"])
    params = {"start": "2026-11-10", "end": "2026-11-29"}

    response = client.get(reverse("reports"), {**params, "format": "csv"})
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0] == ["Budget outlook", "2026-11-10", "2026-11-29", "As of", "2026-11-17"]
    by_label = {row[0]: row[1] for row in rows[2:] if len(row) == 2}
    assert by_label["Actual spending through as-of"] == "120.00"
    assert all(row[0] != "Groceries" or row[2] == "120.00" for row in rows if len(row) == 4)

    captured = {}

    class Analyzer:
        def analyze(self, payload):
            captured.update(payload)
            raise ReportAnalysisError("stop")

    monkeypatch.setattr("budget.analysis_jobs.configured_report_analyzer", lambda: Analyzer())
    monkeypatch.setattr("budget.analysis_jobs.spawn", lambda target, *args: target(*args))
    client.post(reverse("reports_analysis_start"), params, follow=True)
    assert captured["as_of"] == "2026-11-17"
    assert captured["selected_period"]["end"] == "2026-11-29"
    assert captured["totals"]["selected"]["spending"] == "120.00"
    assert captured["trend_evidence"]["non_bill_spending"]["actual_through_as_of"] == "120.00"


def test_anticipated_spending_uses_the_target_not_one_off_history(world):
    next_period = PayPeriod.objects.get(household=world["home"], start_date=date(2026, 11, 30))
    PayPeriodBudget.objects.filter(pay_period=next_period).update(spending_target=D("120.00"))
    forecast = payday_forecast(world["home"], TODAY)
    anticipated = forecast["anticipated_spending"]
    assert anticipated["amount"] == D("120.00")
    labels = [row["label"] for row in anticipated["inputs"]]
    assert labels[0] == "Spending target for next period"
    assert any(label.startswith("For comparison only") for label in labels)
    # A big one-off in the last completed period changes nothing while a target is set.
    Expense.objects.create(
        household=world["home"],
        date=date(2026, 11, 1),
        amount=D("900.00"),
        account=world["spending"],
        category=world["groceries"],
        merchant="Plane tickets",
    )
    assert payday_forecast(world["home"], TODAY)["anticipated_spending"]["amount"] == D("120.00")


@pytest.mark.django_db
def test_carry_over_comes_from_bills_first_then_spending(world):
    # Needed 160 bills + 200 spending - 100 paycheck = 260; Bills has 170 left after this
    # period's bills, so 170 comes from Bills and 90 from Spending (the Spending hold-back).
    forecast = payday_forecast(world["home"], TODAY)
    carry = forecast["carry_over"]
    assert carry["needed"] == forecast["hold_back"]["amount"] == D("260.00")
    assert carry["from_bills"] == D("170.00")
    assert carry["from_spending"] == D("90.00") == forecast["held_for_next"]
    assert carry["bills_shortfall_from_spending"] == D("0.00")
    assert carry["spare"] == D("0.00")


@pytest.mark.django_db
def test_carry_over_when_bills_is_short_this_period(world):
    # 220 more paid from Bills: it ends this period 50 short, so Spending holds the whole
    # 260 carry-over plus the 50 shortfall.
    Expense.objects.create(
        household=world["home"],
        date=TODAY,
        amount=D("220.00"),
        account=world["bills"],
        category=world["groceries"],
        merchant="Extra from Bills",
    )
    forecast = payday_forecast(world["home"], TODAY)
    carry = forecast["carry_over"]
    assert forecast["bills_surplus"]["amount"] == D("-50.00")
    assert carry["needed"] == D("260.00")
    assert carry["from_bills"] == D("0.00")
    assert carry["from_spending"] == D("260.00")
    assert carry["bills_shortfall_from_spending"] == D("50.00")
    assert forecast["held_for_next"] == D("310.00")


@pytest.mark.django_db
def test_no_carry_over_when_the_paycheck_covers_next_period(world):
    next_period = PayPeriod.objects.get(household=world["home"], start_date=date(2026, 11, 30))
    PayPeriodBudget.objects.filter(pay_period=next_period).update(
        spending_target=D("120.00"), expected_paycheck=D("1000.00")
    )
    carry = payday_forecast(world["home"], TODAY)["carry_over"]
    # 160 bills + 120 target - 1000 paycheck: nothing to carry, 720 to spare.
    assert carry["needed"] == D("0.00")
    assert carry["from_bills"] == carry["from_spending"] == D("0.00")
    assert carry["spare"] == D("720.00")


@pytest.mark.django_db
def test_dashboard_carry_over_card_states(world, client, frozen_today):
    client.force_login(world["user"])
    content = client.get(reverse("dashboard")).content.decode()
    assert "Needs to carry over" in content
    assert "From Bills" in content and "$170.00" in content
    assert "From Spending" in content and "$90.00" in content

    next_period = PayPeriod.objects.get(household=world["home"], start_date=date(2026, 11, 30))
    PayPeriodBudget.objects.filter(pay_period=next_period).update(
        spending_target=D("120.00"), expected_paycheck=D("1000.00")
    )
    content = client.get(reverse("dashboard")).content.decode()
    assert "Nothing needs to carry over" in content
    assert "with $720.00 to spare" in content
    assert "From Bills" not in content


@pytest.mark.django_db
def test_outlook_explains_the_pace_behind_the_projected_end_balance(world, client, frozen_today):
    client.force_login(world["user"])
    response = client.get(reverse("reports"))
    content = response.content.decode()
    ctx = response.context
    periods = ctx["pace_periods"]
    assert periods == 3
    remaining = ctx["projected_spending_remaining"]
    assert (
        abs(ctx["daily_pace"] * ctx["remaining_days"] - remaining)
        < D("0.01") * ctx["remaining_days"]
    )
    assert "Based on your spending over the last 3 pay periods" in content
    assert f"averaging ${ctx['daily_pace']:,.2f} a day" in content
    end = ctx["projected_end_balance"]
    if end < 0:
        assert f"${-end:,.2f} short by" in content
        assert f"an additional ${-end:,.2f} the next pay period would need to cover" in content
    else:
        assert f"would have ${end:,.2f} left on" in content
    assert "This is a projection from past spending" in content


@pytest.mark.django_db
def test_outlook_pace_note_names_the_shortfall(world, client, frozen_today):
    # A $1,000 purchase today takes the projected end balance from 297.56 to -702.44;
    # the pace (from completed periods) does not change.
    Expense.objects.create(
        household=world["home"],
        date=TODAY,
        amount=D("1000.00"),
        account=world["spending"],
        category=world["groceries"],
        merchant="Large purchase",
    )
    client.force_login(world["user"])
    response = client.get(reverse("reports"))
    content = response.content.decode()
    assert response.context["projected_end_balance"] == D("-702.44")
    assert "averaging $13.95 a day" in content
    assert "would be $702.44 short by" in content
    assert "an additional $702.44 the next pay period would need to cover" in content
