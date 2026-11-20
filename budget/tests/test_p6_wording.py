"""P6: page headings match menu names, Statement check is in the menus, plain wording."""

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget.forms import PAGE_LABEL_DEFAULTS, PAGE_TITLE_DEFAULTS, QuickActionForm
from budget.models import (
    Account,
    ApplicationSetting,
    Category,
    Expense,
    Household,
    HouseholdMembership,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def home(client):
    user = get_user_model().objects.create_user("p6", password="a-secure-password-123")
    household = Household.objects.create(name="Test")
    HouseholdMembership.objects.create(household=household, user=user)
    client.force_login(user)
    return household


def test_default_headings_equal_default_menu_names():
    assert PAGE_TITLE_DEFAULTS == PAGE_LABEL_DEFAULTS
    assert PAGE_LABEL_DEFAULTS["reports"] == "Outlook"


@pytest.mark.parametrize(
    ("url_name", "page"),
    [
        ("transactions", "transactions"),
        ("budgets", "budgets"),
        ("reports", "reports"),
        ("savings", "savings"),
        ("docs", "docs"),
    ],
)
def test_page_heading_is_its_menu_name(client, home, url_name, page):
    html = client.get(reverse(url_name), follow=True).content.decode()
    name = PAGE_LABEL_DEFAULTS[page]
    assert f"<h1>{name}</h1>" in html
    assert f">{name}</a>" in html


def test_outlook_menu_name_is_the_same_in_sidebar_and_phone_menu(client, home):
    html = client.get(reverse("dashboard")).content.decode()
    assert html.count(">Outlook</a>") == 2
    assert "Budget Outlook" not in html


def test_owner_renamed_page_keeps_its_name_everywhere(client, home):
    ApplicationSetting.objects.create(
        household=home, key="page_labels", value={"transactions": "Ledger book"}
    )
    html = client.get(reverse("transactions")).content.decode()
    assert "<h1>Ledger book</h1>" in html and ">Ledger book</a>" in html


def test_transfer_is_called_add_transfer_everywhere(client, home):
    assert ("transfer_add", "Add transfer") in QuickActionForm.DESTINATIONS
    assert ("transactions", "History") in QuickActionForm.DESTINATIONS
    assert "Add transfer" in client.get(reverse("transaction_add")).content.decode()
    assert "Move money" not in client.get(reverse("transactions")).content.decode()
    page = client.get(reverse("transfer_add")).content.decode()
    assert "<h1>Add transfer</h1>" in page


def test_statement_check_is_in_both_menus_with_aria_current(client, home):
    html = client.get(reverse("dashboard")).content.decode()
    assert html.count(">Statement check</a>") == 2
    page = client.get(reverse("statements")).content.decode()
    assert page.count('aria-current="page">Statement check</a>') == 2
    assert "<h1>Statement check</h1>" in page
    accounts = client.get(reverse("accounts")).content.decode()
    assert 'aria-current="page">Statement check</a>' not in accounts


def test_undo_banner_states_its_limits(client, home):
    cat = Category.objects.create(household=home, name="Food", type="spending")
    acct = Account.objects.create(household=home, name="Spending", type="spending")
    exp = Expense.objects.create(
        household=home,
        created_by=home.memberships.first().user,
        date=date(2026, 8, 1),
        amount="10.00",
        account=acct,
        category=cat,
        merchant="Shop",
    )
    response = client.post(
        reverse("transaction_delete", args=["expense", exp.pk]),
        {"quick": "1", "next": "/transactions/"},
        follow=True,
    )
    html = response.content.decode()
    assert "Undo works within 10 minutes, for the latest delete only." in html


@pytest.mark.parametrize("url_name", ["accounts", "savings", "bills"])
def test_plain_wording_replaces_jargon(client, home, url_name):
    html = client.get(reverse(url_name)).content.decode().lower()
    for old in ("ledger balance", "monthly obligations", "explicitly linked", "placeholder"):
        assert old not in html


def test_form_labels_use_plain_words(client, home):
    expense = client.get(reverse("expense_add")).content.decode()
    assert "Cost of this part" in expense and "What it was for" in expense
    assert "Allocated dollars" not in expense and "Allocation type" not in expense
    income = client.get(reverse("income_add")).content.decode()
    assert "This is irregular income" in income and "Is irregular" not in income
