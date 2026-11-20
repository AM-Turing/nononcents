"""A made-up "Demo Household" profile for trying the app, tests and guide screenshots.

Every name and amount is fictional. Dates are relative to today, so the demo always shows a
current pay period. It adds a separate profile to the database, so it only runs when you
confirm with --yes (or in BROWSER_TEST_MODE=true, the scratch browser-test database).
Running it again replaces the demo profile only; other profiles are never touched.
"""

import hashlib
import os
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from budget import models as m
from budget.pay_periods import create_period_budget, sync_pay_period_boundaries
from budget.services import (
    create_expense,
    create_income,
    create_refund,
    create_savings_goal,
    create_transfer,
    ensure_bill_occurrences,
    ensure_savings_goals,
    expected_deposit_date,
    pay_bill_occurrence,
)
from budget.statements import ParsedLine, ParsedSection, stage_statement

DEMO_NAME = "Demo Household"

SPENDING = [
    # name, essential, budget per pay period
    ("Groceries", True, "300.00"),
    ("Gas", True, "100.00"),
    ("Eating Out", False, "70.00"),
    ("Household Supplies", True, "40.00"),
    ("Kids Activities", True, "50.00"),
    ("Entertainment", False, "40.00"),
    ("Clothing", False, "30.00"),
    ("Pet Care", True, "20.00"),
]
BILL_CATEGORIES = ["Housing", "Utilities", "Communications", "Insurance", "Subscriptions", "Loans"]
BILLS = [
    # name, due day, amount, category
    ("Rent", 1, "1350.00", "Housing"),
    ("Electric", 12, "142.18", "Utilities"),
    ("Phone", 18, "85.00", "Communications"),
    ("Internet", 22, "65.00", "Communications"),
    ("Car Insurance", 25, "128.40", "Insurance"),
    ("Streaming Service", 8, "15.49", "Subscriptions"),
    ("Car Loan", 15, "289.00", "Loans"),
]
# (days ago, category, merchant, amount)
PURCHASES = [
    (2, "Groceries", "Fresh Market", "86.42"),
    (3, "Gas", "Corner Fuel", "41.10"),
    (4, "Eating Out", "Pizza Place", "32.75"),
    (6, "Household Supplies", "Big Box Store", "27.89"),
    (8, "Groceries", "Fresh Market", "112.30"),
    (9, "Kids Activities", "Swim Club", "40.00"),
    (11, "Gas", "Corner Fuel", "38.64"),
    (12, "Entertainment", "Movie Theater", "28.50"),
    (14, "Groceries", "Discount Grocer", "95.17"),
    (16, "Eating Out", "Taco Stand", "18.40"),
    (17, "Pet Care", "Pet Supply Co", "24.99"),
    (19, "Groceries", "Fresh Market", "78.55"),
    (21, "Gas", "Corner Fuel", "44.02"),
    (22, "Clothing", "Shoe Outlet", "54.00"),
    (24, "Eating Out", "Burger Barn", "26.15"),
    (26, "Groceries", "Discount Grocer", "103.66"),
    (28, "Household Supplies", "Big Box Store", "19.48"),
    (30, "Kids Activities", "Art Class", "35.00"),
    (32, "Gas", "Corner Fuel", "39.80"),
    (33, "Groceries", "Fresh Market", "91.24"),
    (36, "Eating Out", "Pizza Place", "29.60"),
    (38, "Entertainment", "Mini Golf", "22.00"),
    (40, "Groceries", "Discount Grocer", "87.93"),
    (42, "Gas", "Corner Fuel", "42.37"),
    (44, "Pet Care", "Pet Supply Co", "31.20"),
    (47, "Groceries", "Fresh Market", "99.08"),
    (49, "Eating Out", "Taco Stand", "21.35"),
    (52, "Gas", "Corner Fuel", "40.55"),
    (54, "Clothing", "Kids Clothing Co", "46.70"),
]
PAYCHECK = Decimal("1900.00")
SPLIT = {"Spending": Decimal("650.00"), "Bills": Decimal("1100.00"), "Savings": Decimal("150.00")}


