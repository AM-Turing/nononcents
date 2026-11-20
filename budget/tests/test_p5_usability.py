"""P5: daily flows and first run (checklist, add forms, History, wording)."""

from datetime import date, timedelta
from decimal import Decimal
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
    ExpenseSplit,
    Household,
    HouseholdMembership,
    PayPeriod,
    RecurringBill,
    SavingsGoal,
    Transfer,
)
from budget.services import create_expense, create_savings_goal, ensure_savings_goals
from budget.setup_checklist import setup_checklist

TODAY = timezone.localdate()


@pytest.fixture
def world(client, db):
    user = get_user_model().objects.create_user("p5-user", password="a-secure-password-123")
    home = Household.objects.create(name="P5 profile")
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spending = Account.objects.create(
        household=home, name="Spending", type="spending", display_order=0
    )
    bills = Account.objects.create(household=home, name="Bills", type="bills", display_order=1)
    savings = Account.objects.create(
        household=home, name="Savings", type="savings", display_order=2
    )
    general = Category.objects.create(household=home, name="General", type="spending")
    Category.objects.create(household=home, name="ATM Withdrawal", type="spending")
    other_bills = Category.objects.create(household=home, name="Other Bills", type="bill")
    return {
        "client": client,
        "user": user,
        "home": home,
        "spending": spending,
        "bills": bills,
        "savings": savings,
        "general": general,
        "other_bills": other_bills,
    }


def checklist_done(home):
    steps = setup_checklist(home)
    return None if steps is None else [step["done"] for step in steps]


def current_period(home):
    return PayPeriod.objects.create(
        household=home,
        nominal_payday=TODAY - timedelta(days=2),
        expected_deposit_date=TODAY - timedelta(days=2),
        start_date=TODAY - timedelta(days=2),
        end_date=TODAY + timedelta(days=11),
    )


# --- Item 2: setup checklist -----------------------------------------------------------------


def test_new_profile_sees_the_checklist_with_only_the_starter_steps_ticked(world):
    # Steps: accounts, opening balances, pay period, bills, categories, statement check.
    # The three starter accounts exist (ticked); everything else is still to do.
    assert checklist_done(world["home"]) == [True, False, False, False, False, False]
    page = world["client"].get(reverse("dashboard")).content.decode()
    assert "Set up your budget" in page
    for name in ("accounts", "pay_periods", "bill_add", "categories", "statements"):
        assert f'href="{reverse(name)}"' in page


def test_checklist_steps_tick_from_data(world):
    home = world["home"]
    Account.objects.filter(pk=world["spending"].pk).update(opening_balance=Decimal("250.00"))
    assert checklist_done(home)[:2] == [True, True]
    current_period(home)
    assert checklist_done(home)[2] is True
    RecurringBill.objects.create(
        household=home,
        name="Rent",
        default_amount=Decimal("900.00"),
        due_day=1,
        category=world["other_bills"],
        account=world["bills"],
        effective_start=date(2026, 1, 1),
    )
    assert checklist_done(home)[3] is True
    assert checklist_done(home)[4] is False  # only the starter categories so far
    Category.objects.create(household=home, name="Groceries", type="spending")
    assert checklist_done(home) == [True, True, True, True, True, False]


def test_checklist_disappears_when_every_step_is_done(world):
    from budget.models import StatementImport

    home = world["home"]
    Account.objects.filter(pk=world["spending"].pk).update(opening_balance=Decimal("250.00"))
    current_period(home)
    RecurringBill.objects.create(
        household=home,
        name="Rent",
        default_amount=Decimal("900.00"),
        due_day=1,
        category=world["other_bills"],
        account=world["bills"],
        effective_start=date(2026, 1, 1),
    )
    Category.objects.create(household=home, name="Groceries", type="spending")
    StatementImport.objects.create(
        household=home,
        account=world["spending"],
        period_start=TODAY - timedelta(days=30),
        period_end=TODAY,
        closing_balance=Decimal("250.00"),
        file_sha256="0" * 64,
    )
    assert setup_checklist(home) is None
    assert "Set up your budget" not in world["client"].get(reverse("dashboard")).content.decode()


