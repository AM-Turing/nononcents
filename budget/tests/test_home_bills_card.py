"""Home: the Bills account card (balance, bills left) and the Bill Account Projections card."""

import re
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from budget.models import (
    Account,
    BillOccurrence,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    PayPeriod,
    PayPeriodBudget,
    RecurringBill,
)

TODAY = date(2026, 11, 17)
D = Decimal


def _card(content, heading_id):
    match = re.search(rf'<article[^>]*aria-labelledby="{heading_id}".*?</article>', content, re.S)
    assert match, heading_id
    return re.sub(r"\s+", " ", match.group(0))


@pytest.fixture
def home_page(client, db, monkeypatch):
    monkeypatch.setattr(timezone, "localdate", lambda: TODAY)
    user = get_user_model().objects.create_user("bills-card", password="safe-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spending = Account.objects.create(
        household=home, name="Spending", type="spending", opening_balance="500.00"
    )
    bills = Account.objects.create(
        household=home, name="Bills", type="bills", opening_balance="1000.00"
    )
    category = Category.objects.create(household=home, name="Utilities", type="bill")
    for start, end, budget in (
        (date(2026, 11, 10), date(2026, 11, 29), {}),
        (
            date(2026, 11, 30),
            date(2026, 12, 14),
            {"expected_paycheck": "2000.00", "spending_target": "300.00"},
        ),
    ):
        period = PayPeriod.objects.create(
            household=home,
            nominal_payday=start,
            expected_deposit_date=start,
            start_date=start,
            end_date=end,
        )
        PayPeriodBudget.objects.create(pay_period=period, **budget)

    def bill(name, amount, account, due):
        item = RecurringBill.objects.create(
            household=home,
            name=name,
            default_amount=D(amount),
            due_day=due.day,
            category=category,
            account=account,
            effective_start=date(2026, 1, 1),
        )
        return BillOccurrence.objects.create(bill=item, due_date=due, expected_amount=D(amount))

    bill("Rent", "600.00", bills, date(2026, 11, 20))  # unpaid, from Bills
    bill("Water", "80.00", bills, date(2026, 11, 25))  # unpaid, from Bills
    bill("Streaming", "15.00", spending, date(2026, 11, 22))  # unpaid, from Spending: not counted
    bill("Insurance", "200.00", bills, date(2026, 12, 5))  # next period
    internet = bill("Internet", "60.00", bills, date(2026, 11, 12))
    internet.status = BillOccurrence.Status.PAID
    internet.save()
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 11, 12),
        amount=D("60.00"),
        account=bills,
        category=category,
        bill_occurrence=internet,
    )
    return client


@pytest.mark.django_db
def test_bills_account_card_shows_balance_and_bills_still_to_pay(home_page):
    content = home_page.get(reverse("dashboard")).content.decode()
    card = _card(content, "bills-card-title")
    assert 'class="label">Bills account</h3>' in card
    # Balance today: 1000.00 opening - 60.00 Internet already paid = 940.00.
    assert "Balance today" in card and "$940.00" in card
    # Still to pay before payday from Bills: Rent 600.00 + Water 80.00 = 680.00 (2 bills);
    # the paid Internet and the Spending-paid Streaming (15.00) are not counted.
    assert "Bills still to pay before payday: $680.00 · 2 bills" in card
    assert "$695.00" not in card
    assert "Reconcile with bank" in card and "Edit paycheck" not in card
    # The card follows Spending and precedes the projections card.
    assert content.index("spending-card-title") < content.index('id="bills-card-title"')
    assert content.index('id="bills-card-title"') < content.index('id="projections-card-title"')
    assert content.index('id="projections-card-title"') < content.index("carry-card-title")


@pytest.mark.django_db
def test_projections_card_keeps_the_same_figures_under_new_labels(home_page):
    response = home_page.get(reverse("dashboard"))
    content = response.content.decode()
    forecast = response.context["forecast"]
    card = _card(content, "projections-card-title")
    assert 'class="label">Bill Account Projections</h3>' in card
    # Remaining Funds in Bills after this period: 940.00 - 680.00 = 260.00.
    assert forecast["bills_surplus"]["amount"] == D("260.00")
    assert 'Remaining Funds in Bills after this period</p><p class="money">$260.00' in card
    # Projected remaining after next period: 260.00 + 2000.00 paycheck - 200.00 Insurance
    # - 300.00 spending target = 1760.00.
    assert forecast["next_bills_covered"]["amount"] == D("1760.00")
    assert 'Projected remaining after next period</p><p class="money">$1,760.00' in card
    assert "Edit paycheck or spending target" in card and "Reconcile with bank" not in card
    assert "Remaining after Bills this period" not in content
    assert "Remaining after bills next period" not in content