class Command(BaseCommand):
    help = "Create (or replace) the fictional Demo Household profile. Pass --yes to confirm."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm adding the demo profile to this database.",
        )

    def handle(self, *args, **options):
        if not options["yes"] and os.environ.get("BROWSER_TEST_MODE") != "true":
            raise CommandError(
                "This adds a fictional demo profile to the current database. "
                "Run it again with --yes to confirm (or set BROWSER_TEST_MODE=true)."
            )
        with transaction.atomic():
            remove_demo_profile()
            home = build_demo_profile(timezone.localdate())
        self.stdout.write(self.style.SUCCESS(f"{home.name} ready"))


def remove_demo_profile():
    """Delete the demo profile, children first (ledger rows protect their accounts)."""
    for home in m.Household.objects.filter(name=DEMO_NAME):
        m.SavingsGoalEntry.objects.filter(goal__household=home).delete()
        m.SavingsGoal.objects.filter(household=home).delete()
        m.StatementLine.objects.filter(statement__household=home).delete()
        m.StatementImport.objects.filter(household=home).delete()
        m.Reconciliation.objects.filter(household=home).delete()
        for model in (m.Refund, m.Expense, m.Income, m.Transfer, m.BalanceAdjustment):
            model.objects.filter(household=home).delete()
        m.BillOccurrence.objects.filter(bill__household=home).delete()
        m.RecurringBill.objects.filter(household=home).delete()
        m.CategoryBudget.objects.filter(household=home).delete()
        m.DebtBalance.objects.filter(debt__household=home).delete()
        m.Debt.objects.filter(household=home).delete()
        m.HabitView.objects.filter(household=home).delete()
        m.PayPeriod.objects.filter(household=home).delete()
        m.AuditEvent.objects.filter(household=home).delete()
        home.delete()


