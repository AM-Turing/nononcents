from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from budget.models import (
    Account,
    AuditEvent,
    BalanceAdjustment,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    Income,
    IncomeAllocation,
    PayPeriod,
    Reconciliation,
    Refund,
    Transfer,
)
from budget.reconciliation import account_ledger
from budget.services import account_balance
from budget.views import category_totals_for_period, report_period_totals

D = Decimal


@pytest.fixture
def setup(client, db):
    user = get_user_model().objects.create_user("recon", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spending = Account.objects.create(
        household=home, name="Spending", type="spending", opening_balance=D("100.00")
    )
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    return {
        "client": client,
        "user": user,
        "home": home,
        "spending": spending,
        "bills": bills,
        "groceries": groceries,
    }


def expense(s, amount, day, account=None, merchant="Shop"):
    return Expense.objects.create(
        household=s["home"],
        created_by=s["user"],
        date=date(2026, 8, day),
        amount=D(amount),
        account=account or s["spending"],
        category=s["groceries"],
        merchant=merchant,
    )


def income(s, amount, day, account=None):
    inc = Income.objects.create(
        household=s["home"],
        created_by=s["user"],
        date=date(2026, 8, day),
        amount=D(amount),
        source="Pay",
    )
    IncomeAllocation.objects.create(income=inc, account=account or s["spending"], amount=D(amount))
    return inc


def url(s):
    return reverse("account_reconcile", args=[s["spending"].pk])


def post(s, **data):
    base = {"statement_date": "2026-08-10", "statement_balance": "50.00", "action": "reconcile"}
    base.update(data)
    return s["client"].post(url(s), base)


def test_zero_difference_clears_selected_items(setup):
    e1 = expense(setup, "30.00", 2)
    e2 = expense(setup, "20.00", 3)
    response = post(setup, clear=[f"expense:{e1.pk}"])
    assert response.status_code == 302
    e1.refresh_from_db()
    e2.refresh_from_db()
    assert e1.cleared_at is not None
    assert e2.cleared_at is None
    rec = Reconciliation.objects.get()
    assert rec.adjustment is None
    assert rec.ledger_balance == D("50.00")
    assert AuditEvent.objects.filter(record_type="budget.Reconciliation").count() == 1
    assert not BalanceAdjustment.objects.exists()


def test_difference_without_correction_writes_nothing(setup):
    e1 = expense(setup, "30.00", 2)
    response = post(setup, statement_balance="80.00", clear=[f"expense:{e1.pk}"])
    assert response.status_code == 200
    assert "post a balance correction" in response.content.decode()
    e1.refresh_from_db()
    assert e1.cleared_at is None
    assert not Reconciliation.objects.exists()
    assert not BalanceAdjustment.objects.exists()


@pytest.mark.parametrize(("bank", "increase"), [("80.00", True), ("60.00", False)])
def test_correction_is_signed_dated_and_matches_bank(setup, bank, increase):
    expense(setup, "30.00", 2)
    response = post(setup, statement_balance=bank, post_correction="on", reason="Bank fee")
    assert response.status_code == 302
    adj = BalanceAdjustment.objects.get()
    assert adj.is_increase is increase
    assert adj.amount == abs(D(bank) - D("70.00"))
    assert adj.date == date(2026, 8, 10)
    assert adj.cleared_at is not None
    assert account_balance(setup["spending"], through=date(2026, 8, 10)) == D(bank)
    rec = Reconciliation.objects.get()
    assert rec.adjustment == adj
    assert rec.ledger_balance == D("70.00")
    event = AuditEvent.objects.get(record_type="budget.Reconciliation")
    assert "Bank fee" not in str(event.changes)


def test_correction_requires_reason(setup):
    expense(setup, "30.00", 2)
    response = post(setup, statement_balance="80.00", post_correction="on", reason=" ")
    assert response.status_code == 200
    assert not BalanceAdjustment.objects.exists()
    long = post(setup, statement_balance="80.00", post_correction="on", reason="x" * 256)
    assert long.status_code == 200
    assert not BalanceAdjustment.objects.exists()


def test_tampered_ledger_numbers_are_ignored(setup):
    expense(setup, "30.00", 2)
    response = post(
        setup,
        statement_balance="80.00",
        ledger_balance="80.00",
        difference="0.00",
        adjustment_amount="0.00",
    )
    assert response.status_code == 200
    assert not Reconciliation.objects.exists()


def test_cannot_clear_foreign_or_later_items(setup):
    other_home = Household.objects.create()
    foreign_account = Account.objects.create(household=other_home, name="Spending", type="spending")
    foreign_cat = Category.objects.create(household=other_home, name="G", type="spending")
    foreign = Expense.objects.create(
        household=other_home,
        date=date(2026, 8, 2),
        amount=D("5.00"),
        account=foreign_account,
        category=foreign_cat,
    )
    other_account = expense(setup, "5.00", 2, account=setup["bills"])
    later = expense(setup, "5.00", 15)
    for item in (foreign, other_account, later):
        response = post(setup, statement_balance="100.00", clear=[f"expense:{item.pk}"])
        assert response.status_code == 200
        item.refresh_from_db()
        assert item.cleared_at is None
    assert not Reconciliation.objects.exists()


def test_locked_period_rejects_reconciliation(setup):
    expense(setup, "30.00", 2)
    PayPeriod.objects.create(
        household=setup["home"],
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 15),
        is_locked=True,
    )
    response = post(setup, statement_balance="80.00", post_correction="on", reason="Fix")
    assert response.status_code == 200
    assert "locked" in response.content.decode()
    assert not Reconciliation.objects.exists()
    assert not BalanceAdjustment.objects.exists()


