"""Money tests for review package P3 (money-logic fixes). Every amount is worked by hand."""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse

from budget import undo
from budget.forms import ExpenseForm
from budget.models import (
    Account,
    BillOccurrence,
    Category,
    Expense,
    Household,
    HouseholdMembership,
)
from budget.services import pay_bill_occurrence
from budget.tests.test_forecast import make_bill, make_period, occurrence
from budget.tests.test_savings_goals import savings_setup  # noqa: F401

D = Decimal


@pytest.fixture
def world(db):
    user = get_user_model().objects.create_user("p3-user", password="safe-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    spending = Account.objects.create(
        household=home, name="Spending", type=Account.Type.SPENDING, opening_balance="1000.00"
    )
    bills = Account.objects.create(
        household=home, name="Bills", type=Account.Type.BILLS, opening_balance="1000.00"
    )
    utilities = Category.objects.create(household=home, name="Utilities", type="bill")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    return {
        "user": user,
        "home": home,
        "spending": spending,
        "bills": bills,
        "utilities": utilities,
        "groceries": groceries,
    }


# --- 1. A removed bill's unpaid live months can still be paid -----------------------------


def removed_bill_world(world):
    """Water: $100 due the 15th, removed after Oct 31; Oct 15 is unpaid and still live."""
    home = world["home"]
    bill = make_bill(home, "Water", "100.00", world["bills"], world["utilities"])
    oct_due = occurrence(bill, date(2026, 10, 15), "100.00")
    nov_due = occurrence(bill, date(2026, 11, 15), "100.00")
    bill.active = False
    bill.effective_end = date(2026, 10, 31)
    bill.save(update_fields=["active", "effective_end"])
    return bill, oct_due, nov_due


def test_removed_bill_live_month_can_be_paid(world):
    bill, oct_due, _ = removed_bill_world(world)
    problems, expense = None, None
    try:
        expense = pay_bill_occurrence(oct_due, world["user"], date(2026, 11, 19))
    except ValidationError as exc:
        problems = exc.messages
    assert problems is None
    assert expense is not None
    # Expected 100.00, nothing paid yet: the whole $100.00 is paid.
    assert expense.amount == D("100.00")
    oct_due.refresh_from_db()
    assert oct_due.status == BillOccurrence.Status.PAID
    assert oct_due.amount_paid == D("100.00")


def test_removed_bill_month_after_the_end_is_still_refused(world):
    # Nov 15 is after the Oct 31 end, so it is not a live obligation (not payable).
    bill, _, nov_due = removed_bill_world(world)
    with pytest.raises(ValidationError):
        pay_bill_occurrence(nov_due, world["user"], date(2026, 11, 19))
    assert Expense.objects.count() == 0


def test_expense_form_offers_removed_bill_with_live_month(world):
    bill, oct_due, nov_due = removed_bill_world(world)
    form = ExpenseForm(household=world["home"])
    assert list(form.fields["bill"].queryset) == [bill]
    # Only Oct 15 is live; Nov 15 is past the end.
    assert list(form.fields["bill_occurrence"].queryset) == [oct_due]


def test_undo_of_removed_bill_payment_restores_it(world):
    bill, oct_due, _ = removed_bill_world(world)
    expense = Expense.objects.create(
        household=world["home"],
        created_by=world["user"],
        date=date(2026, 10, 14),
        amount=D("100.00"),
        account=world["bills"],
        category=world["utilities"],
        bill_occurrence=oct_due,
    )
    data = undo.snapshot("expense", expense, "Water")
    expense.delete()
    problems = None
    try:
        undo.restore(world["home"], world["user"], data)
    except ValidationError as exc:
        problems = exc.messages
    assert problems is None
    restored = Expense.objects.get()
    # Restored at the same $100.00, linked to the Oct 15 month again.
    assert restored.amount == D("100.00")
    assert restored.bill_occurrence_id == oct_due.pk


# --- 3. Outlook for a range that starts after today ----------------------------------------