def test_checklist_is_hidden_once_the_profile_has_entries(world):
    # An established profile (set up, with entries) must never see the card, even if it
    # has never uploaded a statement.
    home = world["home"]
    current_period(home)
    assert setup_checklist(home) is not None
    create_expense(
        household=home,
        actor=world["user"],
        entry_date=TODAY,
        account=world["spending"],
        category=world["general"],
        amount=Decimal("5.00"),
    )
    assert setup_checklist(home) is None
    assert "Set up your budget" not in world["client"].get(reverse("dashboard")).content.decode()


# --- Item 3 and 7: History -------------------------------------------------------------------


def test_empty_history_without_filters_is_a_first_run_message(world):
    page = world["client"].get(reverse("transactions")).content.decode()
    assert "No transactions match your filters" not in page
    assert "No transactions yet." in page
    assert f'<a href="{reverse("expense_add")}">Add your first expense</a>' in page
    assert "0 transactions." in page


def test_empty_history_with_a_filter_says_nothing_matches(world):
    page = world["client"].get(reverse("transactions"), {"q": "nothing-like-this"}).content.decode()
    assert "No transactions match your filters." in page
    assert "No transactions yet." not in page
    assert "0 transactions match your filters." in page
    # An active filter keeps the collapsed-on-phones filter group open.
    assert "data-filter-details open" in page


def test_history_shows_the_result_count(world):
    for merchant in ("Cafe", "Cafe Two", "Gas"):
        create_expense(
            household=world["home"],
            actor=world["user"],
            entry_date=TODAY,
            account=world["spending"],
            category=world["general"],
            amount=Decimal("5.00"),
            merchant=merchant,
        )
    client = world["client"]
    assert "3 transactions." in client.get(reverse("transactions")).content.decode()
    # Two of the three names contain "cafe".
    filtered = client.get(reverse("transactions"), {"q": "cafe"}).content.decode()
    assert "2 transactions match your filters." in filtered
    assert "data-filter-details>" in client.get(reverse("transactions")).content.decode()


# --- Item 4: straight to the expense form, with a type switcher ------------------------------


def test_add_buttons_go_straight_to_the_expense_form(world):
    for name in ("dashboard", "transactions", "bills"):
        page = world["client"].get(reverse(name)).content.decode()
        assert f'class="add" href="{reverse("expense_add")}">Add＋' in page
        assert f'href="{reverse("expense_add")}" class="primary">＋ Add transaction' in page
        assert f'href="{reverse("transaction_add")}"' not in page


def test_direct_links_to_the_chooser_still_work(world):
    assert world["client"].get(reverse("transaction_add")).status_code == 200


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("expense_add", "Expense"),
        ("income_add", "Income"),
        ("refund_add", "Refund"),
        ("transfer_add", "Transfer"),
    ],
)
def test_every_add_form_has_the_type_switcher_with_its_own_type_current(world, name, label):
    page = world["client"].get(reverse(name)).content.decode()
    assert '<nav aria-label="Type of entry">' in page
    for other_name, other_label in (
        ("expense_add", "Expense"),
        ("income_add", "Income"),
        ("refund_add", "Refund"),
        ("transfer_add", "Transfer"),
    ):
        current = ' aria-current="page"' if other_name == name else ""
        assert f'<a href="{reverse(other_name)}"{current}>{other_label}</a>' in page


def test_edit_forms_have_no_type_switcher(world):
    expense = create_expense(
        household=world["home"],
        actor=world["user"],
        entry_date=TODAY,
        account=world["spending"],
        category=world["general"],
        amount=Decimal("5.00"),
    )
    page = world["client"].get(reverse("transaction_edit", args=["expense", expense.pk]))
    assert "Type of entry" not in page.content.decode()


# --- Item 5: "More options" ------------------------------------------------------------------


def more_options_html(page):
    start = page.index('<details class="more-options"')
    return page[start : page.index("</details>", start)], page[start : start + 40]


def test_expense_form_tucks_four_fields_into_closed_more_options(world):
    page = world["client"].get(reverse("expense_add")).content.decode()
    inside, opening = more_options_html(page)
    assert " open" not in opening
    for field in ("split_transaction", "is_essential_override", "bill", "bill_occurrence"):
        assert f'name="{field}"' in inside
    outside = page.replace(inside, "")
    for field in ("amount", "date", "account", "category", "merchant", "notes"):
        assert f'name="{field}"' in outside
    assert 'name="bill"' not in outside