def test_compare_lists_uncleared_items_and_writes_nothing(setup):
    expense(setup, "30.00", 2)
    expense(setup, "9.00", 20, merchant="Later")
    response = setup["client"].post(
        url(setup),
        {"statement_date": "2026-08-10", "statement_balance": "60.00", "action": "compare"},
    )
    compare = response.context["compare"]
    assert compare["ledger_balance"] == D("70.00")
    assert compare["difference"] == D("-10.00")
    assert [e["item"].description for e in compare["items"]] == ["Shop"]
    assert not Reconciliation.objects.exists()
    assert setup["client"].get(url(setup)).status_code == 200


def test_correction_does_not_change_spending_income_or_category_totals(setup):
    expense(setup, "30.00", 2)
    income(setup, "200.00", 3)
    start, end = date(2026, 8, 1), date(2026, 8, 31)
    before_totals = report_period_totals(setup["home"], start, end)
    before_categories = category_totals_for_period(setup["home"], start, end)
    before_balance = account_balance(setup["spending"])
    post(setup, statement_balance="300.00", post_correction="on", reason="Opening drift")
    assert report_period_totals(setup["home"], start, end) == before_totals
    assert category_totals_for_period(setup["home"], start, end) == before_categories
    assert account_balance(setup["spending"]) != before_balance


def test_accounts_page_summarises_balances_and_uncleared(setup):
    e1 = expense(setup, "30.00", 2)
    expense(setup, "20.00", 3)
    post(setup, statement_balance="70.00", clear=[f"expense:{e1.pk}"], statement_date="2026-08-02")
    response = setup["client"].get(reverse("accounts"))
    rows = {r["account"].name: r for r in response.context["rows"]}
    assert rows["Spending"]["balance"] == D("50.00")
    assert rows["Spending"]["last"].statement_balance == D("70.00")
    assert rows["Spending"]["uncleared_count"] == 1
    assert rows["Spending"]["uncleared_total"] == D("-20.00")


def test_views_are_login_and_household_scoped(setup, client):
    other_home = Household.objects.create()
    other = Account.objects.create(household=other_home, name="X", type="spending")
    assert setup["client"].get(reverse("account_reconcile", args=[other.pk])).status_code == 404
    names = [r["account"].name for r in setup["client"].get(reverse("accounts")).context["rows"]]
    assert "X" not in names
    client.logout()
    assert client.get(reverse("accounts")).status_code == 302
    outsider = get_user_model().objects.create_user("out", password="a-secure-password-123")
    client.force_login(outsider)
    assert client.get(reverse("accounts")).status_code == 403


def test_editing_amount_resets_cleared_but_notes_do_not(setup):
    e = expense(setup, "30.00", 2)
    Expense.objects.filter(pk=e.pk).update(cleared_at=timezone.now())
    edit = reverse("transaction_edit", args=["expense", e.pk])
    data = {
        "date": "2026-08-03",
        "description": "Shop",
        "amount": "30.00",
        "category": setup["groceries"].pk,
        "notes": "new note",
    }
    assert setup["client"].post(edit, data).status_code == 302
    e.refresh_from_db()
    assert e.cleared_at is not None
    response = setup["client"].post(edit, {**data, "amount": "31.00"}, follow=True)
    e.refresh_from_db()
    assert e.cleared_at is None
    assert "marked uncleared because the amount changed" in response.content.decode()


def test_editing_transfer_amount_resets_both_sides(setup):
    t = Transfer.objects.create(
        household=setup["home"],
        date=date(2026, 8, 2),
        amount=D("10.00"),
        source=setup["spending"],
        destination=setup["bills"],
        source_cleared_at=timezone.now(),
        destination_cleared_at=timezone.now(),
    )
    setup["client"].post(
        reverse("transaction_edit", args=["transfer", t.pk]),
        {"date": "2026-08-02", "description": "", "amount": "12.00"},
    )
    t.refresh_from_db()
    assert t.amount == D("12.00")
    assert t.source_cleared_at is None and t.destination_cleared_at is None


