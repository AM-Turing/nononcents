from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse

from budget.models import (
    Account,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    PayPeriod,
    SavingsGoal,
    SavingsGoalEntry,
    Transfer,
)
from budget.services import (
    account_balance,
    archive_savings_goal,
    complete_savings_goal,
    create_expense,
    create_savings_goal,
    create_transfer,
    ensure_savings_goals,
    reallocate_savings,
    restore_savings_goal,
)


@pytest.fixture
def savings_setup(db):
    home = Household.objects.create(name="Goals profile")
    user = get_user_model().objects.create_user("goals-user")
    savings = Account.objects.create(
        household=home,
        name="Savings",
        type=Account.Type.SAVINGS,
        opening_balance=Decimal("1000.00"),
    )
    spending = Account.objects.create(household=home, name="Spending", type=Account.Type.SPENDING)
    Category.objects.create(household=home, name="General", type=Category.Type.SPENDING)
    emergency, _ = ensure_savings_goals(home)
    return home, user, savings, spending, emergency


@pytest.mark.django_db
def test_goal_allocations_partition_savings_without_changing_ledger(savings_setup):
    home, user, savings, spending, emergency = savings_setup
    trip = create_savings_goal(
        household=home,
        actor=user,
        name="Trip",
        target_amount="400.00",
        priority=2,
        initial_amount="400.00",
        source_goal=emergency,
    )

    assert account_balance(savings) == Decimal("1000.00")
    assert emergency.current_amount == Decimal("600.00")
    assert trip.current_amount == Decimal("400.00")

    create_transfer(
        household=home,
        actor=user,
        entry_date=date.today(),
        source=savings,
        destination=spending,
        amount="700.00",
        savings_source_allocations=[
            (emergency, "500.00", False),
            (trip, "200.00", False),
        ],
    )

    assert account_balance(savings) == Decimal("300.00")
    assert emergency.current_amount == Decimal("100.00")
    assert trip.current_amount == Decimal("200.00")


@pytest.mark.django_db
def test_savings_withdrawal_never_allows_goal_to_go_negative(savings_setup):
    home, user, savings, spending, emergency = savings_setup

    with pytest.raises(ValidationError, match="does not have enough"):
        create_transfer(
            household=home,
            actor=user,
            entry_date=date(2026, 9, 1),
            source=savings,
            destination=spending,
            amount="1001.00",
            savings_source_allocations=[(emergency, "1001.00", False)],
        )

    assert not Transfer.objects.exists()
    assert emergency.current_amount == Decimal("1000.00")


@pytest.mark.django_db
def test_completed_archived_goal_keeps_its_money_and_name_can_be_reused(savings_setup):
    home, user, savings, _, emergency = savings_setup

    complete_savings_goal(emergency, user)
    archive_savings_goal(emergency, user)

    emergency.refresh_from_db()
    assert emergency.status == SavingsGoal.Status.ARCHIVED
    assert emergency.current_amount == Decimal("1000.00")
    assert account_balance(savings) == Decimal("1000.00")
    restore_savings_goal(emergency, user)
    emergency.refresh_from_db()
    assert emergency.status == SavingsGoal.Status.ACTIVE
    assert emergency.current_amount == Decimal("1000.00")
    complete_savings_goal(emergency, user)
    archive_savings_goal(emergency, user)
    replacement = create_savings_goal(
        household=home,
        actor=user,
        name="Emergency Fund",
        target_amount="2000.00",
        priority=1,
    )
    assert replacement.name == emergency.name
    assert replacement.status == SavingsGoal.Status.ACTIVE


@pytest.mark.django_db
def test_active_goal_cannot_be_archived_before_it_is_complete(savings_setup):
    _, user, _, _, emergency = savings_setup

    with pytest.raises(ValidationError, match="Complete this goal"):
        archive_savings_goal(emergency, user)

    emergency.refresh_from_db()
    assert emergency.status == SavingsGoal.Status.ACTIVE


