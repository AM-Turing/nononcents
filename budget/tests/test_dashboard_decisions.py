from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from budget.models import (
    Account,
    ApplicationSetting,
    BillOccurrence,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    PayPeriod,
    PayPeriodBudget,
    RecurringBill,
)
from budget.views import dashboard_overview_title


@pytest.fixture
def dashboard_setup(client, db):
    user = get_user_model().objects.create_user("dashboard-user", password="safe-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spending = Account.objects.create(
        household=home, name="Spending", type="spending", opening_balance="180.00"
    )
    bills = Account.objects.create(
        household=home, name="Bills", type="bills", opening_balance="100.00"
    )
    savings = Account.objects.create(household=home, name="Savings", type="savings")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    utilities = Category.objects.create(household=home, name="Utilities", type="bill")
    today = timezone.localdate()
    period = PayPeriod.objects.create(
        household=home,
        nominal_payday=today,
        expected_deposit_date=today,
        start_date=today - timedelta(days=3),
        end_date=today + timedelta(days=10),
    )
    internet = RecurringBill.objects.create(
        household=home,
        name="Internet",
        default_amount="500.00",
        due_day=today.day,
        category=utilities,
        account=bills,
        effective_start=today.replace(day=1),
    )
    BillOccurrence.objects.create(
        bill=internet,
        due_date=today,
        expected_amount="500.00",
        pay_period=period,
    )
    Expense.objects.create(
        household=home,
        created_by=user,
        date=today,
        amount="30.00",
        account=spending,
        category=groceries,
        merchant="Market",
    )
    Expense.objects.create(
        household=home,
        created_by=user,
        date=today,
        amount="10.00",
        account=bills,
        category=utilities,
    )
    Expense.objects.create(
        household=home,
        created_by=user,
        date=today,
        amount="20.00",
        account=spending,
        category=utilities,
    )
    return client, spending, bills, savings


@pytest.mark.django_db
def test_dashboard_shows_low_spending_balance_chart_and_bills_shortage(dashboard_setup):
    client, _, _, _ = dashboard_setup
    response = client.get(reverse("dashboard"))
    content = response.content.decode()

    assert response.context["spending_balance"] == Decimal("130.00")
    assert response.context["bills_shortage"] == Decimal("410.00")
    assert response.context["spending_chart"]["total"] == Decimal("30.00")
    assert response.context["spending_chart"]["items"][0]["name"] == "Groceries"
    assert "<h1>My Budget Overview</h1>" in content
    assert "Low balance · below $200" in content
    assert "Bills funding shortage: $410.00" in content
    assert "Acknowledge" in content and "Add transfer" in content
    assert "1 bill needs confirmation" in content
    assert "Scan receipt" not in content


@pytest.mark.parametrize(
    ("profile_name", "expected"),
    [
        ("My Household", "My Budget Overview"),
        ("Test Household", "Test’s Budget Overview"),
        ("Alex Budget", "Alex’s Budget Overview"),
        ("Alex's Budget", "Alex’s Budget Overview"),
        ("Alex’s Budget", "Alex’s Budget Overview"),
        ("Smith Household", "Smith’s Budget Overview"),
    ],
)
def test_dashboard_overview_title_uses_profile_name(profile_name, expected):
    assert dashboard_overview_title(Household(name=profile_name)) == expected


@pytest.mark.django_db
def test_dashboard_uses_profile_alert_thresholds_and_toggles(dashboard_setup):
    client, spending, _, _ = dashboard_setup
    ApplicationSetting.objects.create(
        household=spending.household,
        key="alert_settings",
        value={
            "spending_low_balance_enabled": True,
            "spending_low_balance_threshold": "100.00",
            "bills_shortage_enabled": False,
            "bill_confirmation_enabled": False,
        },
    )

    response = client.get(reverse("dashboard"))
    content = response.content.decode()

    assert "Low balance" not in content
    assert "Bills funding shortage" not in content
    assert "needs confirmation" not in content