def build_demo_profile(today):
    home = m.Household.objects.create(name=DEMO_NAME)
    accounts = {}
    for order, (name, kind, opening, suffix) in enumerate(
        (
            ("Spending", m.Account.Type.SPENDING, "150.00", "4821"),
            ("Bills", m.Account.Type.BILLS, "1000.00", "4839"),
            ("Savings", m.Account.Type.SAVINGS, "800.00", "5102"),
        )
    ):
        accounts[name] = m.Account.objects.create(
            household=home,
            name=name,
            type=kind,
            opening_balance=Decimal(opening),
            display_order=order,
            statement_suffix=suffix,
        )
    categories = {}
    for order, (name, essential, _) in enumerate(SPENDING):
        categories[name] = m.Category.objects.create(
            household=home,
            name=name,
            type=m.Category.Type.SPENDING,
            display_order=order,
            is_essential=essential,
        )
    for order, name in enumerate(BILL_CATEGORIES):
        categories[name] = m.Category.objects.create(
            household=home, name=name, type=m.Category.Type.BILL, display_order=order
        )
    m.Category.objects.create(household=home, name="Paycheck", type=m.Category.Type.INCOME)

    # Bi-weekly pay on Fridays: the first period began about eight weeks ago.
    m.PaySchedule.objects.create(
        household=home, frequency=m.PaySchedule.Frequency.BIWEEKLY, nominal_days=[1]
    )
    last_friday = today - timedelta(days=(today.weekday() - 4) % 7)
    first_payday = last_friday - timedelta(days=14 * 4)
    first = m.PayPeriod.objects.create(
        household=home,
        nominal_payday=first_payday,
        expected_deposit_date=expected_deposit_date(first_payday),
        start_date=expected_deposit_date(first_payday),
        end_date=expected_deposit_date(first_payday + timedelta(days=14)) - timedelta(days=1),
    )
    budget = create_period_budget(first)
    budget.expected_paycheck = PAYCHECK
    budget.spending_target = Decimal("650.00")
    budget.save(update_fields=["expected_paycheck", "spending_target"])
    from budget.views import generate_future_pay_periods  # the same code the Pay periods page runs

    generate_future_pay_periods(home, today + timedelta(days=120))

    for name, _, amount in SPENDING:
        m.CategoryBudget.objects.create(
            household=home,
            category=categories[name],
            amount=Decimal(amount),
            effective_from=first.start_date,
        )

    for name, day, amount, category in BILLS:
        m.RecurringBill.objects.create(
            household=home,
            name=name,
            due_day=day,
            default_amount=Decimal(amount),
            category=categories[category],
            account=accounts["Bills"],
            effective_start=first.start_date.replace(day=1),
        )
    ensure_bill_occurrences(home, today + timedelta(days=120), start=first.start_date)

    # Savings goals before any money moves into Savings.
    emergency, _ = ensure_savings_goals(home)
    create_savings_goal(
        household=home,
        actor=None,
        name="Summer Trip",
        target_amount="1500.00",
        priority=2,
        initial_amount="300.00",
        source_goal=emergency,
    )

    payday = first_payday
    while expected_deposit_date(payday) <= today:
        create_income(
            household=home,
            actor=None,
            income_date=expected_deposit_date(payday),
            source="Main Street Employer",
            amount=PAYCHECK,
            allocations=[(accounts[name], value) for name, value in SPLIT.items()],
            is_paycheck=True,
            savings_goal=emergency,
        )
        payday += timedelta(days=14)
    sync_pay_period_boundaries(home)

    for occurrence in m.BillOccurrence.objects.filter(
        bill__household=home, due_date__lt=today - timedelta(days=2)
    ).order_by("due_date"):
        pay_bill_occurrence(occurrence, None, occurrence.due_date)

    expenses = {}
    for days_ago, category, merchant, amount in PURCHASES:
        expenses[(days_ago, merchant)] = create_expense(
            household=home,
            actor=None,
            entry_date=today - timedelta(days=days_ago),
            account=accounts["Spending"],
            category=categories[category],
            amount=Decimal(amount),
            merchant=merchant,
        )
    create_refund(
        household=home,
        actor=None,
        entry_date=today - timedelta(days=20),
        account=accounts["Spending"],
        category=categories["Clothing"],
        amount=Decimal("18.00"),
        merchant="Shoe Outlet",
        original_expense=expenses[(22, "Shoe Outlet")],
    )
    create_transfer(
        household=home,
        actor=None,
        entry_date=today - timedelta(days=10),
        source=accounts["Spending"],
        destination=accounts["Savings"],
        amount=Decimal("75.00"),
        notes="Extra savings",
        savings_destination_goal=emergency,
    )

    car = m.Debt.objects.create(
        household=home,
        name="Car Loan",
        kind=m.Debt.Kind.LOAN,
        apr=Decimal("6.250"),
        minimum_payment=Decimal("289.00"),
    )
    card = m.Debt.objects.create(
        household=home,
        name="Store Credit Card",
        kind=m.Debt.Kind.CREDIT_CARD,
        apr=Decimal("24.990"),
        minimum_payment=Decimal("35.00"),
    )
    m.RecurringBill.objects.filter(household=home, name="Car Loan").update(debt=car)
    m.DebtBalance.objects.create(debt=car, as_of=today - timedelta(days=40), balance="9120.44")
    m.DebtBalance.objects.create(debt=car, as_of=today - timedelta(days=10), balance="8879.10")
    m.DebtBalance.objects.create(debt=card, as_of=today - timedelta(days=12), balance="1240.65")

    stage_demo_statement(home, accounts["Spending"], today)
    return home


def stage_demo_statement(home, account, today):
    """A Spending statement for the last four weeks, staged for review like an upload.

    Most lines match entries already in the budget; two (a coffee shop and a bank fee) do not,
    so the review page shows both cases.
    """
    start = today - timedelta(days=28)
    end = today - timedelta(days=1)
    lines = [
        ParsedLine(today - timedelta(days=d), -Decimal(a), f"POS PURCHASE {merchant.upper()}")
        for d, _, merchant, a in PURCHASES
        if 1 <= d <= 28
    ]
    lines += [
        ParsedLine(today - timedelta(days=5), -Decimal("6.75"), "POS PURCHASE BEAN COFFEE"),
        ParsedLine(today - timedelta(days=15), -Decimal("3.00"), "MONTHLY SERVICE FEE"),
    ]
    lines.sort(key=lambda line: line.date)
    section = ParsedSection(
        account_label="Spending",
        period_start=start,
        period_end=end,
        lines=lines,
        account_suffix=account.statement_suffix,
    )
    digest = hashlib.sha256(f"demo-statement-{start}".encode()).hexdigest()
    stage_statement(
        household=home, actor=None, account=account, section=section, file_sha256=digest
    )