def post_expense(world, **extra):
    data = {
        "amount": "50.00",
        "date": TODAY.isoformat(),
        "account": world["spending"].pk,
        "category": world["general"].pk,
        "merchant": "Shop",
        "notes": "",
        "form_token": str(uuid4()),
        **extra,
    }
    return world["client"].post(reverse("expense_add"), data)


def test_more_options_opens_when_a_hidden_field_has_a_value(world):
    response = post_expense(world, is_essential_override="true", amount="0")  # amount error
    page = response.content.decode()
    assert response.status_code == 200
    assert " open" in more_options_html(page)[1]


def test_more_options_opens_when_a_hidden_field_has_an_error(world):
    # A bill month without a bill is an error on the bill month field.
    bill = RecurringBill.objects.create(
        household=world["home"],
        name="Rent",
        default_amount=Decimal("900.00"),
        due_day=1,
        category=world["other_bills"],
        account=world["bills"],
        effective_start=date(2026, 1, 1),
    )
    month = BillOccurrence.objects.create(
        bill=bill, due_date=TODAY, expected_amount=Decimal("900.00")
    )
    response = post_expense(world, bill_occurrence=month.pk)
    page = response.content.decode()
    assert "Choose a bill before choosing a bill month." in page
    assert " open" in more_options_html(page)[1]


def test_more_options_stays_closed_when_only_visible_fields_have_errors(world):
    response = post_expense(world, amount="0")
    assert " open" not in more_options_html(response.content.decode())[1]


def test_split_inside_more_options_still_saves_exact_amounts(world):
    groceries = Category.objects.create(household=world["home"], name="Groceries", type="spending")
    response = post_expense(
        world,
        split_transaction="on",
        category="",
        **{
            "splits-TOTAL_FORMS": "2",
            "splits-INITIAL_FORMS": "0",
            "splits-MIN_NUM_FORMS": "0",
            "splits-MAX_NUM_FORMS": "10",
            "splits-0-category": world["general"].pk,
            "splits-0-amount": "30.00",
            "splits-1-category": groceries.pk,
            "splits-1-amount": "20.00",
        },
    )
    assert response.status_code == 302
    expense = Expense.objects.get()
    assert expense.amount == Decimal("50.00")
    # 30.00 + 20.00 = 50.00, the whole expense.
    amounts = sorted(ExpenseSplit.objects.filter(expense=expense).values_list("amount", flat=True))
    assert amounts == [Decimal("20.00"), Decimal("30.00")]


def test_bill_and_month_still_apply_the_payment(world):
    bill = RecurringBill.objects.create(
        household=world["home"],
        name="Rent",
        default_amount=Decimal("900.00"),
        due_day=1,
        category=world["other_bills"],
        account=world["bills"],
        effective_start=date(2026, 1, 1),
    )
    month = BillOccurrence.objects.create(
        bill=bill, due_date=TODAY, expected_amount=Decimal("900.00")
    )
    data = {
        "amount": "900.00",
        "date": TODAY.isoformat(),
        "account": world["bills"].pk,
        "category": world["other_bills"].pk,
        "merchant": "Landlord",
        "notes": "",
        "bill": bill.pk,
        "bill_occurrence": month.pk,
        "form_token": str(uuid4()),
    }
    assert world["client"].post(reverse("expense_add"), data).status_code == 302
    month.refresh_from_db()
    assert month.status == BillOccurrence.Status.PAID  # 900.00 paid of 900.00


# --- Item 6 and 7: date help, save message ---------------------------------------------------


def test_date_help_names_no_format(world):
    page = world["client"].get(reverse("expense_add")).content.decode()
    assert "YYYY" not in page
    assert "Choose a date from the calendar" in page


def test_remembered_date_is_explained(world):
    yesterday = TODAY - timedelta(days=1)
    session = world["client"].session
    session["last_entry_date"] = yesterday.isoformat()
    session.save()
    page = world["client"].get(reverse("expense_add")).content.decode()
    assert f'value="{yesterday.isoformat()}"' in page
    assert (
        f"Same date as your last entry ({yesterday:%b} {yesterday.day}, {yesterday.year})" in page
    )


def test_todays_date_needs_no_explanation(world):
    page = world["client"].get(reverse("expense_add")).content.decode()
    assert "Same date as your last entry" not in page