@pytest.mark.django_db
def test_dashboard_compares_spending_and_current_and_next_period_bills(dashboard_setup):
    client, _, bills, _ = dashboard_setup
    current = PayPeriod.objects.get(start_date__lte=timezone.localdate())
    PayPeriodBudget.objects.create(pay_period=current, spending_target=Decimal("1400.00"))
    for days_ago, target in ((40, "1200.00"), (70, "1600.00")):
        historical = PayPeriod.objects.create(
            household=bills.household,
            nominal_payday=current.start_date - timedelta(days=days_ago),
            expected_deposit_date=current.start_date - timedelta(days=days_ago),
            start_date=current.start_date - timedelta(days=days_ago),
            end_date=current.start_date - timedelta(days=days_ago - 13),
        )
        PayPeriodBudget.objects.create(pay_period=historical, spending_target=Decimal(target))
    next_period = PayPeriod.objects.create(
        household=bills.household,
        nominal_payday=current.end_date + timedelta(days=1),
        expected_deposit_date=current.end_date + timedelta(days=1),
        start_date=current.end_date + timedelta(days=1),
        end_date=current.end_date + timedelta(days=15),
    )
    PayPeriodBudget.objects.create(pay_period=next_period, expected_paycheck=Decimal("100.00"))
    category = Category.objects.get(name="Utilities")
    next_bill = RecurringBill.objects.create(
        household=bills.household,
        name="Next period phone",
        default_amount=Decimal("225.00"),
        due_day=next_period.start_date.day,
        category=category,
        account=bills,
        effective_start=next_period.start_date.replace(day=1),
    )
    BillOccurrence.objects.create(
        bill=next_bill,
        due_date=next_period.start_date,
        expected_amount=Decimal("225.00"),
        pay_period=next_period,
    )

    response = client.get(reverse("dashboard"))
    content = response.content.decode()

    assert response.context["spending_budget_target"] == Decimal("1400.00")
    assert response.context["spent"] == Decimal("30.00")
    assert response.context["spending_remaining"] == Decimal("1370.00")
    assert response.context["spending_used_percent"] == Decimal("2.14")
    assert response.context["current_bills_total"] == Decimal("500.00")
    assert response.context["bills_remaining"] == Decimal("500.00")
    assert response.context["bills_paid"] == Decimal("0.00")
    assert response.context["bills_paid_percent"] == Decimal("0.00")
    forecast = response.context["forecast"]
    assert forecast["available"]
    assert forecast["next_period_bills"]["total"] == Decimal("225.00")
    assert forecast["expected_paycheck"]["amount"] == Decimal("100.00")
    # Last three completed periods spent nothing, so anticipated spending is 0, not a target.
    assert forecast["anticipated_spending"]["amount"] == Decimal("0.00")
    assert forecast["hold_back"]["amount"] == Decimal("125.00")
    # One reconciling card per account replaces the separate comparison cards.
    assert "Spending account" in content and "Bills account" in content
    # Each card leads with its result; the calculation sits in a dropdown underneath.
    assert '<p class="headline-label">Safe to spend</p>' in content
    assert '<p class="headline-label">Remaining after Bills this period</p>' in content
    assert '<p class="headline-label">Remaining after bills next period</p>' in content
    assert "Left over" not in content and "Left after this period" not in content
    # Carry-over: 225 next bills + 0 expected spending - 100 paycheck = 125 needed.
    assert "Carry over to next pay period" in content
    assert '<p class="headline-label">Needs to carry over</p>' in content
    carry = forecast["carry_over"]
    assert carry["needed"] == Decimal("125.00")
    assert carry["from_bills"] + carry["from_spending"] == Decimal("125.00")
    assert "Next pay period" in content and "− expected spending" in content  # no target set
    assert "Budged vs. Actual" not in content and "Hold back" not in content
    assert "Until payday" in content


@pytest.mark.django_db
def test_dashboard_does_not_count_removed_bill_as_needing_confirmation(dashboard_setup):
    client, _, bills, _ = dashboard_setup
    active_occurrence = BillOccurrence.objects.get(bill__name="Internet")
    active_occurrence.status = BillOccurrence.Status.CONFIRMED
    active_occurrence.save(update_fields=["status"])
    removed = RecurringBill.objects.create(
        household=bills.household,
        name="Removed subscription",
        default_amount="25.00",
        due_day=active_occurrence.due_date.day,
        category=active_occurrence.bill.category,
        account=bills,
        effective_start=active_occurrence.due_date.replace(month=1, day=1),
        effective_end=active_occurrence.due_date.replace(day=1) - timedelta(days=1),
        active=False,
    )
    BillOccurrence.objects.create(
        bill=removed,
        due_date=active_occurrence.due_date,
        expected_amount="25.00",
        pay_period=active_occurrence.pay_period,
    )

    response = client.get(reverse("dashboard"))

    assert response.context["unconfirmed"] == 0
    assert "Action needed:" not in response.content.decode()
    assert "Removed subscription" not in response.content.decode()


@pytest.mark.django_db
def test_bills_shortage_can_be_acknowledged_for_the_current_state(dashboard_setup):
    client, _, _, _ = dashboard_setup
    response = client.get(reverse("dashboard"))
    key = response.context["bills_alert_key"]

    client.post(reverse("acknowledge_bills_shortage"), {"alert_key": key})
    response = client.get(reverse("dashboard"))

    assert response.context["bills_shortage"] > 0
    assert response.context["bills_shortage_alert"] is False


@pytest.mark.django_db
def test_move_money_to_bills_prefills_destination(dashboard_setup):
    client, _, bills, _ = dashboard_setup
    response = client.get(reverse("transfer_add"), {"to": "bills"})
    assert response.context["form"].initial["destination"] == bills


@pytest.mark.django_db
def test_display_mode_replaces_appearance_label(dashboard_setup):
    client, _, _, _ = dashboard_setup
    content = client.get(reverse("dashboard")).content.decode()
    assert "Display Mode" in content
    assert ">Appearance<" not in content
