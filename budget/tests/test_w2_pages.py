from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from budget.models import (
    Account,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    PayPeriod,
    RecurringBill,
    Transfer,
)
from budget.outlook import _chart_point_rows, chart_scale_max

D = Decimal
pytestmark = pytest.mark.django_db


@pytest.fixture
def env(client):
    user = get_user_model().objects.create_user("w2", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spend = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    cat = Category.objects.create(household=home, name="Food", type="spending")
    expense = Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 1),
        amount="10.00",
        account=spend,
        category=cat,
        merchant="Shop",
    )
    return {"home": home, "user": user, "spend": spend, "bills": bills, "cat": cat, "e": expense}


def test_filter_controls_share_layout(client, env):
    html = client.get(reverse("transactions")).content.decode()
    assert '<label>Account<select name="account">' in html
    assert '<label>Type<select name="type">' in html
    css = open("budget/static/budget/transactions.css").read()
    assert ".transaction-filters select" in css and "min-block-size: 46px" in css


def test_history_has_no_mark_cleared_buttons_but_shows_status(client, env):
    Transfer.objects.create(
        household=env["home"],
        created_by=env["user"],
        date=date(2026, 8, 2),
        amount="5.00",
        source=env["spend"],
        destination=env["bills"],
    )
    for params in ({}, {"account": env["spend"].pk}):
        html = client.get(reverse("transactions"), params).content.decode()
        assert "Mark cleared" not in html and "Mark not cleared" not in html
        assert "cleared-toggle" not in html
        assert "Not cleared" in html


def test_reconcile_page_can_still_tick_items(client, env):
    url = reverse("account_reconcile", args=[env["spend"].pk])
    html = client.post(
        url, {"statement_date": "2026-08-10", "statement_balance": "-10.00", "action": "compare"}
    ).content.decode()
    assert 'type="checkbox" name="clear"' in html
    client.post(
        url,
        {
            "statement_date": "2026-08-10",
            "statement_balance": "-10.00",
            "action": "reconcile",
            "clear": [f"expense:{env['e'].pk}"],
        },
    )
    env["e"].refresh_from_db()
    assert env["e"].cleared_at is not None


def test_debt_form_bills_is_compact_dropdown(client, env):
    for i in range(3):
        RecurringBill.objects.create(
            household=env["home"],
            name=f"Bill {i}",
            default_amount="10.00",
            due_day=5,
            category=env["cat"],
            account=env["bills"],
            effective_start=date(2026, 1, 1),
        )
    html = client.get(reverse("debt_add")).content.decode()
    assert '<details class="dropdown"' in html and "Choose bills (0 chosen)" in html
    assert "dropdown-panel" in html and "Bill 2" in html
    assert "max-height:16rem" in open("budget/static/budget/debts.css").read()


def test_chart_scale_covers_largest_value():
    values = [D("0.00"), D("5.00"), D("480.00"), D("12.00")]
    assert chart_scale_max(values) > max(values)
    assert chart_scale_max(values, peak=D("900.00")) > D("900.00")
    assert chart_scale_max([]) == D("1.00") and chart_scale_max([D("0")]) == D("1.00")
    for point in _chart_point_rows(values):
        assert D(point["y"]) > 0  # the spike is not cut off at the top
        assert D(point["y"]) <= 220


def test_outlook_label_and_budgets_wording(client, env):
    today = timezone.localdate()
    page = client.get(reverse("reports")).content.decode()
    assert "Spending after refunds" in page and "Net of refunds" not in page
    PayPeriod.objects.create(
        household=env["home"],
        start_date=today - timedelta(days=3),
        end_date=today + timedelta(days=10),
        nominal_payday=today,
        expected_deposit_date=today,
    )
    budgets = client.get(reverse("budgets")).content.decode()
    assert "Category budgets (optional)" in budgets
    assert "Only categories you give an amount are tracked; leave the rest blank." in budgets