def future_range_world(world):
    """Today Sep 15. Spending $10/day pace; balance Sep 15 = 1300; a $500 paycheck on Sep 21."""
    from budget.models import Income, IncomeAllocation, PayPeriod, PayPeriodBudget

    home = world["home"]
    spending = world["spending"]
    # Only Spending counts here; empty the Bills account so the totals stay simple.
    world["bills"].opening_balance = D("0.00")
    world["bills"].save()
    spending.opening_balance = D("1000.00")
    spending.save()
    food = Category.objects.create(household=home, name="Food", type=Category.Type.SPENDING)
    for start, end, budget in (
        (date(2026, 9, 1), date(2026, 9, 10), {}),
        (date(2026, 9, 11), date(2026, 9, 20), {}),
        (date(2026, 9, 21), date(2026, 9, 30), {"expected_paycheck": D("500.00")}),
    ):
        period = PayPeriod.objects.create(
            household=home,
            nominal_payday=start,
            expected_deposit_date=start,
            start_date=start,
            end_date=end,
        )
        PayPeriodBudget.objects.create(pay_period=period, **budget)
    Expense.objects.create(
        household=home,
        created_by=world["user"],
        date=date(2026, 9, 5),
        amount=D("100.00"),
        account=spending,
        category=food,
    )
    pay = Income.objects.create(
        household=home,
        created_by=world["user"],
        date=date(2026, 9, 11),
        amount=D("400.00"),
        source="Pay",
        is_paycheck=True,
    )
    IncomeAllocation.objects.create(income=pay, account=spending, amount=D("400.00"))
    bill = make_bill(home, "Electric", "60.00", world["bills"], world["utilities"])
    occurrence(bill, date(2026, 9, 23), "60.00")
    return home


def test_future_range_end_balance_counts_spending_and_bills_before_the_range(world):
    from budget.outlook import outlook_data

    home = future_range_world(world)
    data = outlook_data(home, date(2026, 9, 26), date(2026, 9, 30), date(2026, 9, 15))
    # Balance Sep 15: 1000 opening - 100 spent + 400 paycheck = 1300.
    # Pace: Sep 1-10 spent 100 over 10 days = $10/day.
    # Gap Sep 16-25 = 10 days -> 100.00 spending; Electric 60.00 due Sep 23 is in the gap.
    # The $500 paycheck lands Sep 21, also before the range.
    # Range Sep 26-30 = 5 days -> 50.00 spending.
    # End: 1300 + 500 - 100 - 60 - 50 = 1590.00 (pre-fix: 1300 + 500 - 50 = 1750.00).
    assert data["projected_end_balance"] == D("1590.00")
    # Balance expected on Sep 25 (day before the range): 1300 + 500 - 100 - 60 = 1640.00.
    assert data["opening_balance"] == D("1640.00")
    # Change over the range alone: 1590 - 1640 = -50.00 (just the range's spending).
    assert data["forecast_surplus"] == D("-50.00")
    # The range's own forecasts are unchanged: 5 days x $10 and no bills due Sep 26-30.
    assert data["projected_spending_remaining"] == D("50.00")
    assert data["projected_bills_remaining"] == D("0.00")


# --- 4. Edit and delete release statement matches ------------------------------------------


def _matched_line(world, account, key, signed_amount, fingerprint):
    from budget.models import StatementLine
    from budget.statements import ParsedLine, ParsedSection, confirm_match, stage_statement

    staged = stage_statement(
        household=world["home"],
        actor=world["user"],
        account=account,
        section=ParsedSection(
            account_label=account.name,
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
            lines=[ParsedLine(date(2026, 10, 5), D(signed_amount), "MATCHED ITEM")],
        ),
        file_sha256=fingerprint * 64,
    )
    line = StatementLine.objects.get(statement=staged.statement)
    confirm_match(household=world["home"], actor=world["user"], line_id=line.pk, key=key)
    return line


def _state(line):
    from budget.models import StatementLine

    line = StatementLine.objects.get(pk=line.pk)
    return line.status, line.matched_key


