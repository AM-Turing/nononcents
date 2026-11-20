from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from budget.models import (
    Account,
    AuditEvent,
    Category,
    CategoryBudget,
    Debt,
    DebtBalance,
    Household,
    HouseholdMembership,
    PayPeriod,
    RecurringBill,
)

D = Decimal


@pytest.fixture
def env(client, db):
    user = get_user_model().objects.create_user("dp-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    loans = Category.objects.create(household=home, name="Loans", type="bill")
    food = Category.objects.create(household=home, name="Food", type="spending")
    return {"home": home, "bills": bills, "loans": loans, "food": food, "user": user}


def _debt(home, name="Card", apr="24", minimum="50.00"):
    return Debt.objects.create(household=home, name=name, apr=D(apr), minimum_payment=D(minimum))


def _bill(env, name="Car Loan", amount="200.00", debt=None):
    return RecurringBill.objects.create(
        household=env["home"],
        name=name,
        default_amount=D(amount),
        due_day=5,
        category=env["loans"],
        account=env["bills"],
        effective_start=timezone.localdate() - timedelta(days=400),
        debt=debt,
    )


def _other_household():
    other = Household.objects.create()
    return other


def test_empty_page(client, env):
    response = client.get(reverse("debts"))
    assert response.status_code == 200
    assert "No debts yet" in response.content.decode()


def test_page_with_data(client, env):
    home = env["home"]
    card = _debt(home, "Card", "24", "50.00")
    DebtBalance.objects.create(debt=card, as_of=timezone.localdate(), balance=D("5000.00"))
    loan = _debt(home, "Loan", "6", "100.00")
    DebtBalance.objects.create(debt=loan, as_of=timezone.localdate(), balance=D("3000.00"))
    html = client.get(reverse("debts")).content.decode()
    assert "entered" in html and "interest" in html  # formula text
    assert "never pays off" in html
    assert "Paid off in" in html
    html = client.get(reverse("debts"), {"extra": "100"}).content.decode()
    assert "debt-free in" in html  # baseline never pays the card off; the extra does


def test_plan_comparison_sooner_and_less_interest(client, env):
    loan = _debt(env["home"], "Loan", "6", "100.00")
    DebtBalance.objects.create(debt=loan, as_of=timezone.localdate(), balance=D("3000.00"))
    html = client.get(reverse("debts"), {"extra": "100"}).content.decode()
    assert "sooner" in html and "less interest" in html


def test_no_balance_prompt_and_bad_extra(client, env):
    _debt(env["home"])
    html = client.get(reverse("debts"), {"extra": "-5"}).content.decode()
    assert "No balance entered yet" in html
    assert "ignored" in html


def test_add_edit_links_and_unlinks_bills(client, env):
    home = env["home"]
    other = _debt(home, "Other")
    first = _bill(env, "First", debt=other)
    second = _bill(env, "Second")
    response = client.post(
        reverse("debt_add"),
        {
            "name": "Sedan",
            "kind": "loan",
            "apr": "7.5",
            "minimum_payment": "50",
            "notes": "",
            "bills": [first.pk, second.pk],
        },
    )
    assert response.status_code == 302
    sedan = Debt.objects.get(name="Sedan")
    first.refresh_from_db()
    assert first.debt == sedan  # moved from Other
    assert set(sedan.bills.all()) == {first, second}
    assert AuditEvent.objects.filter(record_type="budget.Debt", action="create").exists()
    client.post(
        reverse("debt_edit", args=[sedan.pk]),
        {
            "name": "Sedan",
            "kind": "loan",
            "apr": "7.5",
            "minimum_payment": "50",
            "notes": "",
            "bills": [second.pk],
        },
    )
    first.refresh_from_db()
    assert first.debt is None and list(sedan.bills.all()) == [second]


def test_duplicate_name_and_apr_range(client, env):
    _debt(env["home"], "Card")
    body = {"name": "Card", "kind": "loan", "apr": "7", "minimum_payment": "5"}
    assert "already have a debt" in client.post(reverse("debt_add"), body).content.decode()
    body = {"name": "New", "kind": "loan", "apr": "101", "minimum_payment": "5"}
    assert client.post(reverse("debt_add"), body).status_code == 200
    assert not Debt.objects.filter(name="New").exists()


def test_other_household_is_404_everywhere(client, env):
    other = _debt(_other_household(), "Theirs")
    for name in ("debt_edit", "debt_balance_add"):
        assert client.get(reverse(name, args=[other.pk])).status_code == 404
    assert client.post(reverse("debt_balance_add", args=[other.pk]), {}).status_code == 404
    assert client.post(reverse("debt_archive", args=[other.pk])).status_code == 404
    other.refresh_from_db()
    assert other.archived_at is None


def test_balance_same_day_updates_and_future_refused(client, env):
    debt = _debt(env["home"])
    today = timezone.localdate()
    url = reverse("debt_balance_add", args=[debt.pk])
    client.post(url, {"as_of": today.isoformat(), "balance": "100.00", "notes": "a"})
    client.post(url, {"as_of": today.isoformat(), "balance": "90.00", "notes": "b"})
    row = DebtBalance.objects.get(debt=debt)
    assert row.balance == D("90.00") and row.notes == "b"
    assert AuditEvent.objects.filter(record_type="budget.DebtBalance", action="update").exists()
    future = (today + timedelta(days=1)).isoformat()
    response = client.post(url, {"as_of": future, "balance": "5.00"})
    assert "future" in response.content.decode()
    assert DebtBalance.objects.filter(debt=debt).count() == 1


def test_archive_and_restore(client, env):
    debt = _debt(env["home"])
    client.post(reverse("debt_archive", args=[debt.pk]))
    debt.refresh_from_db()
    assert debt.archived_at is not None
    html = client.get(reverse("debts")).content.decode()
    assert "Archived debts" in html and "Restore" in html
    client.post(reverse("debt_archive", args=[debt.pk]), {"restore": "1"})
    debt.refresh_from_db()
    assert debt.archived_at is None


def test_category_budget_via_budgets_page(client, env):
    today = timezone.localdate()
    PayPeriod.objects.create(
        household=env["home"],
        start_date=today - timedelta(days=3),
        end_date=today + timedelta(days=10),
        nominal_payday=today,
        expected_deposit_date=today,
    )
    page = client.get(reverse("budgets")).content.decode()
    assert "Category budgets (optional)" in page and "Food" in page
    url = reverse("category_budget_set")
    client.post(
        url,
        {
            "category": env["food"].pk,
            "amount": "250.00",
            "effective_from": today.isoformat(),
        },
    )
    assert CategoryBudget.objects.get(category=env["food"]).amount == D("250.00")
    earlier = (today - timedelta(days=30)).isoformat()
    response = client.post(
        url,
        {"category": env["food"].pk, "amount": "100.00", "effective_from": earlier},
        follow=True,
    )
    assert "later budget" in response.content.decode()
    assert CategoryBudget.objects.filter(category=env["food"]).count() == 1


def test_category_budget_refuses_non_spending(client, env):
    response = client.post(
        reverse("category_budget_set"),
        {
            "category": env["loans"].pk,
            "amount": "10.00",
            "effective_from": timezone.localdate().isoformat(),
        },
        follow=True,
    )
    assert response.status_code == 200
    assert not CategoryBudget.objects.exists()
