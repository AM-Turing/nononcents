"""Owner requests 2026-11-19: Display Mode follows the account; the statement account ending
can be corrected on the account page; Quick actions include Add expense."""

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget.models import Account, AuditEvent, Household, HouseholdMembership, UserPreference


@pytest.fixture
def env(client, db):
    user = get_user_model().objects.create_user("fix-user", password="a-secure-password-123")
    home = Household.objects.create(name="Fix budget")
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    return {"home": home, "user": user, "spending": spending, "bills": bills}


# ---- Display Mode ------------------------------------------------------------------------


def test_saved_display_mode_is_sent_with_every_page(client, env):
    page = client.get(reverse("accounts")).content.decode()
    assert "data-saved-appearance" not in page  # nothing saved yet: the browser's choice is used
    response = client.post(reverse("appearance"), {"appearance": "dark", "next": "/accounts/"})
    assert response.status_code == 302
    assert UserPreference.objects.get(user=env["user"]).appearance == "dark"
    page = client.get(reverse("accounts")).content.decode()
    assert '<html lang="en" data-theme="system" data-saved-appearance="dark">' in page
    # The page script prefers the saved choice and copies it into the browser.
    # (now a static file, since the Content-Security-Policy forbids inline scripts)
    assert "budget/theme-init.js" in page
    script = (settings.BASE_DIR / "budget/static/budget/theme-init.js").read_text()
    assert "root.dataset.savedAppearance" in script
    assert 'localStorage.setItem("appearance", saved)' in script


def test_signed_out_pages_use_the_browser_choice(client, env):
    client.post(reverse("appearance"), {"appearance": "light", "next": "/"})
    client.logout()
    page = client.get(reverse("login")).content.decode()
    assert "data-saved-appearance" not in page


def test_display_mode_is_per_person(client, env):
    other = get_user_model().objects.create_user("other", password="a-secure-password-123")
    HouseholdMembership.objects.create(household=env["home"], user=other)
    UserPreference.objects.create(user=other, appearance="dark")
    UserPreference.objects.create(user=env["user"], appearance="light")
    page = client.get(reverse("accounts")).content.decode()
    assert 'data-saved-appearance="light"' in page


# ---- Statement account ending ------------------------------------------------------------


def _edit(client, account, **extra):
    data = {
        "name": account.name,
        "type": account.type,
        "opening_balance": "0",
        "display_order": "0",
        "statement_suffix": "",
    }
    return client.post(reverse("account_edit", args=[account.pk]), data | extra)


def test_account_page_shows_and_changes_the_statement_ending(client, env):
    env["spending"].statement_suffix = "1111"
    env["spending"].save()
    page = client.get(reverse("account_edit", args=[env["spending"].pk])).content.decode()
    assert "Statement account ending (optional)" in page
    assert 'value="1111"' in page
    assert _edit(client, env["spending"], statement_suffix="2222").status_code == 302
    env["spending"].refresh_from_db()
    assert env["spending"].statement_suffix == "2222"
    # The log records that it changed, never the digits.
    event = AuditEvent.objects.filter(record_type="budget.Account").latest("occurred_at")
    assert event.changes["statement_suffix"] == "changed"
    assert "2222" not in str(event.changes) and "1111" not in str(event.changes)


def test_clearing_the_ending_makes_the_next_upload_ask(client, env):
    env["spending"].statement_suffix = "1111"
    env["spending"].save()
    _edit(client, env["spending"])
    env["spending"].refresh_from_db()
    assert env["spending"].statement_suffix == ""
    event = AuditEvent.objects.filter(record_type="budget.Account").latest("occurred_at")
    assert event.changes["statement_suffix"] == "cleared"


@pytest.mark.parametrize("value", ["123", "12345", "12a4", "-123"])
def test_ending_must_be_four_digits(client, env, value):
    response = _edit(client, env["spending"], statement_suffix=value)
    assert response.status_code == 200
    page = response.content.decode()
    assert "Enter exactly 4 digits" in page or "at most 4 characters" in page
    env["spending"].refresh_from_db()
    assert env["spending"].statement_suffix == ""


def test_ending_cannot_belong_to_two_accounts(client, env):
    env["bills"].statement_suffix = "4321"
    env["bills"].save()
    response = _edit(client, env["spending"], statement_suffix="4321")
    assert response.status_code == 200
    assert "Bills already uses this ending. Clear it there first." in response.content.decode()
    env["spending"].refresh_from_db()
    assert env["spending"].statement_suffix == ""


def test_ending_is_not_shared_across_profiles(client, env):
    elsewhere = Household.objects.create(name="Elsewhere")
    Account.objects.create(
        household=elsewhere, name="Other", type="spending", statement_suffix="5555"
    )
    assert _edit(client, env["spending"], statement_suffix="5555").status_code == 302


# ---- Quick actions -----------------------------------------------------------------------


def test_quick_actions_start_with_add_expense(client, env):
    from django.template.loader import render_to_string

    html = render_to_string("budget/_quick_actions.html", {"custom_quick_actions": []})
    links = [part.split('"')[1] for part in html.split("href=")[1:]]
    assert links[0] == reverse("expense_add")
    assert "Add expense" in html