@pytest.mark.django_db
def test_spending_full_completed_allocation_fulfills_and_archives_goal(savings_setup):
    home, user, savings, _, emergency = savings_setup
    category = Category.objects.get(household=home, name="General")
    complete_savings_goal(emergency, user)

    create_expense(
        household=home,
        actor=user,
        entry_date=date(2026, 9, 1),
        account=savings,
        category=category,
        amount="1000.00",
        merchant="Goal purchase",
        savings_allocations=[(emergency, "1000.00", True)],
    )

    emergency.refresh_from_db()
    assert emergency.status == SavingsGoal.Status.ARCHIVED
    assert emergency.current_amount == Decimal("0.00")
    assert account_balance(savings) == Decimal("0.00")
    assert Expense.objects.filter(merchant="Goal purchase").exists()


@pytest.mark.django_db
def test_fulfilling_goal_requires_spending_its_full_allocation(savings_setup):
    home, user, savings, _, emergency = savings_setup
    category = Category.objects.get(household=home, name="General")
    complete_savings_goal(emergency, user)

    with pytest.raises(ValidationError, match=r"full \$1,000.00"):
        create_expense(
            household=home,
            actor=user,
            entry_date=date(2026, 9, 1),
            account=savings,
            category=category,
            amount="500.00",
            merchant="Partial purchase",
            savings_allocations=[(emergency, "500.00", True)],
        )

    emergency.refresh_from_db()
    assert emergency.status == SavingsGoal.Status.COMPLETED
    assert emergency.current_amount == Decimal("1000.00")
    assert not Expense.objects.filter(merchant="Partial purchase").exists()


@pytest.mark.django_db
def test_completed_goal_offers_prefilled_spending_or_long_term_archive(client, savings_setup):
    home, user, savings, _, emergency = savings_setup
    HouseholdMembership.objects.create(household=home, user=user)
    complete_savings_goal(emergency, user)
    client.force_login(user)

    savings_page = client.get(reverse("savings"))

    assert b"Spend fulfilled goal" in savings_page.content
    assert b"Archive and keep in Savings" in savings_page.content
    assert b"Active goals" in savings_page.content
    assert b'<button type="submit">Archive</button>' not in savings_page.content

    expense_page = client.get(reverse("expense_add"), {"savings_goal": emergency.pk})

    assert expense_page.context["form"]["account"].value() == savings.pk
    assert expense_page.context["form"]["amount"].value() == Decimal("1000.00")
    first_allocation = expense_page.context["savings_formset"].forms[0]
    assert first_allocation["goal"].value() == emergency.pk
    assert first_allocation["fulfills_goal"].value() is True


@pytest.mark.django_db
def test_reallocation_never_marks_a_goal_fulfilled(savings_setup):
    home, user, _, _, emergency = savings_setup
    next_goal = create_savings_goal(
        household=home,
        actor=user,
        name="Next priority",
        target_amount="500.00",
        priority=2,
    )

    reallocate_savings(
        household=home,
        actor=user,
        entry_date=date(2026, 9, 1),
        allocations=[(emergency, "100.00", True)],
        destination_goal=next_goal,
        amount="100.00",
    )

    emergency.refresh_from_db()
    assert emergency.status == SavingsGoal.Status.ACTIVE
    assert not SavingsGoalEntry.objects.filter(
        goal=emergency, kind=SavingsGoalEntry.Kind.REALLOCATION_OUT, fulfills_goal=True
    ).exists()


@pytest.mark.django_db
def test_locked_pay_period_rejects_goal_reallocation(savings_setup):
    home, user, _, _, emergency = savings_setup
    next_goal = create_savings_goal(
        household=home,
        actor=user,
        name="Next priority",
        target_amount="500.00",
        priority=2,
    )
    PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 9, 1),
        expected_deposit_date=date(2026, 8, 31),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 14),
        is_locked=True,
    )

    with pytest.raises(ValidationError, match="locked"):
        reallocate_savings(
            household=home,
            actor=user,
            entry_date=date(2026, 9, 5),
            allocations=[(emergency, "100.00", False)],
            destination_goal=next_goal,
            amount="100.00",
        )

    assert emergency.current_amount == Decimal("1000.00")
    assert next_goal.current_amount == Decimal("0.00")
