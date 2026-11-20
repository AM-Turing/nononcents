"""P8: History pages in SQL-light fashion and Bills reads each Paid figure once.

Money tests: every expected amount is worked out by hand beside the assertion.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget import views
from budget.models import (
    Account,
    BalanceAdjustment,
    BillOccurrence,
    Category,
    Expense,
    ExpenseSplit,
    Household,
    HouseholdMembership,
    Income,
    IncomeAllocation,
    RecurringBill,
    Refund,
    Transfer,
)

CLEARED = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
D = Decimal


@pytest.fixture
def world(client, db):
    user = get_user_model().objects.create_user("p8-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    return client, home, user


@pytest.fixture
def history(world):
    """60 movements. Spending opens at 1000.00, Bills at 500.00, Savings at 200.00."""
    client, home, user = world
    spending = Account.objects.create(
        household=home, name="Spending", type="spending", opening_balance="1000.00"
    )
    bills = Account.objects.create(
        household=home, name="Bills", type="bills", opening_balance="500.00"
    )
    savings = Account.objects.create(
        household=home, name="Savings", type="savings", opening_balance="200.00"
    )
    food = Category.objects.create(household=home, name="Groceries", type="spending")
    home_cat = Category.objects.create(household=home, name="Household", type="spending")
    common = {"household": home, "created_by": user}
    # 2026-08-01: pay 2000.00 = 1500.00 to Spending (cleared) + 500.00 to Bills (not cleared)
    pay = Income.objects.create(date=date(2026, 8, 1), amount="2000.00", source="Pay", **common)
    IncomeAllocation.objects.create(
        income=pay, account=spending, amount="1500.00", cleared_at=CLEARED
    )
    IncomeAllocation.objects.create(income=pay, account=bills, amount="500.00")
    # 2026-08-02: Spending -> Savings 300.00, only the Spending side cleared
    Transfer.objects.create(
        date=date(2026, 8, 2),
        amount="300.00",
        source=spending,
        destination=savings,
        source_cleared_at=CLEARED,
        **common,
    )
    # 2026-08-03: refund 25.00 into Spending, then a 100.00 split purchase (60.00 + 40.00)
    Refund.objects.create(
        date=date(2026, 8, 3),
        amount="25.00",
        account=spending,
        category=food,
        merchant="Shop",
        **common,
    )
    split = Expense.objects.create(
        date=date(2026, 8, 3),
        amount="100.00",
        account=spending,
        category=food,
        merchant="Big Store",
        cleared_at=CLEARED,
        **common,
    )
    ExpenseSplit.objects.create(expense=split, category=food, amount="60.00")
    ExpenseSplit.objects.create(expense=split, category=home_cat, amount="40.00")
    # 2026-08-10: balance correction +10.00 on Spending
    BalanceAdjustment.objects.create(
        date=date(2026, 8, 10),
        amount="10.00",
        account=spending,
        is_increase=True,
        reason="Found cash",
        **common,
    )
    # 55 purchases of 2.00 from 2026-09-01 (two a day), Spending, not cleared
    for i in range(55):
        Expense.objects.create(
            date=date(2026, 9, 1 + i // 2),
            amount="2.00",
            account=spending,
            category=food,
            merchant=f"Shop {i:02d}",
            **common,
        )
    return client, spending, bills, savings


def figures(response):
    return [
        (r["type"], r["amount"], r["sign"], r["balance_after"], r["cleared"])
        for r in response.context["rows"]
    ]


def test_history_without_account_filter_pages_and_keeps_every_figure(history):
    client = history[0]
    page1 = client.get(reverse("transactions"))
    page2 = client.get(reverse("transactions"), {"page": 2})
    # 5 admin rows + 55 purchases = 60 rows, 50 per page.
    assert page1.context["page_obj"].paginator.count == 60
    assert len(page1.context["rows"]) == 50 and len(page2.context["rows"]) == 10
    # Newest first. Page 1 is purchases 54..5 (each 2.00, no running balance without a filter).
    assert figures(page1)[0] == ("Expense", D("2.00"), -1, None, "Not cleared")
    assert figures(page1)[-1] == ("Expense", D("2.00"), -1, None, "Not cleared")
    # Page 2: purchases 4..0, then the five older rows in date order, newest first.
    assert figures(page2)[:5] == [("Expense", D("2.00"), -1, None, "Not cleared")] * 5
    assert figures(page2)[5:] == [
        ("Balance correction", D("10.00"), 1, None, "Not cleared"),  # 2026-08-10 +10.00
        ("Expense", D("100.00"), -1, None, "Cleared"),  # 2026-08-03 split purchase
        ("Refund", D("25.00"), 1, None, "Not cleared"),  # 2026-08-03 refund
        ("Transfer", D("300.00"), 0, None, "Partly cleared"),  # 2026-08-02, one side cleared
        ("Income", D("2000.00"), 1, None, "Partly cleared"),  # 2026-08-01 1500 cleared + 500 not
    ]
    descriptions = [r["description"] for r in page1.context["rows"]]
    assert descriptions[0] == "Shop 54" and descriptions[-1] == "Shop 05"
    split_row = page2.context["rows"][6]
    assert split_row["category"] == "Split — Groceries: $60.00; Household: $40.00"
    income_row = page2.context["rows"][9]
    assert income_row["account"] == "Bills: $500.00; Spending: $1,500.00"


def test_history_for_spending_account_keeps_running_balance_after_each_row(history):
    client, spending = history[0], history[1]
    params = {"account": spending.pk}
    page1 = client.get(reverse("transactions"), params)
    page2 = client.get(reverse("transactions"), {**params, "page": 2})
    assert page1.context["page_obj"].paginator.count == 60  # transfer touches Spending too
    # Ledger for Spending, oldest first, by hand:
    #   open 1000.00; +1500.00 pay -> 2500.00; -300.00 transfer -> 2200.00;
    #   +25.00 refund -> 2225.00; -100.00 purchase -> 2125.00; +10.00 correction -> 2135.00;
    #   then 55 purchases of 2.00: after purchase k (0-based) = 2135.00 - 2.00 * (k + 1).
    first, last = figures(page1)[0], figures(page1)[-1]
    assert first == ("Expense", D("2.00"), -1, D("2025.00"), "Not cleared")  # k=54: 2135 - 110
    assert last == ("Expense", D("2.00"), -1, D("2123.00"), "Not cleared")  # k=5: 2135 - 12
    assert figures(page2) == [
        ("Expense", D("2.00"), -1, D("2125.00"), "Not cleared"),  # k=4: 2135 - 10
        ("Expense", D("2.00"), -1, D("2127.00"), "Not cleared"),  # k=3: 2135 - 8
        ("Expense", D("2.00"), -1, D("2129.00"), "Not cleared"),  # k=2: 2135 - 6
        ("Expense", D("2.00"), -1, D("2131.00"), "Not cleared"),  # k=1: 2135 - 4
        ("Expense", D("2.00"), -1, D("2133.00"), "Not cleared"),  # k=0: 2135 - 2
        ("Balance correction", D("10.00"), 1, D("2135.00"), "Not cleared"),
        ("Expense", D("100.00"), -1, D("2125.00"), "Cleared"),  # 2225.00 - 100.00
        ("Refund", D("25.00"), 1, D("2225.00"), "Not cleared"),  # 2200.00 + 25.00
        ("Transfer", D("300.00"), -1, D("2200.00"), "Cleared"),  # 2500.00 - 300.00, source cleared
        ("Income", D("1500.00"), 1, D("2500.00"), "Cleared"),  # 1000.00 + 1500.00 (Spending share)
    ]


def test_history_for_savings_account_and_type_filter(history):
    client, savings = history[0], history[3]
    rows = client.get(reverse("transactions"), {"account": savings.pk}).context["rows"]
    # Savings opens at 200.00 and only receives the 300.00 transfer: 200.00 + 300.00 = 500.00.
    assert [(r["type"], r["amount"], r["balance_after"], r["cleared"]) for r in rows] == [
        ("Transfer", D("300.00"), D("500.00"), "Not cleared")  # destination side not cleared
    ]
    only_refunds = client.get(reverse("transactions"), {"type": "refund"})
    assert only_refunds.context["page_obj"].paginator.count == 1
    assert only_refunds.context["rows"][0]["amount"] == D("25.00")
    keyword = client.get(reverse("transactions"), {"q": "Shop 0"})
    # "Shop 00".."Shop 09" match; the refund (merchant "Shop") does not contain "Shop 0".
    assert keyword.context["page_obj"].paginator.count == 10


def test_history_loads_full_rows_for_one_page_only(history):
    client = history[0]
    real = views.transaction_description
    calls = []

    def counting(obj, kind):
        calls.append(kind)
        return real(obj, kind)

    with patch.object(views, "transaction_description", counting):
        client.get(reverse("transactions"))
    # 60 rows match, one page is 50: only 50 rows may be built (before: all 60).
    assert len(calls) == 50


def test_history_query_count_does_not_grow_with_rows(history, django_assert_max_num_queries):
    client = history[0]
    # Measured 43 for 60 rows: sign-in and page chrome (about 25), one id query per kind (5),
    # one load per kind present on the page (about 6). Constant however many rows match.
    with django_assert_max_num_queries(48):
        assert client.get(reverse("transactions")).status_code == 200


@pytest.fixture
def bills_month(world):
    """August 2026: 12 bills, with history on four of them. Every figure is worked out below."""
    client, home, user = world
    account = Account.objects.create(household=home, name="Bills", type="bills")
    category = Category.objects.create(household=home, name="Utilities", type="spending")
    occurrences = []
    for n in range(12):
        bill = RecurringBill.objects.create(
            household=home,
            name=f"Bill {n:02d}",
            default_amount="100.00",
            due_day=n + 1,
            category=category,
            account=account,
            effective_start=date(2026, 1, 1),
        )
        occurrences.append(bill)

    def occurrence(bill, month, status, confirmed=None):
        return BillOccurrence.objects.create(
            bill=bill,
            due_date=date(2026, month, bill.due_day),
            expected_amount="100.00",
            confirmed_amount=confirmed,
            status=status,
        )

    def pay(item, amount, day):
        Expense.objects.create(
            household=home,
            created_by=user,
            date=date(2026, item.due_date.month, day),
            amount=amount,
            account=account,
            category=category,
            merchant=item.bill.name,
            bill_occurrence=item,
        )

    for bill in occurrences[:4]:  # history: June 150.00, July 100.00 + 70.00; August unconfirmed
        june = occurrence(bill, 6, "paid", "150.00")
        pay(june, "150.00", 20)
        july = occurrence(bill, 7, "paid", "170.00")
        pay(july, "100.00", 20)
        pay(july, "70.00", 21)
        occurrence(bill, 8, "unconfirmed")
    for bill in occurrences[4:6]:  # confirmed 200.00, 80.00 paid
        item = occurrence(bill, 8, "partial", "200.00")
        pay(item, "80.00", 25)
    for bill in occurrences[6:8]:  # confirmed 100.00, fully paid
        item = occurrence(bill, 8, "paid", "100.00")
        pay(item, "100.00", 25)
    occurrence(occurrences[8], 8, "skipped")
    return client  # bills 9..11 are created unconfirmed by the page itself


def test_bills_page_figures(bills_month):
    response = bills_month.get(reverse("bills"), {"month": "2026-08"})
    by_name = {
        o.bill.name: (o.amount_paid, o.remaining_amount, o.forecast and o.forecast["amount"])
        for o in response.context["occurrences"]
    }
    assert len(by_name) == 12
    # Bills 00-03: nothing paid in August, target = expected 100.00 -> remaining 100.00.
    # Forecast: June 150.00, July 100.00 + 70.00 = 170.00; average (150 + 170) / 2 = 160.00.
    for n in range(4):
        assert by_name[f"Bill {n:02d}"] == (D("0.00"), D("100.00"), D("160.00"))
    # Bills 04-05: confirmed 200.00, paid 80.00 -> remaining 120.00 (no forecast on confirmed).
    for n in (4, 5):
        assert by_name[f"Bill {n:02d}"] == (D("80.00"), D("120.00"), None)
    # Bills 06-07: confirmed 100.00, paid 100.00 -> remaining 0.00.
    for n in (6, 7):
        assert by_name[f"Bill {n:02d}"] == (D("100.00"), D("0.00"), None)
    assert by_name["Bill 08"] == (D("0.00"), D("0.00"), None)  # skipped
    # Bills 09-11: unconfirmed, no paid history -> forecast equals expected, so none is shown.
    for n in (9, 10, 11):
        assert by_name[f"Bill {n:02d}"] == (D("0.00"), D("100.00"), None)
    # Month total (skipped excluded): 4 x 100 + 2 x 200 + 2 x 100 + 3 x 100 = 1300.00.
    assert response.context["month_total"] == D("1300.00")
    # Remaining: 4 x 100 + 2 x 120 + 2 x 0 + 3 x 100 = 940.00.
    assert response.context["month_remaining"] == D("940.00")
    page = response.content.decode()
    assert page.count("Forecast $160.00") == 4


def test_bills_page_runs_a_bounded_number_of_queries(bills_month, django_assert_max_num_queries):
    # Measured 44: 12 schedule checks (one per bill), 3 creations for bills 09-11 (3 queries
    # each), page chrome, the month list and one query for recent payments. Before the change
    # every row also ran about seven separate Paid totals.
    with django_assert_max_num_queries(52):
        assert bills_month.get(reverse("bills"), {"month": "2026-08"}).status_code == 200
