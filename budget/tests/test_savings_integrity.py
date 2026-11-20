from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.urls import reverse

from budget.models import (
    Account,
    AuditEvent,
    Category,
    Expense,
    HouseholdMembership,
    Income,
    SavingsGoal,
    SavingsGoalEntry,
)
from budget.services import (
    archive_savings_goal,
    complete_savings_goal,
    create_expense,
    create_income,
    create_savings_goal,
    ensure_savings_goals,
    reconcile_savings,
    savings_ledger_balance,
)
from budget.tests.test_savings_goals import savings_setup  # noqa: F401

TODAY = date.today()


def goal_total(home):
    return SavingsGoalEntry.objects.filter(goal__household=home).aggregate(t=Sum("amount"))[
        "t"
    ] or Decimal("0.00")


@pytest.fixture
def logged_in(client, savings_setup):  # noqa: F811
    home, user, savings, spending, emergency = savings_setup
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    return client, home, user, savings, spending, emergency


def income_with_savings(home, user, savings, spending, goal, amount="300.00", income_date=TODAY):
    return create_income(
        household=home,
        actor=user,
        income_date=income_date,
        source="Paycheck",
        amount=Decimal(amount) + Decimal("100.00"),
        allocations=[(savings, amount), (spending, "100.00")],
        savings_goal=goal,
    )


def edit_income(client, income, savings, spending, goal, *, notes="", savings_amount="300.00"):
    return client.post(
        reverse("transaction_edit", args=["income", income.pk]),
        {
            "date": income.date.isoformat(),
            "description": income.source,
            "amount": str(Decimal(savings_amount) + Decimal("100.00")),
            "notes": notes,
            f"allocation_{savings.pk}": savings_amount,
            f"allocation_{spending.pk}": "100.00",
            "savings_destination_goal": goal.pk,
        },
    )


def delete_income(client, income):
    return client.post(
        reverse("transaction_delete", args=["income", income.pk]),
        {"confirmation": f"Income: {income.source}"},
    )


def spend_from_goal(home, user, savings, goal, amount, entry_date=TODAY, fulfills=False):
    category = Category.objects.filter(household=home).first()
    return create_expense(
        household=home,
        actor=user,
        entry_date=entry_date,
        account=savings,
        category=category,
        amount=amount,
        merchant="Spend",
        savings_allocations=[(goal, amount, fulfills)],
    )


@pytest.mark.django_db
def test_renamed_emergency_fund_is_not_recreated(logged_in):
    client, home, _, savings, _, emergency = logged_in
    emergency.name = "Rainy day"
    emergency.save()
    before = goal_total(home)

    response = client.get(reverse("expense_add"))

    assert response.status_code == 200
    assert not SavingsGoal.objects.filter(name="Emergency Fund").exists()
    assert goal_total(home) == before == savings_ledger_balance(home)


@pytest.mark.django_db
def test_deleting_empty_emergency_fund_does_not_double_count(logged_in):
    client, home, user, savings, _, emergency = logged_in
    trip = create_savings_goal(
        household=home,
        actor=user,
        name="Trip",
        target_amount="1000.00",
        priority=2,
        initial_amount="1000.00",
        source_goal=emergency,
    )
    SavingsGoalEntry.objects.all().delete()
    emergency.delete()
    SavingsGoalEntry.objects.create(
        goal=trip, amount=Decimal("1000.00"), date=TODAY, kind=SavingsGoalEntry.Kind.INITIAL
    )

    ensure_savings_goals(home)
    assert client.get(reverse("expense_add")).status_code == 200

    assert not SavingsGoal.objects.filter(name="Emergency Fund").exists()
    assert goal_total(home) == Decimal("1000.00")
    assert trip.current_amount == Decimal("1000.00")


@pytest.mark.django_db
def test_archived_and_new_same_named_goal_keep_entry_pages_working(logged_in):
    client, home, user, savings, _, emergency = logged_in
    complete_savings_goal(emergency, user)
    archive_savings_goal(emergency, user)
    SavingsGoal.objects.create(
        household=home, name="Emergency Fund", target_amount=Decimal("500.00"), priority=1
    )

    for name in ["expense_add", "income_add", "transfer_add", "savings"]:
        assert client.get(reverse(name)).status_code == 200


