from budget.report_analysis import enforce_report_grounding, parse_report_analysis

GROUNDING_SUMMARY = {
    "as_of": "2026-09-10",
    "totals": {"selected": {"spending": "1234.56", "bills": "800.00"}},
    "analysis_context": {"as_of_date": "2026-09-10", "selected_period_status": "complete"},
    "trend_evidence": {
        "income": {
            "actual_through_as_of": "5000.00",
            "scheduled_remaining": "0.00",
            "projected_full_period": "5000.00",
            "completed_history_average": "5000.00",
        },
        "non_bill_spending": {
            "projected_full_period": "1234.56",
            "budget_target": "2000.00",
            "completed_history_average": "1600.00",
        },
        "bills": {"projected_full_period": "800.00"},
        "savings": {
            "adjusted_actual_change": "200.00",
            "completed_history_average_adjusted_change": "150.00",
        },
    },
    "outlook": {"forecast": {"forecast_surplus": "2965.44"}, "cut_scenarios": []},
    "categories": [],
}


def analysis_with(**overrides):
    value = {
        "bottom_line_up_front": {
            "concerns": "Groceries are steady.",
            "expense_summary": "Expenses remain below income.",
            "recommended_way_ahead": "Keep the current plan.",
        },
        "overview": "Overall spending remained manageable.",
        "spending_trends": "Groceries were steady.",
        "negative_trends": [],
        "neutral_trends": ["Bills were unchanged."],
        "positive_trends": [],
        "actionable_options": [],
        "account_flow_summary": "Income funded Spending.",
        "conclusion": "Keep going.",
    }
    value.update(overrides)
    return parse_report_analysis(value, model="local")


def test_grounding_rejects_narrative_citing_an_unknown_amount():
    result = enforce_report_grounding(
        analysis_with(overview="Spending reached $9,999.99 this period."), GROUNDING_SUMMARY
    )

    assert result.model == "local (grounded)"
    assert "9,999.99" not in result.overview


def test_grounding_accepts_payload_amounts_and_whole_dollar_rounding():
    result = enforce_report_grounding(
        analysis_with(
            overview="Spending was $1,234.56, about $1,235, against bills of $800.",
            negative_trends=["Income of $5,000.00 arrived."],
        ),
        GROUNDING_SUMMARY,
    )

    assert result.model == "local"
    assert result.overview.startswith("Spending was $1,234.56")


def test_grounding_does_not_accept_percentages_as_dollar_amounts():
    summary = {**GROUNDING_SUMMARY, "categories": [{"name": "Food", "share_percent": "37.5"}]}

    result = enforce_report_grounding(analysis_with(overview="Food cost $37.50."), summary)

    assert result.model == "local (grounded)"