def test_save_message_names_amount_and_account(world):
    Account.objects.filter(pk=world["spending"].pk).update(opening_balance=Decimal("100.00"))
    post_expense(world, amount="12.50")
    messages = [str(m) for m in world["client"].get(reverse("dashboard")).context["messages"]]
    # 100.00 opening - 12.50 expense = 87.50
    assert messages == ["Expense of $12.50 saved to Spending. Resulting balance: $87.50."]


# --- Item 7: save and add another, other wording ---------------------------------------------


def test_income_and_transfer_offer_and_honour_save_and_add_another(world):
    client = world["client"]
    for name in ("income_add", "transfer_add"):
        assert "Save and add another" in client.get(reverse(name)).content.decode()
    income = client.post(
        reverse("income_add"),
        {
            "date": TODAY.isoformat(),
            "source": "Acme",
            "amount": "100.00",
            "notes": "",
            "form_token": str(uuid4()),
            f"allocation_{world['spending'].pk}": "100.00",
            "add_another": "1",
        },
    )
    assert income.url == reverse("income_add")
    transfer = client.post(
        reverse("transfer_add"),
        {
            "date": TODAY.isoformat(),
            "source": world["spending"].pk,
            "destination": world["bills"].pk,
            "amount": "10.00",
            "notes": "",
            "form_token": str(uuid4()),
            "add_another": "1",
        },
    )
    assert transfer.url == reverse("transfer_add")
    assert Transfer.objects.get().amount == Decimal("10.00")


def test_plain_save_on_income_and_transfer_still_goes_home(world):
    transfer = world["client"].post(
        reverse("transfer_add"),
        {
            "date": TODAY.isoformat(),
            "source": world["spending"].pk,
            "destination": world["bills"].pk,
            "amount": "10.00",
            "notes": "",
            "form_token": str(uuid4()),
        },
    )
    assert transfer.url == reverse("dashboard")


def test_empty_account_and_category_lists_offer_a_next_step(world):
    other = Household.objects.create(name="Empty")
    user = get_user_model().objects.create_user("empty-user", password="a-secure-password-123")
    HouseholdMembership.objects.create(household=other, user=user)
    world["client"].force_login(user)
    accounts = world["client"].get(reverse("accounts")).content.decode()
    assert f'No active accounts. <a href="{reverse("account_add")}">Add an account</a>.' in accounts
    categories = world["client"].get(reverse("categories")).content.decode()
    assert (
        f'<a href="{reverse("category_add")}?kind=spending">Add a spending category</a>'
        in categories
    )
    assert f'<a href="{reverse("category_add")}?kind=bill">Add a bill category</a>' in categories


def test_account_button_says_archive(world):
    page = world["client"].get(reverse("accounts")).content.decode()
    assert "Archive<span" in page
    assert ">Remove<span" not in page


def test_confirm_all_message_drops_the_bracket_when_nothing_was_skipped(world):
    bill = RecurringBill.objects.create(
        household=world["home"],
        name="Rent",
        default_amount=Decimal("900.00"),
        due_day=1,
        category=world["other_bills"],
        account=world["bills"],
        effective_start=date(2026, 1, 1),
    )
    BillOccurrence.objects.create(bill=bill, due_date=TODAY, expected_amount=Decimal("900.00"))
    response = world["client"].post(
        reverse("bills_confirm_all"), {"month": f"{TODAY:%Y-%m}"}, follow=True
    )
    assert [str(m) for m in response.context["messages"]] == ["Confirmed 1 bills"]


def test_savings_goal_delete_asks_before_deleting(world):
    home = world["home"]
    emergency, _ = ensure_savings_goals(home)
    goal = create_savings_goal(
        household=home,
        actor=world["user"],
        name="Trip",
        target_amount="400.00",
        priority=2,
    )
    url = reverse("savings_goal_action", args=[goal.pk, "delete"])
    asked = world["client"].post(url)
    assert asked.status_code == 200
    assert "Delete the savings goal “Trip”?" in asked.content.decode()
    assert SavingsGoal.objects.filter(pk=goal.pk).exists()
    done = world["client"].post(url, {"confirmed": "1"})
    assert done.status_code == 302
    assert not SavingsGoal.objects.filter(pk=goal.pk).exists()
    assert SavingsGoal.objects.filter(pk=emergency.pk).exists()
