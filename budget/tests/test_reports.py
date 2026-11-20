import json
from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget.models import (
    Account,
    BalanceAdjustment,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    Income,
    IncomeAllocation,
    PayPeriod,
    PayPeriodBudget,
    Refund,
    Transfer,
)
from budget.outlook import outlook_data
from budget.report_analysis import (
    OpenAICompatibleReportAnalyzer,
    ReportAnalysisError,
    enforce_report_grounding,
    parse_report_analysis,
)
from budget.views import report_analysis_payload, report_data


@pytest.mark.django_db
def test_report_separates_spending_bills_transfers():
    home = Household.objects.create()
    user = get_user_model().objects.create_user("u")
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    spend_cat = Category.objects.create(household=home, name="Spend", type="spending")
    bill_cat = Category.objects.create(household=home, name="Bill", type="bill")
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 1),
        amount="500",
        account=spending,
        category=spend_cat,
    )
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 1),
        amount="2000",
        account=bills,
        category=bill_cat,
    )
    Refund.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 2),
        amount="25",
        account=bills,
        category=bill_cat,
    )
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 1),
        amount="75",
        account=spending,
        category=bill_cat,
    )
    Transfer.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 1),
        amount="100",
        source=spending,
        destination=bills,
    )
    report = report_data(home, date(2026, 8, 1), date(2026, 8, 31))
    assert report["spending"] == Decimal("500")
    assert report["bills"] == Decimal("2050")
    assert report["refunds"] == Decimal("25")
    assert report["transfers"] == Decimal("100")
    assert [(row["name"], row["amount"]) for row in report["category_rows"]] == [
        ("Bill", Decimal("2050")),
        ("Spend", Decimal("500")),
    ]
    assert report["category_chart_total"] == Decimal("2550")


@pytest.mark.django_db
def test_report_summarizes_category_trends_and_every_account_flow():
    home = Household.objects.create()
    user = get_user_model().objects.create_user("flow-user")
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    savings = Account.objects.create(household=home, name="Savings", type="savings")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 7, 10),
        amount="100.00",
        account=spending,
        category=groceries,
    )
    prior_income = Income.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 7, 1),
        amount="1000.00",
        source="Prior paycheck",
    )
    IncomeAllocation.objects.create(income=prior_income, account=spending, amount="800.00")
    IncomeAllocation.objects.create(income=prior_income, account=savings, amount="200.00")
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 10),
        amount="300.00",
        account=spending,
        category=groceries,
    )
    Refund.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 11),
        amount="20.00",
        account=spending,
        category=groceries,
    )
    income = Income.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 1),
        amount="1500.00",
        source="Paycheck",
    )
    IncomeAllocation.objects.create(income=income, account=spending, amount="1000.00")
    IncomeAllocation.objects.create(income=income, account=savings, amount="500.00")
    Transfer.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 12),
        amount="200.00",
        source=spending,
        destination=savings,
    )
    BalanceAdjustment.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 15),
        account=savings,
        amount="10.00",
        is_increase=True,
        reason="Correction",
    )
    BalanceAdjustment.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 16),
        account=savings,
        amount="5.00",
        is_increase=False,
        reason="Correction",
    )

    report = report_data(home, date(2026, 8, 1), date(2026, 8, 31))

    category = report["category_rows"][0]
    assert category["amount"] == Decimal("280.00")
    assert category["previous_amount"] == Decimal("100.00")
    assert category["change"] == Decimal("180.00")
    flows = {row["name"]: row for row in report["account_rows"]}
    spending_flow = flows["Spending"]
    savings_flow = flows["Savings"]
    assert spending_flow["inflows"] == Decimal("1020.00")
    assert spending_flow["outflows"] == Decimal("500.00")
    assert spending_flow["net"] == Decimal("520.00")
    assert savings_flow["inflows"] == Decimal("710.00")
    assert savings_flow["outflows"] == Decimal("5.00")
    assert savings_flow["net"] == Decimal("705.00")
    assert report["account_inflows"] == Decimal("1730.00")
    assert report["account_outflows"] == Decimal("505.00")
    payload = report_analysis_payload(report)
    assert payload["totals"]["selected"]["income"] == "1500.00"
    assert payload["totals"]["comparison"]["income"] == "1000.00"
    assert payload["categories"][0]["current_net_spending"] == "280.00"
    savings_payload = next(row for row in payload["accounts"] if row["name"] == "Savings")
    assert savings_payload["transfers_in"] == "200.00"