@pytest.fixture
def matched_world(world, client):
    from budget.models import Refund, Transfer

    client.force_login(world["user"])
    session = client.session
    session["active_household_id"] = world["home"].pk
    session.save()
    home, user = world["home"], world["user"]
    expense = Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 10, 5),
        amount=D("40.00"),
        account=world["spending"],
        category=world["groceries"],
        merchant="Store",
    )
    refund = Refund.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 10, 5),
        amount=D("15.00"),
        account=world["spending"],
        category=world["groceries"],
        merchant="Store",
    )
    transfer = Transfer.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 10, 5),
        amount=D("25.00"),
        source=world["spending"],
        destination=world["bills"],
    )
    # Statement lines are signed like the ledger: expense -40.00, refund +15.00,
    # transfer out of Spending -25.00 and into Bills +25.00.
    lines = {
        "expense": _matched_line(world, world["spending"], f"expense:{expense.pk}", "-40.00", "a"),
        "refund": _matched_line(world, world["spending"], f"refund:{refund.pk}", "15.00", "b"),
        "out": _matched_line(
            world, world["spending"], f"transfer_out:{transfer.pk}", "-25.00", "c"
        ),
        "in": _matched_line(world, world["bills"], f"transfer_in:{transfer.pk}", "25.00", "d"),
    }
    return {
        **world,
        "client": client,
        "expense": expense,
        "refund": refund,
        "transfer": transfer,
        "lines": lines,
    }


def _edit(client, kind, obj, amount, **extra):
    data = {
        "date": "2026-10-05",
        "description": "Store",
        "amount": amount,
        "splits-TOTAL_FORMS": "0",
        "splits-INITIAL_FORMS": "0",
        **extra,
    }
    return client.post(reverse("transaction_edit", args=[kind, obj.pk]), data)


def test_matched_fixture_lines_are_matched(matched_world):
    w = matched_world
    assert _state(w["lines"]["expense"]) == ("matched", f"expense:{w['expense'].pk}")
    assert _state(w["lines"]["refund"]) == ("matched", f"refund:{w['refund'].pk}")


def test_editing_expense_amount_releases_its_match(matched_world):
    w = matched_world
    response = _edit(w["client"], "expense", w["expense"], "45.00", category=w["groceries"].pk)
    assert response.status_code == 302
    w["expense"].refresh_from_db()
    assert w["expense"].amount == D("45.00")  # 40.00 -> 45.00
    assert _state(w["lines"]["expense"]) == ("pending", "")


def test_editing_expense_without_changing_amount_keeps_its_match(matched_world):
    w = matched_world
    response = _edit(
        w["client"], "expense", w["expense"], "40.00", category=w["groceries"].pk, notes="n"
    )
    assert response.status_code == 302
    assert _state(w["lines"]["expense"]) == ("matched", f"expense:{w['expense'].pk}")


def test_editing_refund_amount_releases_its_match(matched_world):
    w = matched_world
    response = _edit(w["client"], "refund", w["refund"], "12.00", category=w["groceries"].pk)
    assert response.status_code == 302
    w["refund"].refresh_from_db()
    assert w["refund"].amount == D("12.00")  # 15.00 -> 12.00
    assert _state(w["lines"]["refund"]) == ("pending", "")


def test_editing_transfer_amount_releases_both_sides(matched_world):
    w = matched_world
    response = _edit(w["client"], "transfer", w["transfer"], "30.00")
    assert response.status_code == 302
    w["transfer"].refresh_from_db()
    assert w["transfer"].amount == D("30.00")  # 25.00 -> 30.00
    assert _state(w["lines"]["out"]) == ("pending", "")
    assert _state(w["lines"]["in"]) == ("pending", "")


def test_deleting_matched_entries_releases_their_lines(matched_world):
    w = matched_world
    for kind in ("expense", "refund", "transfer"):
        obj = w[kind]
        response = w["client"].post(
            reverse("transaction_delete", args=[kind, obj.pk]), {"quick": "1"}
        )
        assert response.status_code == 302
    assert not Expense.objects.exists()
    for name in ("expense", "refund", "out", "in"):
        assert _state(w["lines"][name]) == ("pending", ""), name


def test_undo_of_a_matched_delete_leaves_the_line_in_review(matched_world):
    w = matched_world
    w["client"].post(
        reverse("transaction_delete", args=["expense", w["expense"].pk]), {"quick": "1"}
    )
    assert _state(w["lines"]["expense"]) == ("pending", "")
    w["client"].post(reverse("transaction_undo"))
    restored = Expense.objects.get()
    assert restored.amount == D("40.00")
    # The restored entry has a new id and is not matched: the line stays in review.
    assert _state(w["lines"]["expense"]) == ("pending", "")


# --- 5. Bill payments and bill edits respect locked pay periods ------------------------------


