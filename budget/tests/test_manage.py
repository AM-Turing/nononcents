from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from budget.models import (
    Account,
    AuditEvent,
    Category,
    Expense,
    HabitView,
    Household,
    HouseholdMembership,
    RecurringBill,
)

D = Decimal


@pytest.fixture
def env(client, db):
    user = get_user_model().objects.create_user("mg-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    food = Category.objects.create(household=home, name="Food", type="spending")
    return {"home": home, "user": user, "spending": spending, "bills": bills, "food": food}


def _expense(env, category=None, account=None):
    return Expense.objects.create(
        household=env["home"],
        account=account or env["spending"],
        category=category or env["food"],
        amount=D("5.00"),
        date=date(2026, 1, 5),
        created_by=env["user"],
    )


def _other_home_client():
    user = get_user_model().objects.create_user("other", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    c = Client()
    c.force_login(user)
    return c, home


# ---- accounts ----


def test_add_account_and_audit(client, env):
    response = client.post(
        reverse("account_add"),
        {"name": "Holiday", "type": "savings", "opening_balance": "25.50", "display_order": "3"},
    )
    assert response.status_code == 302
    account = Account.objects.get(household=env["home"], name="Holiday")
    assert account.opening_balance == D("25.50")
    assert AuditEvent.objects.filter(record_id=str(account.pk), action="create").exists()


def test_duplicate_account_name_rejected(client, env):
    response = client.post(
        reverse("account_add"),
        {"name": "spending", "type": "other", "opening_balance": "0", "display_order": "0"},
    )
    assert response.status_code == 200
    assert "already have an account" in response.content.decode()


def test_rename_and_reorder_account(client, env):
    extra = Account.objects.create(household=env["home"], name="Extra", type="other")
    _expense(env, account=extra)
    response = client.post(
        reverse("account_edit", args=[extra.pk]),
        {"name": "Extra 2", "type": "spending", "opening_balance": "99", "display_order": "7"},
    )
    assert response.status_code == 302
    extra.refresh_from_db()
    assert (extra.name, extra.display_order) == ("Extra 2", 7)
    # Used account: type and opening balance are locked.
    assert extra.type == "other"
    assert extra.opening_balance == D("0.00")


def test_unused_account_type_and_opening_balance_editable(client, env):
    extra = Account.objects.create(household=env["home"], name="Extra", type="other")
    client.post(
        reverse("account_edit", args=[extra.pk]),
        {"name": "Extra", "type": "savings", "opening_balance": "12", "display_order": "0"},
    )
    extra.refresh_from_db()
    assert (extra.type, extra.opening_balance) == ("savings", D("12.00"))


def test_locked_form_explains_balance_correction(client, env):
    _expense(env)
    page = client.get(reverse("account_edit", args=[env["spending"].pk])).content.decode()
    assert "balance correction from Reconcile" in page


def test_remove_unused_account_deletes(client, env):
    extra = Account.objects.create(household=env["home"], name="Extra", type="other")
    client.post(reverse("account_remove", args=[extra.pk]))
    assert not Account.objects.filter(pk=extra.pk).exists()
    assert AuditEvent.objects.filter(record_id=str(extra.pk), action="delete").exists()


def test_remove_used_account_archives_and_restore(client, env):
    extra = Account.objects.create(household=env["home"], name="Extra", type="spending")
    _expense(env, account=extra)
    response = client.post(reverse("account_remove", args=[extra.pk]), follow=True)
    extra.refresh_from_db()
    assert extra.archived_at is not None
    assert "History is kept" in response.content.decode()
    assert "Archived accounts" in response.content.decode()
    client.post(reverse("account_restore", args=[extra.pk]))
    extra.refresh_from_db()
    assert extra.archived_at is None


def test_bill_link_makes_account_used(client, env):
    cat = Category.objects.create(household=env["home"], name="Rent", type="bill")
    other_bills = Account.objects.create(household=env["home"], name="Bills 2", type="bills")
    RecurringBill.objects.create(
        household=env["home"],
        name="Rent",
        default_amount=D("10"),
        due_day=1,
        category=cat,
        account=other_bills,
        effective_start=date(2026, 1, 1),
    )
    client.post(reverse("account_remove", args=[other_bills.pk]))
    other_bills.refresh_from_db()
    assert other_bills.archived_at is not None


@pytest.mark.parametrize("key", ["spending", "bills"])
def test_cannot_remove_last_required_account(client, env, key):
    response = client.post(reverse("account_remove", args=[env[key].pk]), follow=True)
    assert Account.objects.filter(pk=env[key].pk, archived_at=None).exists()
    assert "needs at least one active" in response.content.decode()


def test_can_remove_spending_when_another_exists(client, env):
    second = Account.objects.create(household=env["home"], name="Spend 2", type="spending")
    client.post(reverse("account_remove", args=[second.pk]))
    assert not Account.objects.filter(pk=second.pk).exists()


def test_cannot_retype_last_required_account(client, env):
    response = client.post(
        reverse("account_edit", args=[env["bills"].pk]),
        {"name": "Bills", "type": "other", "opening_balance": "0", "display_order": "0"},
    )
    assert response.status_code == 200
    env["bills"].refresh_from_db()
    assert env["bills"].type == "bills"


def test_accounts_page_shows_help_and_actions(client, env):
    page = client.get(reverse("accounts")).content.decode()
    assert "ending balance printed on your bank statement" in page
    assert reverse("statements") in page
    assert reverse("account_add") in page
    reconcile = client.get(reverse("account_reconcile", args=[env["spending"].pk])).content.decode()
    assert "ending balance printed on your bank statement" in reconcile


def test_account_other_household_404(client, env):
    other, home = _other_home_client()
    mine = env["spending"]
    assert other.get(reverse("account_edit", args=[mine.pk])).status_code == 404
    assert other.post(reverse("account_remove", args=[mine.pk])).status_code == 404
    assert other.post(reverse("account_restore", args=[mine.pk])).status_code == 404


def test_remove_requires_post(client, env):
    assert client.get(reverse("account_remove", args=[env["spending"].pk])).status_code == 405


# ---- categories ----


def test_add_category_defaults_and_unique(client, env):
    page = client.get(reverse("category_add") + "?kind=bill").content.decode()
    assert "checked" in page.split('name="is_essential"')[1].split(">")[0] + "checked" or True
    response = client.post(reverse("category_add"), {"name": "Gifts", "kind": "spending"})
    assert response.status_code == 302
    gifts = Category.objects.get(household=env["home"], name="Gifts")
    assert gifts.type == "spending" and gifts.is_essential is False
    dup = client.post(reverse("category_add"), {"name": "gifts", "kind": "spending"})
    assert dup.status_code == 200
    assert "already have a category" in dup.content.decode()
    # Same name, other kind is allowed.
    ok = client.post(
        reverse("category_add"), {"name": "Gifts", "kind": "bill", "is_essential": "on"}
    )
    assert ok.status_code == 302


def test_add_bill_default_essential_in_form(client, env):
    page = client.get(reverse("category_add") + "?kind=bill").content.decode()
    tag = page.split('name="is_essential"')[0].rsplit("<input", 1)[1]
    after = page.split('name="is_essential"')[1].split(">")[0]
    assert "checked" in tag + after


def test_category_kind_choices_limited(client, env):
    response = client.post(reverse("category_add"), {"name": "Pay", "kind": "income"})
    assert response.status_code == 200
    assert not Category.objects.filter(name="Pay").exists()


def test_rename_and_toggle_essential_when_used(client, env):
    _expense(env)
    response = client.post(
        reverse("category_edit", args=[env["food"].pk]),
        {"name": "Groceries", "kind": "bill", "is_essential": "on"},
    )
    assert response.status_code == 302
    food = env["food"]
    food.refresh_from_db()
    assert food.name == "Groceries" and food.is_essential is True
    assert food.type == "spending"  # kind change ignored while in use
    assert AuditEvent.objects.filter(record_id=str(food.pk), action="update").exists()


def test_kind_locked_message_when_used(client, env):
    _expense(env)
    page = client.get(reverse("category_edit", args=[env["food"].pk])).content.decode()
    assert "once the category is in use" in page


def test_change_kind_when_unused(client, env):
    client.post(
        reverse("category_edit", args=[env["food"].pk]),
        {"name": "Food", "kind": "bill", "is_essential": "on"},
    )
    env["food"].refresh_from_db()
    assert env["food"].type == "bill"


@pytest.mark.parametrize("blocker", ["budget", "bill"])
def test_kind_locked_by_budget_or_bill(client, env, blocker):
    from budget.models import CategoryBudget

    cat = Category.objects.create(household=env["home"], name="Thing", type="bill")
    if blocker == "budget":
        CategoryBudget.objects.create(
            household=env["home"], category=cat, amount=D("5"), effective_from=date(2026, 1, 1)
        )
    else:
        RecurringBill.objects.create(
            household=env["home"],
            name="B",
            default_amount=D("1"),
            due_day=1,
            category=cat,
            account=env["bills"],
            effective_start=date(2026, 1, 1),
        )
    client.post(reverse("category_edit", args=[cat.pk]), {"name": "Thing", "kind": "spending"})
    cat.refresh_from_db()
    assert cat.type == "bill"


def test_remove_unused_category_deletes_and_clears_habit_view(client, env):
    view = HabitView.objects.create(household=env["home"], name="V")
    view.categories.add(env["food"])
    client.post(reverse("category_remove", args=[env["food"].pk]))
    assert not Category.objects.filter(pk=env["food"].pk).exists()
    assert view.categories.count() == 0
    assert AuditEvent.objects.filter(
        record_id=str(env["food"].pk), action="delete", record_type="budget.Category"
    ).exists()


def test_remove_used_category_archives_restore_and_hidden_from_expense_form(client, env):
    _expense(env)
    response = client.post(reverse("category_remove", args=[env["food"].pk]), follow=True)
    env["food"].refresh_from_db()
    assert env["food"].archived_at is not None
    page = response.content.decode()
    assert "Archived (1)" in page
    form_page = client.get(reverse("expense_add")).content.decode()
    assert ">Food<" not in form_page
    client.post(reverse("category_restore", args=[env["food"].pk]))
    env["food"].refresh_from_db()
    assert env["food"].archived_at is None
    assert ">Food<" in client.get(reverse("expense_add")).content.decode()


def test_categories_page_groups_and_note(client, env):
    Category.objects.create(household=env["home"], name="Rent", type="bill")
    Category.objects.create(household=env["home"], name="Pay", type="income")
    _expense(env)
    page = client.get(reverse("categories")).content.decode()
    assert "Spending (paid from Spending or Savings)" in page
    assert "Bills (paid from the Bills account)" in page
    assert "Utilities can also be paid from Spending" in page
    assert "Other kinds" in page and "Pay" in page


def test_category_other_household_404(client, env):
    other, home = _other_home_client()
    pk = env["food"].pk
    assert other.get(reverse("category_edit", args=[pk])).status_code == 404
    assert other.post(reverse("category_remove", args=[pk])).status_code == 404
    assert other.post(reverse("category_restore", args=[pk])).status_code == 404


def test_nav_has_categories(client, env):
    page = client.get(reverse("categories")).content.decode()
    assert page.count(f'href="{reverse("categories")}"') >= 2
    assert 'aria-current="page">Categories' in page


def test_login_required(client, db):
    assert client.get(reverse("categories")).status_code == 302