@pytest.mark.django_db
def test_deleting_income_whose_money_was_spent_is_rejected(logged_in):
    client, home, user, savings, spending, emergency = logged_in
    trip = create_savings_goal(
        household=home, actor=user, name="Trip", target_amount="300.00", priority=2
    )
    income = income_with_savings(home, user, savings, spending, trip)
    spend_from_goal(home, user, savings, trip, "250.00")

    response = delete_income(client, income)

    assert response.status_code == 200
    assert b"short by $250.00" in response.content
    assert Income.objects.filter(pk=income.pk).exists()
    assert trip.current_amount == Decimal("50.00")
    assert goal_total(home) == savings_ledger_balance(home)


@pytest.mark.django_db
def test_shrinking_income_whose_money_was_spent_is_rejected(logged_in):
    client, home, user, savings, spending, emergency = logged_in
    trip = create_savings_goal(
        household=home, actor=user, name="Trip", target_amount="300.00", priority=2
    )
    income = income_with_savings(home, user, savings, spending, trip)
    spend_from_goal(home, user, savings, trip, "250.00")

    response = edit_income(client, income, savings, spending, trip, savings_amount="100.00")

    assert response.status_code == 200
    assert b"short by $150.00" in response.content
    income.refresh_from_db()
    assert income.amount == Decimal("400.00")
    assert trip.current_amount == Decimal("50.00")
    assert goal_total(home) == savings_ledger_balance(home)


@pytest.mark.django_db
def test_unspent_income_can_be_deleted(logged_in):
    client, home, user, savings, spending, _ = logged_in
    trip = create_savings_goal(
        household=home, actor=user, name="Trip", target_amount="300.00", priority=2
    )
    income = income_with_savings(home, user, savings, spending, trip)

    assert delete_income(client, income).status_code == 302
    assert goal_total(home) == savings_ledger_balance(home)


@pytest.mark.django_db
def test_editing_notes_of_income_tied_to_completed_goal_keeps_entries(logged_in):
    client, home, user, savings, spending, _ = logged_in
    trip = create_savings_goal(
        household=home, actor=user, name="Trip", target_amount="300.00", priority=2
    )
    income = income_with_savings(home, user, savings, spending, trip)
    complete_savings_goal(trip, user)
    entry_before = SavingsGoalEntry.objects.get(income_allocation__income=income)

    response = edit_income(client, income, savings, spending, trip, notes="typo fixed")

    assert response.status_code == 302
    income.refresh_from_db()
    assert income.notes == "typo fixed"
    entry_after = SavingsGoalEntry.objects.get(income_allocation__income=income)
    assert (entry_after.pk, entry_after.goal_id, entry_after.amount) == (
        entry_before.pk,
        trip.pk,
        Decimal("300.00"),
    )
    assert goal_total(home) == savings_ledger_balance(home)


def fulfilling_setup(home, user, savings):
    goal = SavingsGoal.objects.get(household=home, name="Emergency Fund")
    goal.target_amount = Decimal("1000.00")
    goal.save()
    complete_savings_goal(goal, user)
    expense = spend_from_goal(home, user, savings, goal, "1000.00", fulfills=True)
    goal.refresh_from_db()
    assert goal.status == SavingsGoal.Status.ARCHIVED
    return goal, expense


