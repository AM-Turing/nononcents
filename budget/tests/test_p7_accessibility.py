"""P7: accessibility (aria ids, field-naming messages, current-page marks, links)."""

import re
from datetime import timedelta
from uuid import uuid4

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
    RecurringBill,
)

TODAY = timezone.localdate()


@pytest.fixture
def world(client, db):
    user = get_user_model().objects.create_user("p7-user", password="a-secure-password-123")
    home = Household.objects.create(name="P7 profile")
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills", display_order=1)
    Account.objects.create(household=home, name="Savings", type="savings", display_order=2)
    Category.objects.create(household=home, name="General", type="spending")
    bill_category = Category.objects.create(household=home, name="Other Bills", type="bill")
    period = PayPeriod.objects.create(
        household=home,
        nominal_payday=TODAY - timedelta(days=2),
        expected_deposit_date=TODAY - timedelta(days=2),
        start_date=TODAY - timedelta(days=2),
        end_date=TODAY + timedelta(days=11),
    )
    return {
        "client": client,
        "home": home,
        "spending": spending,
        "bills": bills,
        "bill_category": bill_category,
        "period": period,
    }


def ids_in(html):
    return set(re.findall(r'\sid="([^"]+)"', html))


def describedby_targets(html):
    targets = set()
    for value in re.findall(r'aria-describedby="([^"]*)"', html):
        targets.update(value.split())
    return targets


def assert_describedby_resolve(html):
    missing = describedby_targets(html) - ids_in(html)
    assert not missing, f"aria-describedby points at missing ids: {sorted(missing)}"


def blank_post(client, url_name, *args, **extra):
    management = {
        f"{prefix}-{key}": value
        for prefix in ("splits", "savings")
        for key, value in (
            ("TOTAL_FORMS", "0"),
            ("INITIAL_FORMS", "0"),
            ("MIN_NUM_FORMS", "0"),
            ("MAX_NUM_FORMS", "10"),
        )
    }
    data = {"form_token": str(uuid4()), **management, **extra}
    return client.post(reverse(url_name, args=args), data)


FORM_URLS = ("expense_add", "income_add", "refund_add", "transfer_add", "bill_add")


@pytest.mark.parametrize("url_name", FORM_URLS)
def test_describedby_ids_exist_on_forms_without_errors(world, url_name):
    html = world["client"].get(reverse(url_name)).content.decode()
    assert_describedby_resolve(html)
    if url_name != "bill_add":
        assert "aria-describedby" in html


@pytest.mark.parametrize("url_name", FORM_URLS)
def test_describedby_ids_exist_on_forms_with_errors(world, url_name):
    response = blank_post(world["client"], url_name)
    html = response.content.decode()
    assert response.status_code == 200
    assert "errorlist" in html
    assert_describedby_resolve(html)
    assert any(target.endswith("_error") for target in describedby_targets(html))


def test_budget_and_pay_period_forms_describedby_ids_exist(world):
    client = world["client"]
    for url in (reverse("budgets"), reverse("pay_periods")):
        assert_describedby_resolve(client.get(url).content.decode())
    html = client.post(reverse("pay_periods"), {"frequency": ""}).content.decode()
    assert_describedby_resolve(html)
    html = client.post(reverse("budgets"), {"spending_target": "-1"}).content.decode()
    assert_describedby_resolve(html)


@pytest.mark.parametrize("url_name", ("expense_add", "refund_add", "transfer_add", "income_add"))
def test_required_messages_name_the_field(world, url_name):
    html = blank_post(world["client"], url_name).content.decode()
    assert "This field is required." not in html
    assert "Enter an amount." in html
    if url_name == "transfer_add":
        assert "Choose the account the money comes from." in html
        assert "Choose the account the money goes to." in html
    elif url_name != "income_add":
        assert "Choose an account." in html


def test_expense_category_message_names_the_field(world):
    html = blank_post(world["client"], "expense_add").content.decode()
    assert "Choose a category." in html


@pytest.mark.parametrize("url_name", FORM_URLS)
def test_selects_have_no_dashes_placeholder(world, url_name):
    html = world["client"].get(reverse(url_name)).content.decode()
    assert "---------" not in html


def test_empty_select_choices_have_meaningful_text(world):
    html = world["client"].get(reverse("expense_add")).content.decode()
    assert "Choose an account" in html
    assert "Choose a category" in html


SIDEBAR_PAGES = (
    ("dashboard", "Home"),
    ("transactions", "History"),
    ("bills", "Bills"),
    ("budgets", "Budgets"),
    ("savings", "Savings"),
    ("reports", "Outlook"),
)


def sidebar_current(html):
    nav = html.split('id="primary-navigation"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r'<a [^>]*aria-current="page"[^>]*>([^<]*)</a>', nav)


@pytest.mark.parametrize(("url_name", "label"), SIDEBAR_PAGES)
def test_sidebar_marks_the_current_page(world, url_name, label):
    html = world["client"].get(reverse(url_name)).content.decode()
    assert sidebar_current(html) == [label]


def test_sidebar_marks_budgets_on_pay_periods_page(world):
    html = world["client"].get(reverse("pay_periods")).content.decode()
    assert sidebar_current(html) == ["Budgets"]


def test_sidebar_current_page_style_exists():
    from pathlib import Path

    css = Path("budget/static/budget/sidebar.css").read_text()
    assert '.sidebar nav a[aria-current="page"]' in css
    assert "font-weight: 700" in css


def test_home_action_needed_text_is_a_link_to_bill_confirmation(world):
    bill = RecurringBill.objects.create(
        household=world["home"],
        name="Internet",
        default_amount="60.00",
        due_day=TODAY.day,
        category=world["bill_category"],
        account=world["bills"],
        effective_start=TODAY.replace(day=1),
    )
    BillOccurrence.objects.create(
        bill=bill, due_date=TODAY, expected_amount="60.00", pay_period=world["period"]
    )
    html = world["client"].get(reverse("dashboard")).content.decode()
    match = re.search(r'<a href="([^"]+)">([^<]*confirmation)</a>', html)
    assert match, "Action needed text is not a link"
    assert match.group(1) == reverse("bills") + "#review-confirm"
    assert "need" in match.group(2)
    bills_html = world["client"].get(reverse("bills")).content.decode()
    assert 'id="review-confirm"' in bills_html


def test_bills_page_balances_row_actions_and_keeps_css_in_head(world):
    bill = RecurringBill.objects.create(
        household=world["home"],
        name="Internet",
        default_amount="60.00",
        due_day=TODAY.day,
        category=world["bill_category"],
        account=world["bills"],
        effective_start=TODAY.replace(day=1),
    )
    BillOccurrence.objects.create(
        bill=bill, due_date=TODAY, expected_amount="60.00", pay_period=world["period"]
    )
    html = world["client"].get(reverse("bills")).content.decode()
    assert html.count("<div") == html.count("</div>")
    head = html.split("</head>", 1)[0]
    assert "bills.css" in head
    dash = world["client"].get(reverse("dashboard")).content.decode()
    assert "dashboard.css" in dash.split("</head>", 1)[0]


def test_history_cards_omit_label_rows_without_a_value(world):
    Expense.objects.create(
        household=world["home"],
        date=TODAY,
        amount="10.00",
        account=world["spending"],
        category=Category.objects.get(name="General"),
        merchant="Cafe",
        notes="",
    )
    html = world["client"].get(reverse("transactions")).content.decode()
    assert "<dt>Description</dt>" in html
    assert "<dt>Notes</dt>" not in html
    assert 'class="transaction-list" aria-label' not in html