@pytest.fixture
def locked_world(world, client):
    """Oct 1-31 is locked, Nov 1-30 is open. Gas $80 is due Oct 15 (locked month) and has a
    $50 payment dated Nov 5 (open period); Gas Nov 15 is due in the open period."""
    client.force_login(world["user"])
    session = client.session
    session["active_household_id"] = world["home"].pk
    session.save()
    home = world["home"]
    oct_period = make_period(home, date(2026, 10, 1), date(2026, 10, 31))
    make_period(home, date(2026, 11, 1), date(2026, 11, 30))
    bill = make_bill(home, "Gas", "80.00", world["bills"], world["utilities"])
    october = occurrence(bill, date(2026, 10, 15), "80.00")
    november = occurrence(bill, date(2026, 11, 15), "80.00")
    payment = Expense.objects.create(
        household=home,
        created_by=world["user"],
        date=date(2026, 11, 5),
        amount=D("50.00"),
        account=world["bills"],
        category=world["utilities"],
        bill_occurrence=october,
        merchant="Gas",
    )
    oct_period.is_locked = True
    oct_period.save(update_fields=["is_locked"])
    return {
        **world,
        "client": client,
        "bill": bill,
        "october": october,
        "november": november,
        "payment": payment,
    }


def test_late_payment_for_a_locked_bill_month_is_allowed(locked_world):
    from budget.services import create_expense

    w = locked_world
    expense = create_expense(
        household=w["home"],
        actor=w["user"],
        entry_date=date(2026, 11, 6),  # the payment's own date is in the open November period
        account=w["bills"],
        category=w["utilities"],
        amount=D("30.00"),
        bill=w["bill"],
        occurrence=w["october"],  # due Oct 15, a locked period
    )
    assert expense.amount == D("30.00")
    w["october"].refresh_from_db()
    # Oct bill is $80: 50.00 (fixture) + 30.00 = 80.00 paid, 80 - 80 = 0.00 remaining.
    assert w["october"].amount_paid == D("80.00")
    assert w["october"].status == BillOccurrence.Status.PAID


def test_editing_a_late_payment_of_a_locked_bill_month_is_allowed(locked_world):
    w = locked_world
    response = _edit(
        w["client"],
        "expense",
        w["payment"],
        "60.00",
        category=w["utilities"].pk,
        bill=w["bill"].pk,
        date="2026-11-05",
    )
    assert response.status_code == 302
    w["payment"].refresh_from_db()
    assert w["payment"].amount == D("60.00")  # 50.00 -> 60.00
    w["october"].refresh_from_db()
    # 80 bill - 60 paid = 20.00 remaining, so it is partly paid.
    assert w["october"].remaining_amount == D("20.00")


def test_deleting_a_late_payment_of_a_locked_bill_month_is_allowed(locked_world):
    w = locked_world
    w["client"].post(
        reverse("transaction_delete", args=["expense", w["payment"].pk]), {"quick": "1"}
    )
    assert not Expense.objects.filter(pk=w["payment"].pk).exists()
    w["october"].refresh_from_db()
    assert w["october"].amount_paid == D("0.00")  # 50.00 removed -> 0.00 paid


def test_undo_of_a_late_payment_delete_is_allowed(locked_world):
    w = locked_world
    data = undo.snapshot("expense", w["payment"], "Gas")
    w["payment"].delete()
    undo.restore(w["home"], w["user"], data)
    restored = Expense.objects.get()
    assert restored.amount == D("50.00")
    assert restored.bill_occurrence_id == w["october"].pk


def test_payment_dated_in_the_locked_period_is_still_refused(locked_world):
    from budget.services import create_expense

    w = locked_world
    with pytest.raises(ValidationError, match="locked"):
        create_expense(
            household=w["home"],
            actor=w["user"],
            entry_date=date(2026, 10, 20),  # Oct is locked
            account=w["bills"],
            category=w["utilities"],
            amount=D("30.00"),
            bill=w["bill"],
            occurrence=w["october"],
        )
    assert Expense.objects.count() == 1  # only the 50.00 fixture payment
    response = _edit(
        w["client"],
        "expense",
        w["payment"],
        "50.00",
        category=w["utilities"].pk,
        bill=w["bill"].pk,
        date="2026-10-20",  # moving it into the locked period
    )
    assert response.status_code == 200
    w["payment"].refresh_from_db()
    assert w["payment"].date == date(2026, 11, 5)