# ---- history ----


def history(s, **params):
    return s["client"].get(reverse("transactions"), params)


def test_history_account_and_type_filters(setup):
    expense(setup, "10.00", 2)
    expense(setup, "5.00", 2, account=setup["bills"], merchant="Billy")
    income(setup, "40.00", 3)
    Transfer.objects.create(
        household=setup["home"],
        date=date(2026, 8, 4),
        amount=D("7.00"),
        source=setup["spending"],
        destination=setup["bills"],
    )
    BalanceAdjustment.objects.create(
        household=setup["home"],
        account=setup["bills"],
        date=date(2026, 8, 5),
        amount=D("3.00"),
        is_increase=True,
        reason="Drift",
    )
    assert len(history(setup).context["rows"]) == 5
    by_account = history(setup, account=setup["bills"].pk).context["rows"]
    assert {r["type"] for r in by_account} == {"Expense", "Transfer", "Balance correction"}
    assert [r["type"] for r in history(setup, type="income").context["rows"]] == ["Income"]
    adj = history(setup, type="adjustment").context["rows"]
    assert adj[0]["description"] == "Drift" and adj[0]["sign"] == 1
    assert "Balance correction" in history(setup, type="adjustment").content.decode()
    assert history(setup, account="999999").context["selected_account"] is None


def test_history_transfer_signs_and_income_allocation_per_account(setup):
    inc = income(setup, "40.00", 3)
    IncomeAllocation.objects.create(income=inc, account=setup["bills"], amount=D("60.00"))
    Transfer.objects.create(
        household=setup["home"],
        date=date(2026, 8, 4),
        amount=D("7.00"),
        source=setup["spending"],
        destination=setup["bills"],
    )
    spending = {r["type"]: r for r in history(setup, account=setup["spending"].pk).context["rows"]}
    bills = {r["type"]: r for r in history(setup, account=setup["bills"].pk).context["rows"]}
    assert (spending["Transfer"]["sign"], spending["Transfer"]["amount"]) == (-1, D("7.00"))
    assert (bills["Transfer"]["sign"], bills["Transfer"]["amount"]) == (1, D("7.00"))
    assert spending["Income"]["amount"] == D("40.00")
    assert bills["Income"]["amount"] == D("60.00")


def test_history_cleared_indicator(setup):
    e = expense(setup, "10.00", 2)
    assert history(setup).context["rows"][0]["cleared"] == "Not cleared"
    Expense.objects.filter(pk=e.pk).update(cleared_at=timezone.now())
    row = history(setup, account=setup["spending"].pk).context["rows"][0]
    assert row["cleared"] == "Cleared"
    assert "Cleared" in history(setup).content.decode()


def test_running_balance_across_pages_and_same_day_rows(setup):
    # 60 same-day expenses of 1.00 plus an earlier income; opening balance is 100.00.
    income(setup, "10.00", 1)
    for _ in range(60):
        expense(setup, "1.00", 2)
    first = history(setup, account=setup["spending"].pk)
    rows = first.context["rows"]
    assert len(rows) == 50 and first.context["page_obj"].paginator.num_pages == 2
    # Newest first: final balance is 100 + 10 - 60 = 50.00 and drops 1.00 per row upward.
    assert rows[0]["balance_after"] == D("50.00")
    assert rows[1]["balance_after"] == D("51.00")
    second = history(setup, account=setup["spending"].pk, page=2).context["rows"]
    assert len(second) == 11
    assert second[-1]["balance_after"] == D("110.00")
    assert second[0]["balance_after"] == D("100.00")
    ledger = account_ledger(setup["spending"])
    assert ledger[-1].balance_after == account_balance(setup["spending"])


def test_running_balance_ignores_other_filters(setup):
    expense(setup, "10.00", 2, merchant="Alpha")
    expense(setup, "5.00", 3, merchant="Beta")
    rows = history(setup, account=setup["spending"].pk, q="Alpha").context["rows"]
    assert len(rows) == 1 and rows[0]["balance_after"] == D("90.00")


def test_pagination_links_preserve_filters(setup):
    for _ in range(55):
        expense(setup, "1.00", 2)
    response = history(setup, account=setup["spending"].pk, type="expense", q="Shop")
    content = response.content.decode()
    assert "page=2" in content
    assert "account=" in content and "type=expense" in content and "q=Shop" in content
    assert len(response.context["rows"]) == 50
    assert len(history(setup, page=2).context["rows"]) == 5