@pytest.mark.django_db
def test_analysis_payload_projects_scheduled_income_for_an_in_progress_range():
    home = Household.objects.create()
    user = get_user_model().objects.create_user("income-pattern-user")
    prior_period = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 15),
        expected_deposit_date=date(2026, 8, 14),
        actual_deposit_date=date(2026, 8, 14),
        start_date=date(2026, 8, 14),
        end_date=date(2026, 8, 29),
    )
    current_period = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 9, 1),
        expected_deposit_date=date(2026, 8, 31),
        actual_deposit_date=date(2026, 8, 31),
        start_date=date(2026, 8, 30),
        end_date=date(2026, 9, 13),
    )
    upcoming_period = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 9, 15),
        expected_deposit_date=date(2026, 9, 14),
        start_date=date(2026, 9, 14),
        end_date=date(2026, 9, 29),
    )
    PayPeriodBudget.objects.create(pay_period=prior_period, expected_paycheck=Decimal("10000.00"))
    PayPeriodBudget.objects.create(pay_period=current_period, expected_paycheck=Decimal("5000.00"))
    PayPeriodBudget.objects.create(pay_period=upcoming_period, expected_paycheck=Decimal("5000.00"))
    Income.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 14),
        amount=Decimal("10000.00"),
        source="Prior pay",
        is_paycheck=True,
    )
    Income.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 31),
        amount=Decimal("5000.00"),
        source="Current pay",
        is_paycheck=True,
    )

    start = date(2026, 8, 30)
    end = date(2026, 9, 15)
    report = report_data(home, start, end)
    outlook = outlook_data(home, start, end, date(2026, 9, 10))
    payload = report_analysis_payload(report, outlook)

    assert payload["totals"]["selected"]["income"] == "5000.00"
    assert payload["totals"]["comparison"]["income"] == "10000.00"
    assert payload["analysis_context"] == {
        "as_of_date": "2026-09-10",
        "selected_period_status": "in_progress",
        "selected_period_days": 17,
        "observed_days": 12,
        "remaining_days": 5,
        "selected_actuals_are_partial": True,
        "direct_selected_vs_adjacent_income_comparison_is_valid": False,
        "historical_windows_with_activity": 1,
        "interpretation": (
            "Use projected full-period values for an in-progress range. Missing future "
            "deposits are scheduled income, not lost income. Use actual values for a "
            "completed historical range."
        ),
    }
    income_evidence = payload["trend_evidence"]["income"]
    assert income_evidence["actual_through_as_of"] == "5000.00"
    assert income_evidence["scheduled_remaining"] == "5000.00"
    assert income_evidence["projected_full_period"] == "10000.00"
    assert income_evidence["completed_history_average"] == "10000.00"
    assert income_evidence["selected_schedule"][1]["deposit_date"] == "2026-09-14"
    assert income_evidence["selected_schedule"][1]["timing"] == "future"


def valid_analysis(**overrides):
    value = {
        "bottom_line_up_front": {
            "concerns": "Groceries increased compared with the prior period.",
            "expense_summary": "Expenses remain below income in the selected period.",
            "recommended_way_ahead": (
                "Preserve the savings contribution and set a lower grocery target."
            ),
        },
        "overview": "Overall spending remained manageable.",
        "spending_trends": "Groceries increased compared with the prior period.",
        "negative_trends": ["Groceries increased by $180.00."],
        "neutral_trends": ["Bills were unchanged."],
        "positive_trends": ["Savings received a positive net inflow."],
        "actionable_options": ["Use the limited cut option for groceries."],
        "account_flow_summary": "Income funded Spending and Savings, with one internal transfer.",
        "conclusion": "Review the category increase while maintaining savings contributions.",
    }
    value.update(overrides)
    return value


def test_grounding_replaces_false_partial_period_income_loss_claim():
    raw = valid_analysis(
        bottom_line_up_front={
            "concerns": "Income dropped significantly against the prior period.",
            "expense_summary": "Lower income may create an overspending situation.",
            "recommended_way_ahead": "Increase income with additional work.",
        },
        overview="The household lost income during this period.",
        negative_trends=["Income decreased by $5,000."],
        conclusion="Replace the missing income.",
    )
    summary = {
        "analysis_context": {
            "as_of_date": "2026-09-10",
            "selected_period_status": "in_progress",
        },
        "trend_evidence": {
            "income": {
                "actual_through_as_of": "5000.00",
                "scheduled_remaining": "5000.00",
                "projected_full_period": "10000.00",
                "completed_history_average": "10000.00",
            },
            "non_bill_spending": {
                "projected_full_period": "1500.00",
                "budget_target": "2000.00",
                "completed_history_average": "1600.00",
            },
            "bills": {"projected_full_period": "1000.00"},
            "savings": {
                "adjusted_actual_change": "200.00",
                "completed_history_average_adjusted_change": "150.00",
            },
        },
        "outlook": {
            "forecast": {"forecast_surplus": "7000.00"},
            "cut_scenarios": [],
            "first_priority": None,
        },
        "categories": [],
    }

    result = enforce_report_grounding(parse_report_analysis(raw, model="local-model"), summary)

    combined = " ".join(
        (
            result.bottom_line_up_front.concerns,
            result.bottom_line_up_front.expense_summary,
            result.overview,
            result.conclusion,
        )
    )
    assert result.model == "local-model (grounded)"
    assert "$5,000.00 has been received" in combined
    assert "$5,000.00 remains scheduled" in combined
    assert "lost income" not in combined.casefold()
    assert "significant drop" not in combined.casefold()