def test_mark_paid_allows_a_late_payment_for_a_locked_bill_month(locked_world):
    w = locked_world
    expense = pay_bill_occurrence(w["october"], w["user"], date(2026, 11, 6))
    # Oct bill $80, $50 already paid: 80 - 50 = 30.00 remaining is paid.
    assert expense.amount == D("30.00")
    w["october"].refresh_from_db()
    assert w["october"].status == BillOccurrence.Status.PAID
    assert w["october"].remaining_amount == D("0.00")  # 50 + 30 = 80 paid


def test_mark_paid_dated_in_the_locked_period_is_still_refused(locked_world):
    w = locked_world
    with pytest.raises(ValidationError, match="locked"):
        pay_bill_occurrence(w["october"], w["user"], date(2026, 10, 20))
    assert Expense.objects.count() == 1  # only the 50.00 fixture payment


def test_bill_edit_amount_refuses_when_a_later_month_is_locked(locked_world):
    w = locked_world
    # Edit the OPEN November month and move the bill to Spending: that rewrites later months'
    # accounts. Lock December as the later month so the bulk update must be refused.
    dec_period = make_period(w["home"], date(2026, 12, 1), date(2026, 12, 31))
    december = occurrence(w["bill"], date(2026, 12, 15), "80.00")
    dec_period.is_locked = True
    dec_period.save(update_fields=["is_locked"])
    response = w["client"].post(
        reverse("bill_edit_amount", args=[w["november"].pk]),
        {
            "bill_name": "Gas",
            "category": w["utilities"].pk,
            "account": w["spending"].pk,
            "due_date": "2026-11-15",
            "confirmed_amount": "80.00",
            "notes": "",
        },
    )
    assert response.status_code == 200
    december.refresh_from_db()
    assert december.account_id in (None, w["bills"].pk)  # not moved to Spending
    assert december.account_id != w["spending"].pk
    w["bill"].refresh_from_db()
    assert w["bill"].account_id == w["bills"].pk


# --- 6. Low-severity money fixes --------------------------------------------------------------


def test_automatic_bill_month_never_picks_a_skipped_month(world):
    from budget.services import create_expense

    home = world["home"]
    bill = make_bill(home, "Phone", "70.00", world["bills"], world["utilities"])
    # Oct 15 is skipped; there is no other month within 45 days of an Oct 20 payment
    # except the calendar-month fallback, which is the skipped one.
    skipped = occurrence(bill, date(2026, 10, 15), "70.00", status=BillOccurrence.Status.SKIPPED)
    problems = None
    try:
        create_expense(
            household=home,
            actor=world["user"],
            entry_date=date(2026, 10, 20),
            account=world["bills"],
            category=world["utilities"],
            amount=D("70.00"),
            bill=bill,
        )
    except ValidationError as exc:
        problems = exc.messages
    assert problems == ["A skipped bill month cannot receive a payment."]
    skipped.refresh_from_db()
    assert skipped.expenses.count() == 0
    assert Expense.objects.count() == 0


def test_automatic_bill_month_prefers_a_live_neighbour_over_the_skipped_month(world):
    from budget.services import create_expense

    home = world["home"]
    bill = make_bill(home, "Phone", "70.00", world["bills"], world["utilities"])
    occurrence(bill, date(2026, 10, 15), "70.00", status=BillOccurrence.Status.SKIPPED)
    november = occurrence(bill, date(2026, 11, 15), "70.00")
    expense = create_expense(
        household=home,
        actor=world["user"],
        entry_date=date(2026, 10, 20),  # Nov 15 is 26 days away: inside the 45-day window
        account=world["bills"],
        category=world["utilities"],
        amount=D("70.00"),
        bill=bill,
    )
    assert expense.bill_occurrence_id == november.pk


