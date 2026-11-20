import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from budget.habits import set_category_budget
from budget.models import (
    Account,
    Category,
    Expense,
    HabitView,
    Household,
    HouseholdMembership,
    PayPeriod,
)
from budget.services import create_expense

D = Decimal


@pytest.fixture
def s(db, client):
    user = get_user_model().objects.create_user("habit-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    account = Account.objects.create(household=home, name="Spending", type="spending")
    fun = Category.objects.create(household=home, name="Eating Out", type="spending")
    client.force_login(user)
    return {"user": user, "home": home, "account": account, "fun": fun, "client": client}


def other_household():
    user = get_user_model().objects.create_user("other-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    return home


def period(s):
    today = timezone.localdate()
    return PayPeriod.objects.create(
        household=s["home"],
        start_date=today - timedelta(days=3),
        end_date=today + timedelta(days=10),
        nominal_payday=today - timedelta(days=3),
        expected_deposit_date=today - timedelta(days=3),
    )


def buy(s, amount, merchant, days_ago=0):
    return create_expense(
        household=s["home"],
        actor=s["user"],
        entry_date=timezone.localdate() - timedelta(days=days_ago),
        account=s["account"],
        category=s["fun"],
        amount=amount,
        merchant=merchant,
    )


def form_data(**overrides):
    data = {
        "name": "Coffee watch",
        "small_threshold": "10.00",
        "basis": "month",
        "periods": "3",
        "min_repeat": "2",
        "include_essential": "on",
    }
    data.update(overrides)
    return data


def test_page_renders_with_no_data(s):
    response = s["client"].get(reverse("habits"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Small purchases that add up" in html and "Default" in html
    assert "Cleanup queue" in html


def test_page_with_saved_view_and_data(s):
    view = HabitView.objects.create(
        household=s["home"], name="Coffee", basis="month", periods=2, small_threshold=D("10.00")
    )
    for _ in range(3):
        buy(s, "4.00", "Snackbar")
    html = s["client"].get(reverse("habits")).content.decode()
    assert "Coffee" in html and "Snackbar" in html and "3 purchases" in html
    assert f"?view={view.pk}" in html
    adjusted = s["client"].get(
        reverse("habits"), {"adjust": 1, "small_threshold": "1.00", "basis": "month", "periods": 1}
    )
    assert adjusted.status_code == 200 and "adjusted, not saved" in adjusted.content.decode()
    assert HabitView.objects.count() == 1


def test_other_household_view_is_404(s):
    foreign = HabitView.objects.create(household=other_household(), name="Theirs")
    assert s["client"].get(reverse("habits"), {"view": foreign.pk}).status_code == 404
    assert s["client"].get(reverse("habits"), {"view": "abc"}).status_code == 404


def test_save_edit_delete_view(s):
    groceries = Category.objects.create(household=s["home"], name="Food", type="spending")
    bill_cat = Category.objects.create(household=s["home"], name="Rent", type="bill")
    response = s["client"].get(reverse("habit_view_add"))
    content = response.content.decode()
    assert "Food" in content and "Rent" not in content
    response = s["client"].post(
        reverse("habit_view_add"), form_data(categories=[groceries.pk, bill_cat.pk])
    )
    assert response.status_code == 200  # a bill category is not a valid choice
    response = s["client"].post(reverse("habit_view_add"), form_data(categories=[groceries.pk]))
    view = HabitView.objects.get()
    assert response.status_code == 302 and response["Location"].endswith(f"?view={view.pk}")
    assert view.household == s["home"] and view.created_by == s["user"]
    assert list(view.categories.all()) == [groceries]
    response = s["client"].post(
        reverse("habit_view_edit", args=[view.pk]), form_data(name="Renamed", small_threshold="5")
    )
    assert response.status_code == 302
    view.refresh_from_db()
    assert view.name == "Renamed" and view.small_threshold == D("5.00")
    assert view.categories.count() == 0
    assert s["client"].get(reverse("habit_view_delete", args=[view.pk])).status_code == 405
    s["client"].post(reverse("habit_view_delete", args=[view.pk]))
    assert not HabitView.objects.exists()


def test_view_form_validation_and_duplicate_name(s):
    HabitView.objects.create(household=s["home"], name="Coffee watch")
    for bad in (
        {"name": "Coffee watch"},
        {"small_threshold": "0"},
        {"periods": "25"},
        {"periods": "0"},
        {"min_repeat": "1"},
    ):
        response = s["client"].post(reverse("habit_view_add"), form_data(**bad))
        assert response.status_code == 200, bad
    assert (
        "already have a view"
        in s["client"].post(reverse("habit_view_add"), form_data()).content.decode()
    )
    assert HabitView.objects.count() == 1


def test_other_household_view_cannot_be_edited_or_deleted(s):
    foreign = HabitView.objects.create(household=other_household(), name="Theirs")
    assert s["client"].get(reverse("habit_view_edit", args=[foreign.pk])).status_code == 404
    assert (
        s["client"].post(reverse("habit_view_edit", args=[foreign.pk]), form_data()).status_code
        == 404
    )
    assert s["client"].post(reverse("habit_view_delete", args=[foreign.pk])).status_code == 404
    assert HabitView.objects.filter(pk=foreign.pk).exists()


def test_unify_renames_and_messages(s):
    buy(s, "5.00", "Snackbar")
    buy(s, "5.00", "snackbar")
    buy(s, "5.00", "Snackbar")
    html = s["client"].get(reverse("habits")).content.decode()
    assert "Use this name" in html
    response = s["client"].post(
        reverse("habit_unify"), {"key": "snackbar", "name": "Snackbar"}, follow=True
    )
    assert "Renamed 1 entries (0 in locked periods left as they were)." in response.content.decode()
    assert set(Expense.objects.values_list("merchant", flat=True)) == {"Snackbar"}
    bad = s["client"].post(
        reverse("habit_unify"), {"key": "snackbar", "name": "Other"}, follow=True
    )
    assert "Choose one of the spellings" in bad.content.decode()
    assert s["client"].get(reverse("habit_unify")).status_code == 405


def test_blank_merchant_shows_edit_link(s):
    expense = buy(s, "5.00", "")
    html = s["client"].get(reverse("habits")).content.decode()
    assert reverse("transaction_edit", args=["expense", expense.pk]) in html


def test_budget_card_shows_negative_remaining(s):
    period(s)
    set_category_budget(
        s["home"], s["user"], s["fun"], "20.00", timezone.localdate() - timedelta(days=30)
    )
    buy(s, "25.50", "Pizza")
    html = s["client"].get(reverse("habits")).content.decode()
    assert "Category budgets this pay period" in html
    assert "−$5.50" in html and "<meter" in html
    assert reverse("budgets") in html


def test_budget_card_hidden_without_period(s):
    html = s["client"].get(reverse("habits")).content.decode()
    assert "Category budgets this pay period" not in html


def test_navigation_has_habits_and_debts(s):
    html = s["client"].get(reverse("habits")).content.decode()
    sidebar = html.split('aria-label="Primary"', 1)[1].split("</nav>", 1)[0]
    more = html.split('class="more-panel"', 1)[1].split("</details>", 1)[0]
    for part in (sidebar, more):
        assert re.search(r'href="/habits/"[^>]*>Habits</a>', part)
        assert 'href="/debts/"' in part and ">Debts</a>" in part
    assert re.search(r'href="/habits/" aria-current="page">Habits', sidebar)
    assert re.search(r'href="/habits/" aria-current="page">Habits', more)
    assert 'aria-current="page">More' in html