def test_openai_compatible_report_analyzer_sends_aggregate_schema_and_parses_response(settings):
    settings.REPORT_LLM_TIMEOUT_SECONDS = 45
    response = Mock()
    response.read.return_value = json.dumps(
        {"choices": [{"message": {"content": json.dumps(valid_analysis())}}]}
    ).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    analyzer = OpenAICompatibleReportAnalyzer(
        "http://local-model.test/v1/chat/completions", "local-finance-model"
    )

    with patch("budget.report_analysis.urllib.request.urlopen", return_value=response) as urlopen:
        result = analyzer.analyze({"totals": {"spending": "280.00", "groceries": "180.00"}})

    request = urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert payload["model"] == "local-finance-model"
    assert payload["json_schema"] == payload["response_format"]["schema"]
    assert payload["response_format"]["schema"]["additionalProperties"] is False
    assert "OUTPUT JSON SCHEMA" in payload["messages"][1]["content"]
    assert "Never describe income as lost" in payload["messages"][1]["content"]
    assert "280.00" in payload["messages"][1]["content"]
    assert result.bottom_line_up_front.expense_summary.startswith("Expenses remain below income")
    assert result.positive_trends == ("Savings received a positive net inflow.",)
    assert result.neutral_trends == ("Bills were unchanged.",)
    assert result.model == "local-finance-model"


@pytest.mark.parametrize(
    "raw",
    [
        {},
        valid_analysis(overview=""),
        valid_analysis(negative_trends=[123]),
        valid_analysis(bottom_line_up_front={"concerns": "Missing two sections"}),
        {**valid_analysis(), "unexpected": "field"},
    ],
)
def test_report_analysis_rejects_malformed_model_output(raw):
    with pytest.raises(ReportAnalysisError):
        parse_report_analysis(raw)


def test_report_analysis_safely_caps_verbose_trend_findings():
    verbose = "A" * 550
    raw = valid_analysis(
        negative_trends=[verbose, "", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh"],
        actionable_options=["B" * 750],
    )

    analysis = parse_report_analysis(raw)

    assert len(analysis.negative_trends) == 5
    assert len(analysis.negative_trends[0]) == 500
    assert analysis.negative_trends[0].endswith("…")
    assert "Seventh" not in analysis.negative_trends
    assert len(analysis.actionable_options[0]) == 700
    assert analysis.actionable_options[0].endswith("…")


@pytest.mark.django_db
def test_report_page_generates_local_analysis_only_when_requested(client):
    home = Household.objects.create()
    user = get_user_model().objects.create_user("report-user")
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    analyzer = Mock()
    analyzer.analyze.return_value = parse_report_analysis(valid_analysis(), model="local-model")

    with (
        patch("budget.analysis_jobs.configured_report_analyzer", return_value=analyzer),
        patch("budget.analysis_jobs.spawn", lambda target, *args: target(*args)),
    ):
        initial = client.get(reverse("reports"))
        generated = client.post(reverse("reports_analysis_start"), follow=True)

    assert analyzer.analyze.call_count == 1
    assert b"Generate AI analysis" in initial.content
    assert b"Overall spending remained manageable" not in initial.content
    assert b"Overall spending remained manageable" in generated.content
    assert b"Bottom line up front" in generated.content
    assert b"Current concerns" in generated.content
    assert b"Expense summary" in generated.content
    assert b"Recommended way ahead" in generated.content
    assert b"Neutral patterns" in generated.content
    assert b"Actionable options" in generated.content
    assert generated.content.index(b"Bottom line up front") < generated.content.index(
        b"Spending by category"
    )
    assert b"Spending by category" in generated.content
    assert b"Category breakdown" in generated.content
    assert b"Money moved by account" not in generated.content
    analysis_payload = analyzer.analyze.call_args.args[0]
    assert len(analysis_payload["historical_periods"]) == 6
    assert analysis_payload["outlook"]["savings_health"] == "held steady"
    assert "nonessential_categories" in analysis_payload["outlook"]