def test_income_split_across_two_savings_accounts_records_a_goal_entry_for_each(world):
    from budget.models import SavingsGoalEntry
    from budget.services import create_income, ensure_savings_goals

    home = world["home"]
    first = Account.objects.create(household=home, name="Savings A", type=Account.Type.SAVINGS)
    second = Account.objects.create(household=home, name="Savings B", type=Account.Type.SAVINGS)
    goal, _ = ensure_savings_goals(home)
    create_income(
        household=home,
        actor=world["user"],
        income_date=date(2026, 10, 1),
        source="Bonus",
        amount=D("500.00"),
        allocations=[(first, D("300.00")), (second, D("200.00"))],
        savings_goal=goal,
    )
    entries = SavingsGoalEntry.objects.filter(goal__household=home)
    # 300.00 + 200.00 = 500.00 of goal money for 500.00 of Savings-account income.
    assert sorted(entries.values_list("amount", flat=True)) == [D("200.00"), D("300.00")]
    assert sum((e.amount for e in entries), D("0.00")) == D("500.00")


def test_redating_a_savings_inflow_after_spending_it_is_rejected(logged_savings):
    from budget.tests.test_savings_integrity import income_with_savings, spend_from_goal

    client, home, user, savings, spending, goal = logged_savings
    income = income_with_savings(
        home, user, savings, spending, goal, amount="300.00", income_date=date(2026, 10, 1)
    )
    spend_from_goal(home, user, savings, goal, "250.00", entry_date=date(2026, 10, 10))
    response = client.post(
        reverse("transaction_edit", args=["income", income.pk]),
        {
            "date": "2026-10-20",  # after the Oct 10 spending
            "description": income.source,
            "amount": "400.00",  # 300.00 Savings + 100.00 Spending
            "notes": "",
            f"allocation_{savings.pk}": "300.00",
            f"allocation_{spending.pk}": "100.00",
            "savings_destination_goal": goal.pk,
        },
    )
    assert response.status_code == 200  # refused, form shown again
    income.refresh_from_db()
    assert income.date == date(2026, 10, 1)
    entry = income.allocations.get(account=savings).savings_goal_entries.get()
    assert entry.date == date(2026, 10, 1)


def test_redating_a_savings_inflow_earlier_is_allowed(logged_savings):
    from budget.tests.test_savings_integrity import income_with_savings, spend_from_goal

    client, home, user, savings, spending, goal = logged_savings
    income = income_with_savings(
        home, user, savings, spending, goal, amount="300.00", income_date=date(2026, 10, 5)
    )
    spend_from_goal(home, user, savings, goal, "250.00", entry_date=date(2026, 10, 10))
    response = client.post(
        reverse("transaction_edit", args=["income", income.pk]),
        {
            "date": "2026-10-02",
            "description": income.source,
            "amount": "400.00",
            "notes": "",
            f"allocation_{savings.pk}": "300.00",
            f"allocation_{spending.pk}": "100.00",
            "savings_destination_goal": goal.pk,
        },
    )
    assert response.status_code == 302


@pytest.fixture
def logged_savings(client, savings_setup):  # noqa: F811
    from budget.services import create_savings_goal

    home, user, savings, spending, _ = savings_setup
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    # A goal with no money of its own: only the income below ever funds it.
    goal = create_savings_goal(
        household=home, actor=user, name="Trip", target_amount="900.00", priority=2
    )
    return client, home, user, savings, spending, goal


def test_bill_add_is_all_or_nothing(world, client, monkeypatch):
    from budget import views
    from budget.models import RecurringBill

    client.force_login(world["user"])
    session = client.session
    session["active_household_id"] = world["home"].pk
    session.save()

    def explode(*args, **kwargs):
        raise RuntimeError("occurrence generation failed")

    monkeypatch.setattr(views, "ensure_bill_occurrences", explode)
    client.raise_request_exception = False
    response = client.post(
        reverse("bill_add"),
        {
            "name": "Trash",
            "default_amount": "25.00",
            "due_day": "5",
            "every_months": "1",
            "category": world["utilities"].pk,
            "account": world["bills"].pk,
            "effective_start": "2026-11-01",
            "active": "on",
        },
    )
    assert response.status_code == 500
    # The bill must not be left behind without its months.
    assert RecurringBill.objects.count() == 0