def test_delete_balance_correction_flow(setup):
    adj = BalanceAdjustment.objects.create(
        household=setup["home"],
        account=setup["spending"],
        date=date(2026, 8, 5),
        amount=D("3.00"),
        is_increase=True,
        reason="Drift",
    )
    delete = reverse("adjustment_delete", args=[adj.pk])
    assert setup["client"].get(delete).status_code == 200
    bad = setup["client"].post(delete, {"confirmation": "nope"})
    assert bad.status_code == 200 and BalanceAdjustment.objects.exists()
    ok = setup["client"].post(delete, {"confirmation": "Balance correction: $3.00"})
    assert ok.status_code == 302
    assert not BalanceAdjustment.objects.exists()
    assert AuditEvent.objects.filter(record_type="budget.BalanceAdjustment", action="delete")


def test_delete_balance_correction_respects_lock_and_household(setup):
    adj = BalanceAdjustment.objects.create(
        household=setup["home"],
        account=setup["spending"],
        date=date(2026, 8, 5),
        amount=D("3.00"),
        is_increase=True,
        reason="Drift",
    )
    PayPeriod.objects.create(
        household=setup["home"],
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 15),
        is_locked=True,
    )
    delete = reverse("adjustment_delete", args=[adj.pk])
    response = setup["client"].post(delete, {"confirmation": "Balance correction: $3.00"})
    assert response.status_code == 200
    assert BalanceAdjustment.objects.exists()
    other_home = Household.objects.create()
    foreign = BalanceAdjustment.objects.create(
        household=other_home,
        account=Account.objects.create(household=other_home, name="Z", type="other"),
        date=date(2026, 8, 5),
        amount=D("1.00"),
        is_increase=True,
        reason="x",
    )
    assert setup["client"].get(reverse("adjustment_delete", args=[foreign.pk])).status_code == 404


def test_refund_and_future_date_guard(setup):
    Refund.objects.create(
        household=setup["home"],
        date=date(2026, 8, 2),
        amount=D("4.00"),
        account=setup["spending"],
        category=setup["groceries"],
    )
    future = (timezone.localdate() + timedelta(days=3)).isoformat()
    response = post(setup, statement_date=future)
    assert response.status_code == 200
    assert not Reconciliation.objects.exists()


@pytest.mark.parametrize("bank", ["130.00", "70.00"])
def test_savings_correction_keeps_goals_equal_to_ledger(setup, bank):
    from django.db.models import Sum

    from budget.models import SavingsGoal, SavingsGoalEntry
    from budget.services import savings_ledger_balance

    savings = Account.objects.create(household=setup["home"], name="Savings", type="savings")
    goal = SavingsGoal.objects.create(household=setup["home"], name="Trip", target_amount=500)
    SavingsGoal.objects.create(
        household=setup["home"], name="Unallocated Savings", target_amount=0, is_system=True
    )
    deposit = income(setup, "100.00", 1, account=savings)
    SavingsGoalEntry.objects.create(
        goal=goal,
        amount=D("100.00"),
        date=deposit.date,
        kind=SavingsGoalEntry.Kind.INCOME,
        income_allocation=deposit.allocations.get(),
    )

    def goal_total():
        return SavingsGoalEntry.objects.filter(goal__household=setup["home"]).aggregate(
            v=Sum("amount")
        )["v"]

    response = setup["client"].post(
        reverse("account_reconcile", args=[savings.pk]),
        {
            "statement_date": "2026-08-10",
            "statement_balance": bank,
            "action": "reconcile",
            "post_correction": "on",
            "reason": "Interest and fees",
        },
    )
    assert response.status_code == 302
    assert goal_total() == savings_ledger_balance(setup["home"]) == D(bank)
    entry = SavingsGoalEntry.objects.get(kind=SavingsGoalEntry.Kind.RECONCILIATION)
    assert entry.goal.is_system and entry.amount == D(bank) - D("100.00")

    adj = BalanceAdjustment.objects.get()
    delete = reverse("adjustment_delete", args=[adj.pk])
    sign = f"${adj.amount:,.2f}"
    assert (
        setup["client"].post(delete, {"confirmation": f"Balance correction: {sign}"}).status_code
        == 302
    )
    assert goal_total() == savings_ledger_balance(setup["home"]) == D("100.00")


def test_spending_correction_creates_no_goal_entry(setup):
    from budget.models import SavingsGoalEntry

    expense(setup, "30.00", 2)
    post(setup, statement_balance="80.00", post_correction="on", reason="Bank fee")
    assert not SavingsGoalEntry.objects.exists()


def test_dashboard_links_each_account_to_reconcile(setup):
    response = setup["client"].get(reverse("dashboard"))
    assert response.status_code == 200
    assert reverse("account_reconcile", args=[setup["spending"].pk]) in response.content.decode()
