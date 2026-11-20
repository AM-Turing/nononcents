from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from budget import services
from budget.models import (
    Account,
    BillOccurrence,
    Debt,
    Expense,
    Household,
    PayPeriod,
    StatementLine,
)


@pytest.mark.django_db
def test_demo_profile_refuses_outside_browser_test_mode(monkeypatch):
    monkeypatch.delenv("BROWSER_TEST_MODE", raising=False)
    with pytest.raises(CommandError, match="--yes"):
        call_command("seed_demo_profile")
    assert not Household.objects.exists()


@pytest.mark.django_db
def test_demo_profile_is_complete_and_replaced_on_rerun(monkeypatch):
    monkeypatch.setenv("BROWSER_TEST_MODE", "true")
    call_command("seed_demo_profile")
    call_command("seed_demo_profile")
    home = Household.objects.get()
    assert home.name == "Demo Household"
    assert set(Account.objects.filter(household=home).values_list("name", flat=True)) == {
        "Spending",
        "Bills",
        "Savings",
    }
    assert PayPeriod.objects.filter(household=home).count() >= 8
    assert BillOccurrence.objects.filter(bill__household=home, status="paid").exists()
    assert Expense.objects.filter(household=home, bill_occurrence__isnull=True).count() == 29
    assert Debt.objects.filter(household=home).count() == 2
    assert StatementLine.objects.filter(statement__household=home, status="pending").count() > 2
    for account in Account.objects.filter(household=home):
        assert services.account_balance(account) > Decimal("0")