@pytest.mark.parametrize("typed", ["1e3", "1E3", "1e-2", "5e0", "1_000", "Infinity", "NaN"])
def test_money_field_rejects_exponent_and_other_decimal_spellings(typed):
    from django import forms

    from budget.money import INVALID_AMOUNT, MoneyField

    with pytest.raises(forms.ValidationError) as error:
        MoneyField().clean(typed)
    # "1e3" would otherwise be read as 1000; "1_000" is also a Python Decimal spelling.
    assert error.value.messages == [INVALID_AMOUNT]


def test_paycheck_tie_goes_to_the_later_payday_not_the_higher_id(world):
    from budget.pay_periods import paycheck_period

    home = world["home"]
    # Created first (lower id) but the LATER payday: Oct 25 is 5 days after a deposit on Oct 20.
    later = make_period(home, date(2026, 10, 24), date(2026, 11, 8))
    later.nominal_payday = date(2026, 10, 25)
    later.save(update_fields=["nominal_payday"])
    # Created second (higher id) with the EARLIER payday: Oct 15 is 5 days before the deposit.
    earlier = make_period(home, date(2026, 10, 9), date(2026, 10, 23))
    earlier.nominal_payday = date(2026, 10, 15)
    earlier.save(update_fields=["nominal_payday"])
    assert earlier.pk > later.pk
    # Both paydays are 5 days from the deposit (|Oct 25 - Oct 20| = |Oct 15 - Oct 20| = 5).
    chosen = paycheck_period([earlier, later], date(2026, 10, 20))
    assert chosen is not None
    assert chosen.nominal_payday == date(2026, 10, 25)


def test_seed_demo_profile_uses_the_local_date(monkeypatch, db):
    from django.core.management import call_command
    from django.utils import timezone

    monkeypatch.setenv("BROWSER_TEST_MODE", "true")
    frozen = date(2030, 3, 15)
    monkeypatch.setattr(timezone, "localdate", lambda *args, **kwargs: frozen)
    call_command("seed_demo_profile")
    newest = Expense.objects.order_by("-date").first()
    assert newest is not None
    # Demo entries are dated back from "today" (here Mar 15, 2030).
    assert date(2030, 2, 1) <= newest.date <= frozen


# --- 7. Double submit and double Undo --------------------------------------------------------


def test_savings_move_form_submitted_twice_moves_money_once(logged_savings):
    import uuid

    from budget.models import Transfer
    from budget.services import account_balance, ensure_savings_goals

    client, home, user, savings, spending, _ = logged_savings
    emergency, _ = ensure_savings_goals(home)  # holds the 1000.00 opening Savings balance
    payload = {
        "date": "2026-11-18",
        "amount": "50.00",
        "destination_account": spending.pk,
        "notes": "",
        "form_token": str(uuid.uuid4()),
        "savings-TOTAL_FORMS": "1",
        "savings-INITIAL_FORMS": "0",
        "savings-0-goal": emergency.pk,
        "savings-0-amount": "50.00",
    }
    first = client.post(reverse("savings_move"), payload)
    second = client.post(reverse("savings_move"), payload)  # a double tap: same token
    assert first.status_code == 302
    assert second.status_code == 302
    # One transfer of 50.00 only: Savings 1000.00 - 50.00 = 950.00 (not 900.00).
    assert Transfer.objects.count() == 1
    assert account_balance(savings) == D("950.00")


def test_undo_cannot_restore_twice_even_with_a_stale_session_read(world, client, monkeypatch):
    client.force_login(world["user"])
    session = client.session
    session["active_household_id"] = world["home"].pk
    session.save()
    expense = Expense.objects.create(
        household=world["home"],
        created_by=world["user"],
        date=date(2026, 11, 10),
        amount=D("40.00"),
        account=world["spending"],
        category=world["groceries"],
        merchant="Store",
    )
    client.post(reverse("transaction_delete", args=["expense", expense.pk]), {"quick": "1"})
    snapshot = client.session[undo.SESSION_KEY]
    client.post(reverse("transaction_undo"))
    assert Expense.objects.count() == 1  # restored once: 40.00
    # A second worker that read the session before the first Undo committed still sees the
    # snapshot; the claim must find it already taken in the stored session.
    monkeypatch.setattr(undo, "pending", lambda session, household, key=undo.SESSION_KEY: snapshot)
    client.post(reverse("transaction_undo"))
    assert Expense.objects.count() == 1  # still one 40.00 expense, not two
    assert Expense.objects.get().amount == D("40.00")
