from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget.models import Household, HouseholdMembership, PayPeriod, PayPeriodBudget
from budget.pay_periods import create_period_budget


def _login(client):
    user = get_user_model().objects.create_user("budget-user", password="safe-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    return home


def _period(home, start, end, **extra):
    return PayPeriod.objects.create(
        household=home,
        nominal_payday=start,
        expected_deposit_date=start,
        start_date=start,
        end_date=end,
        **extra,
    )


@pytest.mark.django_db
def test_budget_form_only_offers_fields_that_affect_calculations(client):
    home = _login(client)
    period = _period(home, date(2026, 8, 1), date(2026, 8, 15))
    budget = PayPeriodBudget.objects.create(
        pay_period=period, bills_cushion=Decimal("50.00"), weekly_target=Decimal("700.00")
    )

    response = client.get(reverse("budgets"), {"period": period.pk})
    assert response.status_code == 200
    assert list(response.context["form"].fields) == ["spending_target", "expected_paycheck"]

    saved = client.post(
        reverse("budgets") + f"?period={period.pk}",
        {"spending_target": "2100.00", "expected_paycheck": "4500.25"},
    )
    assert saved.status_code == 302
    budget.refresh_from_db()
    assert budget.spending_target == Decimal("2100.00")
    assert budget.expected_paycheck == Decimal("4500.25")
    # Legacy columns keep their stored history.
    assert budget.bills_cushion == Decimal("50.00")
    assert budget.weekly_target == Decimal("700.00")


@pytest.mark.django_db
def test_budget_rejects_negative_amounts(client):
    home = _login(client)
    period = _period(home, date(2026, 8, 1), date(2026, 8, 15))
    PayPeriodBudget.objects.create(pay_period=period)
    response = client.post(
        reverse("budgets") + f"?period={period.pk}",
        {"spending_target": "-1.00", "expected_paycheck": "0"},
    )
    assert response.status_code == 200
    assert PayPeriodBudget.objects.get(pay_period=period).spending_target == Decimal("0.00")


@pytest.mark.django_db
def test_new_period_budget_copies_previous_and_has_no_hard_coded_defaults():
    home = Household.objects.create()
    first = _period(home, date(2026, 8, 1), date(2026, 8, 14))
    blank = create_period_budget(first)
    assert blank.spending_target == Decimal("0.00")
    assert blank.expected_paycheck == Decimal("0.00")

    blank.spending_target = Decimal("2200.00")
    blank.expected_paycheck = Decimal("4688.72")
    blank.save()
    second = _period(home, date(2026, 8, 15), date(2026, 8, 31))
    copied = create_period_budget(second)
    assert copied.spending_target == Decimal("2200.00")
    assert copied.expected_paycheck == Decimal("4688.72")
