from decimal import Decimal as D

import pytest

from budget.outlook import pie_slices


def rows(*amounts):
    return [
        {"name": f"C{i}", "amount": D(a), "color": "#123456", "share": D("0")}
        for i, a in enumerate(amounts)
    ]


def test_no_total_no_slices():
    assert pie_slices(rows("0"), D("0")) == []


def test_single_category_is_a_full_circle():
    (only,) = pie_slices(rows("12.50"), D("12.50"))
    assert only["d"].count(" A ") == 2 and only["d"].startswith("M 100.000 4.000")


def test_two_halves_meet_at_the_bottom_and_back_at_the_top():
    first, second = pie_slices(rows("50", "50"), D("100"))
    assert first["d"] == "M 100.000 100.000 L 100.000 4.000 A 96.0 96.0 0 0 1 100.000 196.000 Z"
    assert second["d"].startswith("M 100.000 100.000 L 100.000 196.000")
    assert second["d"].endswith("100.000 4.000 Z")


def test_zero_and_refund_categories_get_no_slice():
    slices = pie_slices(rows("75", "25", "0", "-5"), D("100"))
    assert [s["name"] for s in slices] == ["C0", "C1"]
    # 270 degrees needs the large-arc flag; 90 degrees does not.
    assert "A 96.0 96.0 0 1 1" in slices[0]["d"]
    assert "A 96.0 96.0 0 0 1" in slices[1]["d"]


@pytest.mark.django_db
def test_homepage_spending_chart_has_one_slice_per_legend_row():
    from datetime import date

    from budget.models import Account, Category, Expense, Household, PayPeriod
    from budget.views import spending_chart_data

    home = Household.objects.create(name="Home")
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    period = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 11, 1),
        expected_deposit_date=date(2026, 11, 1),
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 14),
    )
    for name, amount in (("Groceries", "75"), ("Gas", "25")):
        category = Category.objects.create(household=home, name=name, type="spending")
        Expense.objects.create(
            household=home,
            date=date(2026, 11, 3),
            amount=D(amount),
            account=spending,
            category=category,
            merchant=name,
        )
    chart = spending_chart_data(home, period)
    assert [s["name"] for s in chart["slices"]] == [i["name"] for i in chart["items"]]
    assert [s["share"] for s in chart["slices"]] == [D("75"), D("25")]
    assert "A 96.0 96.0 0 1 1" in chart["slices"][0]["d"]