def edit_expense_post(expense, savings, goal, **overrides):
    data = {
        "date": expense.date.isoformat(),
        "description": "Spend",
        "amount": "1000.00",
        "notes": "",
        "category": Category.objects.first().pk,
        "savings-TOTAL_FORMS": "1",
        "savings-INITIAL_FORMS": "1",
        "savings-MIN_NUM_FORMS": "0",
        "savings-MAX_NUM_FORMS": "20",
        "savings-0-goal": goal.pk,
        "savings-0-amount": "1000.00",
        "savings-0-fulfills_goal": "on",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_editing_text_of_goal_fulfilling_expense_succeeds(logged_in):
    client, home, user, savings, _, _ = logged_in
    goal, expense = fulfilling_setup(home, user, savings)

    response = client.post(
        reverse("transaction_edit", args=["expense", expense.pk]),
        edit_expense_post(expense, savings, goal, description="Spend fixed", notes="typo"),
    )

    assert response.status_code == 302, response.context["form"].errors
    expense.refresh_from_db()
    assert expense.merchant == "Spend fixed"
    goal.refresh_from_db()
    assert goal.status == SavingsGoal.Status.ARCHIVED
    assert goal.entries.filter(fulfills_goal=True).count() == 1
    assert goal_total(home) == savings_ledger_balance(home)


@pytest.mark.django_db
def test_deleting_goal_fulfilling_expense_restores_goal(logged_in):
    client, home, user, savings, _, _ = logged_in
    goal, expense = fulfilling_setup(home, user, savings)

    response = client.post(
        reverse("transaction_delete", args=["expense", expense.pk]),
        {"confirmation": "Expense: Spend"},
    )

    assert response.status_code == 302
    goal.refresh_from_db()
    assert goal.status == SavingsGoal.Status.ACTIVE
    assert goal.current_amount == Decimal("1000.00")
    assert any(
        event.changes.get("status") == "active"
        for event in AuditEvent.objects.filter(record_id=str(goal.pk))
    )


@pytest.mark.django_db
def test_savings_get_never_writes_and_post_assigns_drift(logged_in):
    client, home, user, savings, _, _ = logged_in
    Account.objects.filter(pk=savings.pk).update(opening_balance=Decimal("1250.00"))
    entries_before = SavingsGoalEntry.objects.count()
    audits_before = AuditEvent.objects.count()

    response = client.get(reverse("savings"))

    assert response.status_code == 200
    assert b"Assign $250.00 to Unallocated Savings" in response.content
    assert SavingsGoalEntry.objects.count() == entries_before
    assert AuditEvent.objects.count() == audits_before

    assert client.post(reverse("savings_reconcile")).status_code == 302
    assert goal_total(home) == savings_ledger_balance(home)
    unallocated = SavingsGoal.objects.get(household=home, is_system=True)
    assert unallocated.current_amount == Decimal("250.00")
    assert AuditEvent.objects.count() == audits_before + 1
    assert reconcile_savings(home, user) == Decimal("0.00")


@pytest.mark.django_db
def test_reconcile_post_respects_locked_period(logged_in):
    client, home, user, savings, _, _ = logged_in
    from budget.models import PayPeriod

    PayPeriod.objects.create(
        household=home,
        start_date=TODAY - timedelta(days=3),
        end_date=TODAY + timedelta(days=3),
        nominal_payday=TODAY,
        expected_deposit_date=TODAY,
        is_locked=True,
    )
    Account.objects.filter(pk=savings.pk).update(opening_balance=Decimal("1250.00"))
    with pytest.raises(ValidationError, match="locked"):
        reconcile_savings(home, user)
    assert goal_total(home) == Decimal("1000.00")


@pytest.mark.django_db
def test_drift_compares_all_savings_type_accounts(logged_in):
    client, home, user, savings, _, _ = logged_in
    Account.objects.create(
        household=home,
        name="Vacation jar",
        type=Account.Type.SAVINGS,
        opening_balance=Decimal("75.00"),
    )
    assert client.get(reverse("savings")).context["savings_drift"] == Decimal("75.00")


@pytest.mark.django_db
def test_backdated_outflow_before_money_arrived_is_rejected(logged_in):
    client, home, user, savings, spending, _ = logged_in
    trip = create_savings_goal(
        household=home, actor=user, name="Trip", target_amount="300.00", priority=2
    )
    income_with_savings(home, user, savings, spending, trip, income_date=TODAY)

    with pytest.raises(ValidationError, match="as of"):
        spend_from_goal(home, user, savings, trip, "100.00", entry_date=TODAY - timedelta(days=30))
    assert not Expense.objects.exists()
    spend_from_goal(home, user, savings, trip, "100.00", entry_date=TODAY)


@pytest.mark.django_db
def test_invariant_holds_after_create_edit_delete_sequence(logged_in):
    client, home, user, savings, spending, emergency = logged_in
    trip = create_savings_goal(
        household=home, actor=user, name="Trip", target_amount="500.00", priority=2
    )
    income = income_with_savings(home, user, savings, spending, trip)
    assert goal_total(home) == savings_ledger_balance(home)
    expense = spend_from_goal(home, user, savings, emergency, "200.00")
    assert goal_total(home) == savings_ledger_balance(home)

    assert (
        edit_income(client, income, savings, spending, trip, savings_amount="350.00").status_code
        == 302
    )
    assert goal_total(home) == savings_ledger_balance(home)
    assert (
        client.post(
            reverse("transaction_edit", args=["expense", expense.pk]),
            edit_expense_post(
                expense,
                savings,
                emergency,
                amount="150.00",
                **{"savings-0-amount": "150.00", "savings-0-fulfills_goal": ""},
            ),
        ).status_code
        == 302
    )
    assert goal_total(home) == savings_ledger_balance(home)
    assert delete_income(client, income).status_code == 302
    assert (
        client.post(
            reverse("transaction_delete", args=["expense", expense.pk]),
            {"confirmation": "Expense: Spend"},
        ).status_code
        == 302
    )
    assert goal_total(home) == savings_ledger_balance(home) == Decimal("1000.00")
