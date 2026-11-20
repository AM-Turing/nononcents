import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    Account,
    AuditEvent,
    BillOccurrence,
    Category,
    Expense,
    ExpenseSplit,
    FormSubmission,
    Income,
    IncomeAllocation,
    PayPeriod,
    RecurringBill,
    Refund,
    SavingsGoal,
    SavingsGoalEntry,
    Transfer,
    paid_total,
)

CENT = Decimal("0.01")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def expected_deposit_date(nominal: date) -> date:
    if nominal.weekday() < 5:
        return nominal - timedelta(days=1)
    return nominal - timedelta(days=nominal.weekday() - 3)


def account_balance(account: Account, through: date | None = None) -> Decimal:
    dated = {"date__lte": through} if through else {}
    allocated = (
        IncomeAllocation.objects.filter(account=account, income__date__lte=through).aggregate(
            v=Sum("amount")
        )["v"]
        if through
        else IncomeAllocation.objects.filter(account=account).aggregate(v=Sum("amount"))["v"]
    )
    expenses = account.expenses.filter(**dated).aggregate(v=Sum("amount"))["v"]
    refunds = account.refunds.filter(**dated).aggregate(v=Sum("amount"))["v"]
    incoming = account.incoming_transfers.filter(**dated).aggregate(v=Sum("amount"))["v"]
    outgoing = account.outgoing_transfers.filter(**dated).aggregate(v=Sum("amount"))["v"]
    increases = account.adjustments.filter(is_increase=True, **dated).aggregate(v=Sum("amount"))[
        "v"
    ]
    decreases = account.adjustments.filter(is_increase=False, **dated).aggregate(v=Sum("amount"))[
        "v"
    ]
    return money(
        account.opening_balance
        + (allocated or 0)
        + (refunds or 0)
        + (incoming or 0)
        + (increases or 0)
        - (expenses or 0)
        - (outgoing or 0)
        - (decreases or 0)
    )


def ensure_open(household, entry_date):
    period = PayPeriod.objects.filter(
        household=household, start_date__lte=entry_date, end_date__gte=entry_date
    ).first()
    if period and period.is_locked:
        raise ValidationError("This pay period is locked. An administrator must reopen it first.")


def savings_accounts(household):
    """All active Savings-type accounts; the type, not the name, defines them."""
    return list(
        Account.objects.filter(
            household=household, type=Account.Type.SAVINGS, archived_at=None
        ).order_by("display_order", "name", "pk")
    )


def savings_account(household):
    accounts = savings_accounts(household)
    for account in accounts:
        if account.name == "Savings":
            return account
    return accounts[0] if accounts else None


def savings_ledger_balance(household):
    return money(sum((account_balance(a) for a in savings_accounts(household)), Decimal("0.00")))


def savings_drift(household):
    """Ledger balance of Savings accounts minus the money assigned to goals."""
    if not savings_accounts(household):
        return Decimal("0.00")
    allocated = SavingsGoalEntry.objects.filter(goal__household=household).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0.00")
    return money(savings_ledger_balance(household) - allocated)


def _earliest_ledger_date(account):
    """Date from which the existing Savings balance counts as available to goals."""
    if account.opening_balance:
        return date(1900, 1, 1)  # opening balances predate every ledger entry
    dates = [timezone.localdate()]
    for qs, field in (
        (account.incomeallocation_set.all(), "income__date"),
        (account.expenses.all(), "date"),
        (account.refunds.all(), "date"),
        (account.outgoing_transfers.all(), "date"),
        (account.incoming_transfers.all(), "date"),
    ):
        earliest = qs.order_by(field).values_list(field, flat=True).first()
        if earliest:
            dates.append(earliest)
    return min(dates)


def _system_goal(household):
    goal = SavingsGoal.objects.filter(household=household, is_system=True).first()
    if goal:
        return goal
    name = "Unallocated Savings"
    if (
        SavingsGoal.objects.filter(household=household, name=name)
        .exclude(status=SavingsGoal.Status.ARCHIVED)
        .exists()
    ):
        name = "Unallocated Savings (system)"
    return SavingsGoal.objects.create(
        household=household, name=name, target_amount=Decimal("0.00"), is_system=True
    )


def ensure_savings_goals(household):
    """Return (default inflow goal, system goal).

    Goal tracking is bootstrapped only for a household with no goals at all; after
    that goals are never looked up or created by name.
    """
    account = savings_account(household)
    if not account:
        return None, None
    if not SavingsGoal.objects.filter(household=household).exists():
        emergency = SavingsGoal.objects.create(
            household=household,
            name="Emergency Fund",
            target_amount=Decimal("1000.00"),
            priority=1,
        )
        unallocated = _system_goal(household)
        balance = savings_ledger_balance(household)
        if balance:
            SavingsGoalEntry.objects.create(
                goal=emergency if balance > 0 else unallocated,
                amount=balance,
                date=_earliest_ledger_date(account),
                kind=SavingsGoalEntry.Kind.INITIAL,
                note="Savings balance when goal tracking began",
            )
        return emergency, unallocated
    default = (
        SavingsGoal.objects.filter(
            household=household, status=SavingsGoal.Status.ACTIVE, is_system=False
        )
        .order_by("priority", "created_at", "pk")
        .first()
    )
    return default, _system_goal(household)


def _goal_amount(goal):
    return goal.entries.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def _validate_incoming_goal(household, goal):
    if not goal or goal.household_id != household.pk or goal.is_system:
        raise ValidationError("Choose an active savings goal for money entering Savings.")
    if goal.status != SavingsGoal.Status.ACTIVE:
        raise ValidationError("Completed or archived goals cannot receive more money.")


def record_savings_inflow(
    *,
    household,
    actor,
    goal,
    amount,
    entry_date,
    kind,
    income_allocation=None,
    refund=None,
    transfer=None,
):
    _validate_incoming_goal(household, goal)
    return SavingsGoalEntry.objects.create(
        goal=goal,
        amount=money(amount),
        date=entry_date,
        kind=kind,
        created_by=actor,
        income_allocation=income_allocation,
        refund=refund,
        transfer=transfer,
    )


def record_savings_outflow(
    *, household, actor, allocations, amount, entry_date, kind, expense=None, transfer=None
):
    normalized = [
        (goal, money(value), bool(fulfills))
        for goal, value, fulfills in allocations or []
        if money(value) > 0
    ]
    total = money(amount)
    if not normalized or sum((value for _, value, _ in normalized), Decimal("0.00")) != total:
        raise ValidationError("Savings goal amounts must add up exactly to the transaction total.")
    if len({goal.pk for goal, _, _ in normalized}) != len(normalized):
        raise ValidationError("Use each savings goal only once.")
    locked = {
        goal.pk: goal
        for goal in SavingsGoal.objects.select_for_update().filter(
            household=household, pk__in=[goal.pk for goal, _, _ in normalized]
        )
    }
    if len(locked) != len(normalized):
        raise ValidationError("Choose savings goals from this budget profile.")
    for supplied, value, fulfills in normalized:
        goal = locked[supplied.pk]
        if goal.status == SavingsGoal.Status.ARCHIVED:
            raise ValidationError(f"Restore {goal.name} before using its allocated funds.")
        current_amount = _goal_amount(goal)
        if current_amount < value:
            raise ValidationError(f"{goal.name} does not have enough allocated funds.")
        as_of_amount = goal.entries.filter(date__lte=entry_date).aggregate(total=Sum("amount"))[
            "total"
        ] or Decimal("0.00")
        if as_of_amount < value:
            raise ValidationError(
                f"{goal.name} had only ${as_of_amount:,.2f} allocated as of "
                f"{entry_date:%b %d, %Y}, so it cannot cover this amount on that date."
            )
        if fulfills and not goal.is_system:
            if goal.status not in {
                SavingsGoal.Status.ACTIVE,
                SavingsGoal.Status.COMPLETED,
            }:
                raise ValidationError(f"{goal.name} is not available to fulfill.")
            if current_amount < goal.target_amount:
                raise ValidationError(
                    f"{goal.name} must meet its target before this can fulfill the goal."
                )
            if value != current_amount:
                raise ValidationError(
                    f"Use the full ${current_amount:,.2f} from {goal.name} "
                    "to fulfill and archive it."
                )
        SavingsGoalEntry.objects.create(
            goal=goal,
            amount=-value,
            date=entry_date,
            kind=kind,
            fulfills_goal=fulfills,
            created_by=actor,
            expense=expense,
            transfer=transfer,
        )
        if fulfills and not goal.is_system:
            now = timezone.now()
            goal.status = SavingsGoal.Status.ARCHIVED
            goal.completed_at = goal.completed_at or now
            goal.archived_at = now
            goal.priority = None
            goal.save(update_fields=["status", "completed_at", "archived_at", "priority"])
            normalize_goal_priorities(household)
            audit(
                household,
                actor,
                goal,
                "update",
                {"status": "archived", "reason": "fulfilled by transaction"},
            )


def normalize_goal_priorities(household):
    goals = list(
        SavingsGoal.objects.filter(
            household=household, status=SavingsGoal.Status.ACTIVE, is_system=False
        ).order_by("priority", "created_at", "pk")
    )
    # Move through temporary values to avoid the conditional uniqueness constraint.
    for index, goal in enumerate(goals, 1):
        SavingsGoal.objects.filter(pk=goal.pk).update(priority=10000 + index)
    for index, goal in enumerate(goals, 1):
        SavingsGoal.objects.filter(pk=goal.pk).update(priority=index)


@transaction.atomic
def reconcile_savings(household, actor=None):
    """Explicit action: assign any drift between ledger and goals to Unallocated Savings."""
    ensure_open(household, timezone.localdate())
    list(Account.objects.select_for_update().filter(household=household, type=Account.Type.SAVINGS))
    _, unallocated = ensure_savings_goals(household)
    if not unallocated:
        return Decimal("0.00")
    unallocated = SavingsGoal.objects.select_for_update().get(pk=unallocated.pk)
    difference = savings_drift(household)
    if difference:
        SavingsGoalEntry.objects.create(
            goal=unallocated,
            amount=difference,
            date=timezone.localdate(),
            kind=SavingsGoalEntry.Kind.RECONCILIATION,
            note="Assigned to Unallocated Savings to match the Savings balance",
            created_by=actor,
        )
        audit(
            household,
            actor,
            unallocated,
            "update",
            {"reconciliation": str(difference), "reason": "ledger drift assigned"},
        )
    return difference


def savings_entries_for(obj):
    if isinstance(obj, Income):
        return SavingsGoalEntry.objects.filter(income_allocation__income=obj)
    return obj.savings_goal_entries.all()


def assert_goals_non_negative(goal_ids):
    for goal in SavingsGoal.objects.select_for_update().filter(pk__in=set(goal_ids)):
        total = _goal_amount(goal)
        if total < 0:
            raise ValidationError(
                f"{goal.name} would be short by ${-total:,.2f}: money from this transaction "
                "has already been spent. Move or restore that spending first."
            )


def _reopen_fulfilled_goals(household, actor, entries):
    for goal in {e.goal_id: e.goal for e in entries if e.fulfills_goal}.values():
        goal.refresh_from_db()
        if goal.status == SavingsGoal.Status.ARCHIVED:
            restore_savings_goal(goal, actor)


def snapshot_savings_entries(obj, *, detach_income=False):
    """Capture a record's goal entries before it is rewritten or removed."""
    entries = list(savings_entries_for(obj).select_related("goal").order_by("pk"))
    if detach_income and entries and isinstance(obj, Income):
        # Allocation rows are rebuilt on edit; keep the entries instead of cascading them.
        SavingsGoalEntry.objects.filter(pk__in=[e.pk for e in entries]).update(
            income_allocation=None
        )
    return entries


def finish_savings_removal(household, actor, entries):
    """After a record and its entries were deleted: reopen fulfilled goals, verify balances."""
    if not entries:
        return
    _reopen_fulfilled_goals(household, actor, entries)
    assert_goals_non_negative(e.goal_id for e in entries)


def rewrite_savings_entries(
    *,
    household,
    actor,
    old_entries,
    entry_date,
    amount,
    kind,
    outflow=None,
    inflow=None,
    inflow_kind=None,
    income_allocation=None,
    expense=None,
    refund=None,
    transfer=None,
):
    """Re-sync goal entries after an edit.

    Unchanged goal splits and amounts are updated in place (date only) so completed or
    archived goals can still have their records corrected; anything else is re-validated.
    """
    if inflow and inflow[0] is None:
        raise ValidationError("Choose an active savings goal for money entering Savings.")
    if outflow and sum((money(v) for _, v, _ in outflow if money(v) > 0), Decimal("0.00")) != money(
        amount
    ):
        raise ValidationError("Savings goal amounts must add up exactly to the transaction total.")
    wanted = sorted(
        [(g.pk, -money(v), bool(f)) for g, v, f in outflow or [] if money(v) > 0]
        + ([(inflow[0].pk, money(inflow[1]), False)] if inflow else [])
    )
    current = sorted((e.goal_id, e.amount, e.fulfills_goal) for e in old_entries)
    if wanted == current:
        for entry in old_entries:
            entry.date = entry_date
            if income_allocation is not None:
                entry.income_allocation = income_allocation
            entry.save(update_fields=["date", "income_allocation"])
        for entry in old_entries:
            if entry.amount < 0:
                as_of = entry.goal.entries.filter(date__lte=entry_date).aggregate(
                    total=Sum("amount")
                )["total"] or Decimal("0.00")
                if as_of < 0:
                    raise ValidationError(
                        f"{entry.goal.name} did not have enough allocated funds as of "
                        f"{entry_date:%b %d, %Y}."
                    )
            else:
                # Moving money in later can leave earlier spending without funds.
                for spent in entry.goal.entries.filter(amount__lt=0, date__lt=entry_date):
                    as_of = entry.goal.entries.filter(date__lte=spent.date).aggregate(
                        total=Sum("amount")
                    )["total"] or Decimal("0.00")
                    if as_of < 0:
                        raise ValidationError(
                            f"{entry.goal.name} would not have enough allocated funds as of "
                            f"{spent.date:%b %d, %Y}."
                        )
        return
    SavingsGoalEntry.objects.filter(pk__in=[e.pk for e in old_entries]).delete()
    _reopen_fulfilled_goals(household, actor, old_entries)
    if outflow:
        record_savings_outflow(
            household=household,
            actor=actor,
            allocations=outflow,
            amount=amount,
            entry_date=entry_date,
            kind=kind,
            expense=expense,
            transfer=transfer,
        )
    if inflow:
        record_savings_inflow(
            household=household,
            actor=actor,
            goal=inflow[0],
            amount=inflow[1],
            entry_date=entry_date,
            kind=inflow_kind or kind,
            income_allocation=income_allocation,
            refund=refund,
            transfer=transfer,
        )
    assert_goals_non_negative(
        [e.goal_id for e in old_entries]
        + [g.pk for g, v, _ in outflow or [] if money(v) > 0]
        + ([inflow[0].pk] if inflow else [])
    )


@transaction.atomic
def create_savings_goal(
    *, household, actor, name, target_amount, priority, initial_amount=0, source_goal=None
):
    target = money(target_amount)
    if target <= 0:
        raise ValidationError("The goal target must be greater than zero.")
    active_count = SavingsGoal.objects.filter(
        household=household, status=SavingsGoal.Status.ACTIVE, is_system=False
    ).count()
    position = max(1, min(int(priority or active_count + 1), active_count + 1))
    SavingsGoal.objects.filter(
        household=household,
        status=SavingsGoal.Status.ACTIVE,
        is_system=False,
        priority__gte=position,
    ).update(priority=models.F("priority") + 1)
    goal = SavingsGoal.objects.create(
        household=household,
        name=name.strip(),
        target_amount=target,
        priority=position,
    )
    initial = money(initial_amount or 0)
    if initial:
        reallocate_savings(
            household=household,
            actor=actor,
            entry_date=timezone.localdate(),
            allocations=[(source_goal, initial, False)],
            destination_goal=goal,
            amount=initial,
            note=f"Initial allocation to {goal.name}",
        )
    normalize_goal_priorities(household)
    audit(household, actor, goal, "create", {"target_amount": str(target)})
    return goal


@transaction.atomic
def reallocate_savings(
    *, household, actor, entry_date, allocations, destination_goal, amount, note=""
):
    ensure_open(household, entry_date)
    _validate_incoming_goal(household, destination_goal)
    if any(goal.pk == destination_goal.pk for goal, _, _ in allocations or []):
        raise ValidationError("The source and destination savings goals must be different.")
    # Reassigning Savings is not spending and cannot itself fulfill a goal.
    source_allocations = [(goal, value, False) for goal, value, _ in allocations or []]
    record_savings_outflow(
        household=household,
        actor=actor,
        allocations=source_allocations,
        amount=amount,
        entry_date=entry_date,
        kind=SavingsGoalEntry.Kind.REALLOCATION_OUT,
    )
    SavingsGoalEntry.objects.create(
        goal=destination_goal,
        amount=money(amount),
        date=entry_date,
        kind=SavingsGoalEntry.Kind.REALLOCATION_IN,
        note=note,
        created_by=actor,
    )


@transaction.atomic
def complete_savings_goal(goal, actor):
    goal = SavingsGoal.objects.select_for_update().get(pk=goal.pk)
    if goal.is_system or goal.status != SavingsGoal.Status.ACTIVE:
        raise ValidationError("Only an active savings goal can be completed.")
    if _goal_amount(goal) < goal.target_amount:
        raise ValidationError("This goal must reach its target before it can be completed.")
    goal.status = SavingsGoal.Status.COMPLETED
    goal.completed_at = timezone.now()
    goal.priority = None
    goal.save(update_fields=["status", "completed_at", "priority"])
    normalize_goal_priorities(goal.household)
    audit(goal.household, actor, goal, "update", {"status": "completed"})


@transaction.atomic
def archive_savings_goal(goal, actor):
    goal = SavingsGoal.objects.select_for_update().get(pk=goal.pk)
    if goal.is_system or goal.status != SavingsGoal.Status.COMPLETED:
        raise ValidationError("Complete this goal before archiving it for long-term Savings.")
    goal.status = SavingsGoal.Status.ARCHIVED
    goal.archived_at = timezone.now()
    goal.priority = None
    goal.save(update_fields=["status", "archived_at", "priority"])
    audit(goal.household, actor, goal, "update", {"status": "archived"})


@transaction.atomic
def restore_savings_goal(goal, actor):
    goal = SavingsGoal.objects.select_for_update().get(pk=goal.pk)
    if goal.is_system or goal.status != SavingsGoal.Status.ARCHIVED:
        raise ValidationError("Only an archived goal can be restored.")
    if (
        SavingsGoal.objects.filter(household=goal.household, name=goal.name)
        .exclude(pk=goal.pk)
        .exclude(status=SavingsGoal.Status.ARCHIVED)
        .exists()
    ):
        raise ValidationError("Rename the current goal with this name before restoring this one.")
    goal.status = SavingsGoal.Status.ACTIVE
    goal.archived_at = None
    goal.completed_at = None
    goal.priority = (
        SavingsGoal.objects.filter(
            household=goal.household,
            status=SavingsGoal.Status.ACTIVE,
            is_system=False,
        ).count()
        + 1
    )
    goal.save(update_fields=["status", "archived_at", "completed_at", "priority"])
    normalize_goal_priorities(goal.household)
    audit(goal.household, actor, goal, "update", {"status": "active"})


@transaction.atomic
def delete_savings_goal(goal, actor):
    goal = SavingsGoal.objects.select_for_update().get(pk=goal.pk)
    if goal.is_system or goal.entries.exists():
        raise ValidationError("A goal can be deleted only before any money has been assigned.")
    household = goal.household
    audit(household, actor, goal, "delete", {"name": goal.name})
    goal.delete()
    normalize_goal_priorities(household)


@transaction.atomic
def create_income(
    *,
    household,
    actor,
    income_date,
    source,
    amount,
    allocations,
    notes="",
    irregular=False,
    savings_goal=None,
    is_paycheck=False,
):
    ensure_open(household, income_date)
    if is_paycheck and irregular:
        raise ValidationError("A regular paycheck cannot also be irregular income.")
    total = money(amount)
    normalized = [(account, money(value)) for account, value in allocations if money(value) > 0]
    if not normalized or sum((value for _, value in normalized), Decimal("0")) != total:
        raise ValidationError("Account allocations must add up exactly to the income total.")
    if any(account.household_id != household.id for account, _ in normalized):
        raise ValidationError("Every account must belong to this household.")
    income = Income.objects.create(
        household=household,
        created_by=actor,
        date=income_date,
        source=source,
        amount=total,
        notes=notes,
        is_irregular=irregular,
        is_paycheck=is_paycheck,
    )
    IncomeAllocation.objects.bulk_create(
        [IncomeAllocation(income=income, account=a, amount=v) for a, v in normalized]
    )
    savings_values = [
        (account, value) for account, value in normalized if account.type == Account.Type.SAVINGS
    ]
    if savings_values:
        if savings_goal is None and not SavingsGoal.objects.filter(household=household).exists():
            savings_goal, _ = ensure_savings_goals(household)
        for savings_account_, savings_amount in savings_values:
            allocation = IncomeAllocation.objects.get(income=income, account=savings_account_)
            record_savings_inflow(
                household=household,
                actor=actor,
                goal=savings_goal,
                amount=savings_amount,
                entry_date=income_date,
                kind=SavingsGoalEntry.Kind.INCOME,
                income_allocation=allocation,
            )
    audit(household, actor, income, "create", {"amount": str(total), "source": source})
    return income


@transaction.atomic
def create_transfer(
    *,
    household,
    actor,
    entry_date,
    source,
    destination,
    amount,
    notes="",
    savings_source_allocations=None,
    savings_destination_goal=None,
):
    ensure_open(household, entry_date)
    if (
        source == destination
        or source.household_id != household.id
        or destination.household_id != household.id
    ):
        raise ValidationError("Choose two different household accounts.")
    obj = Transfer.objects.create(
        household=household,
        created_by=actor,
        date=entry_date,
        source=source,
        destination=destination,
        amount=money(amount),
        notes=notes,
    )
    if source.type == Account.Type.SAVINGS:
        record_savings_outflow(
            household=household,
            actor=actor,
            allocations=savings_source_allocations,
            amount=obj.amount,
            entry_date=entry_date,
            kind=SavingsGoalEntry.Kind.TRANSFER_OUT,
            transfer=obj,
        )
    if destination.type == Account.Type.SAVINGS:
        if (
            savings_destination_goal is None
            and not SavingsGoal.objects.filter(household=household).exists()
        ):
            savings_destination_goal, _ = ensure_savings_goals(household)
        record_savings_inflow(
            household=household,
            actor=actor,
            goal=savings_destination_goal,
            amount=obj.amount,
            entry_date=entry_date,
            kind=SavingsGoalEntry.Kind.TRANSFER_IN,
            transfer=obj,
        )
    audit(
        household,
        actor,
        obj,
        "create",
        {"amount": str(obj.amount), "source": source.name, "destination": destination.name},
    )
    return obj


@transaction.atomic
def create_expense(
    *,
    household,
    actor,
    entry_date,
    account,
    category,
    amount,
    merchant="",
    notes="",
    bill=None,
    occurrence=None,
    splits=None,
    savings_allocations=None,
    essential_override=None,
):
    ensure_open(household, entry_date)
    normalized_splits = (
        [(split_category, money(split_amount)) for split_category, split_amount in splits]
        if splits is not None
        else None
    )
    categories = [split_category for split_category, _ in normalized_splits or []]
    if normalized_splits is not None:
        if len(normalized_splits) < 2:
            raise ValidationError("Add at least two category amounts for a split transaction.")
        if sum((value for _, value in normalized_splits), Decimal("0.00")) != money(amount):
            raise ValidationError("Split amounts must add up exactly to the expense total.")
        if len({item.pk for item in categories}) != len(categories):
            raise ValidationError("Use each split category only once.")
        if any(value <= 0 for _, value in normalized_splits):
            raise ValidationError("Every split amount must be greater than zero.")
        category = categories[0]
    if category is None:
        raise ValidationError("Choose a category.")
    if account.household_id != household.id or any(
        item.household_id != household.id for item in [category, *categories]
    ):
        raise ValidationError("Choose an account and category from this budget profile.")
    if account.archived_at or any(item.archived_at for item in [category, *categories]):
        raise ValidationError("Choose an active account and category.")
    chosen_occurrence = None
    if bill:
        if bill.household_id != household.id:
            raise ValidationError("Choose an active bill from this budget profile.")
        if not bill.active and occurrence is None:
            # A removed bill can still be paid, but only for a live month named explicitly.
            raise ValidationError("Choose an active bill from this budget profile.")
        if account.type not in {Account.Type.BILLS, Account.Type.SPENDING}:
            raise ValidationError("Pay a bill from the Bills or Spending account.")
        if any(item.type != Category.Type.BILL for item in [category, *categories]):
            raise ValidationError("Choose a Bills category for the selected bill.")
        if occurrence is not None:
            validate_occurrence_for_bill(bill, occurrence)
            chosen_occurrence = occurrence
        else:
            chosen_occurrence = choose_occurrence(bill, entry_date)
    elif occurrence is not None:
        raise ValidationError("Choose a bill before choosing a bill month.")
    elif any(not expense_category_allowed(account, item) for item in [category, *categories]):
        raise ValidationError("Category does not match the selected account.")
    obj = Expense.objects.create(
        household=household,
        created_by=actor,
        date=entry_date,
        account=account,
        category=category,
        amount=money(amount),
        merchant=merchant.strip(),
        notes=notes,
        bill_occurrence=chosen_occurrence,
        is_essential_override=essential_override,
    )
    if normalized_splits is not None:
        ExpenseSplit.objects.bulk_create(
            [
                ExpenseSplit(expense=obj, category=split_category, amount=split_amount)
                for split_category, split_amount in normalized_splits
            ]
        )
    changes: dict[str, object] = {"amount": str(obj.amount)}
    if normalized_splits is not None:
        changes["splits"] = [
            {"category": split_category.name, "amount": str(split_amount)}
            for split_category, split_amount in normalized_splits
        ]
    if account.type == Account.Type.SAVINGS:
        record_savings_outflow(
            household=household,
            actor=actor,
            allocations=savings_allocations,
            amount=obj.amount,
            entry_date=entry_date,
            kind=SavingsGoalEntry.Kind.EXPENSE,
            expense=obj,
        )
    audit(household, actor, obj, "create", changes)
    if chosen_occurrence:
        update_bill_status(chosen_occurrence)
    return obj


REFUND_CATEGORY_TYPES = (Category.Type.SPENDING, Category.Type.BILL)


def _refund_total(expense, exclude_refund=None):
    refunds = expense.refunds.all()
    if exclude_refund is not None and exclude_refund.pk:
        refunds = refunds.exclude(pk=exclude_refund.pk)
    return refunds.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def validate_refund_rules(
    *, household, entry_date, category, amount, original_expense=None, refund=None
):
    """Enforce refund integrity rules; call inside a transaction.

    Returns the original expense re-fetched with a row lock (or None). ``refund`` is the
    refund being edited, whose old amount is excluded from the total.
    """
    if category.household_id != household.pk or category.type not in REFUND_CATEGORY_TYPES:
        raise ValidationError("A refund must use a Spending or Bills category from this budget.")
    if original_expense is None:
        return None
    locked = (
        Expense.objects.select_for_update()
        .filter(pk=original_expense.pk, household=household)
        .first()
    )
    if locked is None:
        raise ValidationError("The original expense must belong to this budget profile.")
    if entry_date < locked.date:
        raise ValidationError(
            f"The refund date cannot be before the original expense date ({locked.date:%Y-%m-%d})."
        )
    already = _refund_total(locked, exclude_refund=refund)
    if already + money(amount) > locked.amount:
        remaining = max(locked.amount - already, Decimal("0.00"))
        raise ValidationError(
            f"Refunds for this expense cannot exceed its ${locked.amount:,.2f} amount; "
            f"${remaining:,.2f} remains refundable."
        )
    return locked


def validate_expense_against_refunds(expense, *, new_amount, new_date):
    """An expense cannot shrink below, or move after, the refunds linked to it."""
    refunds = expense.refunds.all()
    total = refunds.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    if money(new_amount) < total:
        raise ValidationError(
            f"This expense has ${total:,.2f} in linked refunds, so its amount cannot be "
            "lower than that. Edit or remove the refunds first."
        )
    earliest = refunds.order_by("date").values_list("date", flat=True).first()
    if earliest is not None and new_date > earliest:
        raise ValidationError(
            f"A linked refund is dated {earliest:%Y-%m-%d}, so this expense cannot be dated "
            "after it. Edit the refund first."
        )


FORM_SUBMISSION_RETENTION_DAYS = 30


def claim_form_token(household, token):
    """Record a create-form token. Returns False when it was already used.

    Call inside the transaction that creates the record so a failure releases the token.
    """
    FormSubmission.objects.filter(
        created_at__lt=timezone.now() - timedelta(days=FORM_SUBMISSION_RETENTION_DAYS)
    ).delete()
    try:
        with transaction.atomic():
            FormSubmission.objects.create(token=token, household=household)
    except IntegrityError:
        return False
    return True


@transaction.atomic
def create_refund(
    *,
    household,
    actor,
    entry_date,
    account,
    category,
    amount,
    merchant="",
    notes="",
    original_expense=None,
    savings_goal=None,
):
    ensure_open(household, entry_date)
    if account.household_id != household.pk or category.household_id != household.pk:
        raise ValidationError("Choose an account and category from this budget profile.")
    amount = money(amount)
    original_expense = validate_refund_rules(
        household=household,
        entry_date=entry_date,
        category=category,
        amount=amount,
        original_expense=original_expense,
    )
    obj = Refund.objects.create(
        household=household,
        created_by=actor,
        date=entry_date,
        account=account,
        category=category,
        amount=money(amount),
        merchant=merchant.strip(),
        notes=notes,
        original_expense=original_expense,
    )
    if account.type == Account.Type.SAVINGS:
        record_savings_inflow(
            household=household,
            actor=actor,
            goal=savings_goal,
            amount=obj.amount,
            entry_date=entry_date,
            kind=SavingsGoalEntry.Kind.REFUND,
            refund=obj,
        )
    audit(household, actor, obj, "create", {"amount": str(obj.amount)})
    return obj


def expense_category_allowed(account, category):
    if account.type == Account.Type.BILLS:
        return category.type == Category.Type.BILL
    if account.type == Account.Type.SPENDING:
        return category.type == Category.Type.SPENDING or (
            category.type == Category.Type.BILL and category.name.casefold() == "utilities"
        )
    return category.type == Category.Type.SPENDING


def audit(household, actor, obj, action, changes):
    AuditEvent.objects.create(
        household=household,
        actor=actor,
        record_type=obj._meta.label,
        record_id=str(obj.pk),
        action=action,
        changes=changes,
    )


def prorated_target(weekly: Decimal, start: date, end: date) -> Decimal:
    return money(weekly * Decimal((end - start).days + 1) / Decimal(7))


def occurrence_due_date(year: int, month: int, due_day: int) -> date:
    return date(year, month, min(due_day, calendar.monthrange(year, month)[1]))


def period_for_due_date(household, due):
    return PayPeriod.objects.filter(
        household=household, start_date__lte=due, end_date__gte=due
    ).first()


def occurrence_for_expense_date(bill: RecurringBill, expense_date: date) -> BillOccurrence:
    """Resolve the bill occurrence in the expense date's calendar month."""
    existing = BillOccurrence.objects.filter(
        bill=bill,
        due_date__year=expense_date.year,
        due_date__month=expense_date.month,
    ).first()
    if existing:
        return existing
    due = occurrence_due_date(expense_date.year, expense_date.month, bill.due_day)
    return BillOccurrence.objects.create(
        bill=bill,
        category=bill.category,
        account=bill.account,
        due_date=due,
        expected_amount=bill.default_amount,
        pay_period=period_for_due_date(bill.household, due),
    )


def computed_bill_status(occurrence: BillOccurrence) -> str:
    """The status an occurrence should have given its amounts and linked payments."""
    if occurrence.status == BillOccurrence.Status.SKIPPED:
        return BillOccurrence.Status.SKIPPED
    paid, target = occurrence.amount_paid, occurrence.target_amount
    if paid >= target:
        return BillOccurrence.Status.PAID
    if paid > 0:
        return BillOccurrence.Status.PARTIAL
    if occurrence.confirmed_amount is not None:
        return BillOccurrence.Status.CONFIRMED
    return BillOccurrence.Status.UNCONFIRMED


def update_bill_status(occurrence: BillOccurrence):
    if occurrence.status == BillOccurrence.Status.SKIPPED:
        return
    occurrence.status = computed_bill_status(occurrence)
    occurrence.save(update_fields=["status"])


def _add_months(first_of_month: date, months: int) -> date:
    index = first_of_month.year * 12 + first_of_month.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def end_of_month_ahead(today: date, months: int) -> date:
    """Last day of the month ``months`` after ``today``'s month."""
    return _add_months(today.replace(day=1), months + 1) - timedelta(days=1)


@transaction.atomic
def ensure_bill_occurrences(
    household, through: date, start: date | None = None
) -> list[BillOccurrence]:
    """Create the missing monthly occurrences of every bill that is live in the range.

    Existing rows are never modified, so the call is idempotent. Inactive bills only get
    occurrences up to their ``effective_end``; an inactive bill without one gets none.
    Returns the occurrences created.
    """
    first = (start or timezone.localdate()).replace(day=1)
    created: list[BillOccurrence] = []
    bills = RecurringBill.objects.filter(household=household).select_related("category", "account")
    for bill in bills:
        if not bill.active and bill.effective_end is None:
            continue
        month = max(first, bill.effective_start.replace(day=1))
        existing = {
            (item.due_date.year, item.due_date.month)
            for item in bill.occurrences.filter(due_date__gte=month)
        }
        while month <= through:
            due = occurrence_due_date(month.year, month.month, bill.due_day)
            if bill.effective_end is not None and due > bill.effective_end:
                break
            if (
                (month.year, month.month) not in existing
                and due >= bill.effective_start
                and bill_due_in_month(bill, month)
            ):
                created.append(
                    BillOccurrence.objects.create(
                        bill=bill,
                        category=bill.category,
                        account=bill.account,
                        due_date=due,
                        expected_amount=bill.default_amount,
                        pay_period=period_for_due_date(household, due),
                    )
                )
            month = _add_months(month, 1)
    return created


def bill_due_in_month(bill: RecurringBill, month: date) -> bool:
    """Whether a bill billed every ``every_months`` months is due in ``month``.

    The schedule counts from ``schedule_start`` (or ``effective_start``): that month and
    every ``every_months`` months after it. Months before it are not part of the schedule.
    """
    anchor = (bill.schedule_start or bill.effective_start).replace(day=1)
    months = (month.year - anchor.year) * 12 + month.month - anchor.month
    return months >= 0 and months % (bill.every_months or 1) == 0


def change_bill_frequency(bill: RecurringBill, actor, every_months: int, first_month: date):
    """Change how often a bill is due, starting with ``first_month``.

    Bill months from today on that no longer fit are removed, unless they have payments,
    were skipped on purpose, or sit in a locked pay period; past months are history and
    stay. Returns (removed, kept) counts. Missing months are generated afterwards.
    """
    if every_months not in RecurringBill.Frequency.values:
        raise ValidationError("Choose how often the bill is due.")
    first_month = first_month.replace(day=1)
    if bill.effective_end and first_month > bill.effective_end:
        raise ValidationError("The first due month is after the bill ends.")
    today = timezone.localdate()
    removed = kept = 0
    with transaction.atomic():
        bill = RecurringBill.objects.select_for_update().get(pk=bill.pk)
        before_every, before_start = bill.every_months, bill.schedule_start
        bill.every_months = every_months
        bill.schedule_start = first_month
        bill.save(update_fields=["every_months", "schedule_start"])
        for occurrence in bill.occurrences.filter(due_date__gte=today):
            if bill_due_in_month(bill, occurrence.due_date.replace(day=1)):
                continue
            locked = PayPeriod.objects.filter(
                household=bill.household,
                start_date__lte=occurrence.due_date,
                end_date__gte=occurrence.due_date,
                is_locked=True,
            ).exists()
            if (
                occurrence.status == BillOccurrence.Status.SKIPPED
                or occurrence.expenses.exists()
                or locked
            ):
                kept += 1
                continue
            audit(bill.household, actor, occurrence, "delete", {"reason": "bill frequency changed"})
            occurrence.delete()
            removed += 1
        audit(
            bill.household,
            actor,
            bill,
            "update",
            {
                "every_months": [before_every, every_months],
                "schedule_start": [
                    before_start.isoformat() if before_start else None,
                    first_month.isoformat(),
                ],
                "removed_months": removed,
            },
        )
        ensure_bill_occurrences(bill.household, through=end_of_month_ahead(today, 3), start=today)
    return removed, kept


CHOOSE_OCCURRENCE_WINDOW_DAYS = 45


def validate_occurrence_for_bill(bill: RecurringBill, occurrence: BillOccurrence):
    if occurrence.bill_id != bill.pk:
        raise ValidationError("Choose a bill month that belongs to the selected bill.")
    if occurrence.status == BillOccurrence.Status.SKIPPED:
        raise ValidationError("A skipped bill month cannot receive a payment.")
    if not live_bill_occurrences(bill.household).filter(pk=occurrence.pk).exists():
        raise ValidationError("That bill month is no longer an active obligation.")


def choose_occurrence(bill: RecurringBill, expense_date: date, exclude_expense=None):
    """Pick the unpaid occurrence a payment dated ``expense_date`` most likely settles.

    Among the bill's live occurrences that still have a balance (ignoring the payment being
    edited) and are due within 45 days of the payment, the closest due date wins and ties go
    to the earlier one. With no candidate, fall back to the payment's calendar month.
    """
    window = timedelta(days=CHOOSE_OCCURRENCE_WINDOW_DAYS)
    candidates = []
    for item in live_bill_occurrences(bill.household).filter(
        bill=bill,
        due_date__gte=expense_date - window,
        due_date__lte=expense_date + window,
    ):
        if item.status == BillOccurrence.Status.PAID:
            continue
        payments = item.expenses.all()
        if exclude_expense is not None and exclude_expense.pk:
            payments = payments.exclude(pk=exclude_expense.pk)
        paid = payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        if item.target_amount - paid > 0:
            candidates.append(item)
    if candidates:
        return min(candidates, key=lambda item: (abs(item.due_date - expense_date), item.due_date))
    fallback = occurrence_for_expense_date(bill, expense_date)
    if fallback.status == BillOccurrence.Status.SKIPPED:
        raise ValidationError("A skipped bill month cannot receive a payment.")
    return fallback


@transaction.atomic
def remove_bill(bill: RecurringBill, actor, effective_end: date) -> int:
    """Stop a bill after ``effective_end`` and drop its unpaid later occurrences.

    Occurrences with payments are kept. Returns how many were kept. Refuses the whole
    removal when any affected due date falls in a locked pay period.
    """
    later = list(bill.occurrences.filter(due_date__gt=effective_end).order_by("due_date"))
    for item in later:
        ensure_open(bill.household, item.due_date)
    doomed = [item for item in later if not item.expenses.exists()]
    kept = len(later) - len(doomed)
    bill.active = False
    bill.effective_end = effective_end
    bill.save(update_fields=["active", "effective_end"])
    changes: dict[str, object] = {
        "active": False,
        "effective_end": effective_end.isoformat(),
        "history_preserved": True,
        "occurrences_deleted": len(doomed),
        "occurrences_kept": kept,
    }
    if doomed:
        changes["deleted_from"] = doomed[0].due_date.isoformat()
        changes["deleted_to"] = doomed[-1].due_date.isoformat()
        BillOccurrence.objects.filter(pk__in=[item.pk for item in doomed]).delete()
    audit(bill.household, actor, bill, "delete", changes)
    return kept


# --- Shared Phase 2 definitions -------------------------------------------------------


def net_spending(household, start: date, end: date, category_type=Category.Type.SPENDING):
    """The one definition of spending used by the dashboard, outlook and reports.

    Expenses (split allocations when present) in categories of ``category_type`` on any
    account, minus refunds in categories of that type, dated ``start``..``end``
    inclusive. Transfers and balance corrections never count.
    """
    unsplit = Expense.objects.filter(
        household=household,
        date__range=(start, end),
        splits__isnull=True,
        category__type=category_type,
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    split = ExpenseSplit.objects.filter(
        expense__household=household,
        expense__date__range=(start, end),
        category__type=category_type,
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    refunds = Refund.objects.filter(
        household=household, date__range=(start, end), category__type=category_type
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    return money(unsplit + split - refunds)


def live_bill_occurrences(household):
    """Occurrences that are real obligations: not skipped and inside the bill's life.

    An occurrence of a removed bill counts only up to the bill's ``effective_end``; a
    bill made inactive without an end date contributes nothing.
    """
    return (
        BillOccurrence.objects.filter(bill__household=household)
        .filter(due_date__gte=models.F("bill__effective_start"))
        .filter(
            models.Q(bill__active=True, bill__effective_end=None)
            | models.Q(due_date__lte=models.F("bill__effective_end"))
        )
        .exclude(status=BillOccurrence.Status.SKIPPED)
    )


RECENT_BILL_PAYMENTS = 3


def recent_paid_amounts(occurrences) -> dict[int, list[Decimal]]:
    """For each unconfirmed occurrence, what its bill's last fully paid earlier ones cost.

    One query for all of them; the same figures ``bill_forecast`` would read one by one.
    """
    wanted = {o.pk: o for o in occurrences if o.status == BillOccurrence.Status.UNCONFIRMED}
    paid = list(
        BillOccurrence.objects.filter(
            bill_id__in={o.bill_id for o in wanted.values()},
            status=BillOccurrence.Status.PAID,
        )
        .annotate(paid_total=paid_total())
        .order_by("-due_date")
    )
    return {
        pk: [
            p.paid_total or Decimal("0.00")
            for p in [p for p in paid if p.bill_id == o.bill_id and p.due_date < o.due_date][
                :RECENT_BILL_PAYMENTS
            ]
        ]
        for pk, o in wanted.items()
    }


def bill_forecast(occurrence: BillOccurrence, recent_paid: list[Decimal] | None = None) -> dict:
    """Forecast amount for one occurrence, with the basis shown to the user.

    A paid occurrence is settled at what was paid. Otherwise a confirmed amount wins, and
    failing that the forecast is the larger of the expected
    amount and the average paid on the bill's last three fully paid earlier occurrences,
    so a stale default never understates a variable bill. Nothing is written back.
    """
    paid = occurrence.amount_paid
    if occurrence.status == BillOccurrence.Status.SKIPPED:
        amount, basis = Decimal("0.00"), "skipped"
    elif occurrence.status == BillOccurrence.Status.PAID:
        # Paid is settled: a recent average above what was paid must not leave a remainder.
        amount, basis = paid, "paid"
    elif occurrence.confirmed_amount is not None:
        amount, basis = occurrence.confirmed_amount, "confirmed amount"
    else:
        recent = (
            recent_paid
            if recent_paid is not None
            else [
                item.amount_paid
                for item in BillOccurrence.objects.filter(
                    bill_id=occurrence.bill_id,
                    status=BillOccurrence.Status.PAID,
                    due_date__lt=occurrence.due_date,
                ).order_by("-due_date")[:RECENT_BILL_PAYMENTS]
            ]
        )
        average = money(sum(recent, Decimal("0.00")) / len(recent)) if recent else None
        if average is not None and average > occurrence.expected_amount:
            amount = average
            basis = f"average of last {len(recent)} payment{'s' if len(recent) != 1 else ''}"
        else:
            amount, basis = occurrence.expected_amount, "expected amount"
    return {
        "amount": money(amount),
        "basis": basis,
        "paid": money(paid),
        "remaining": money(max(amount - paid, Decimal("0.00"))),
    }


def pay_bill_occurrence(occurrence: BillOccurrence, actor, entry_date: date):
    """Pay the remaining amount of a bill occurrence in one step; returns the Expense."""
    with transaction.atomic():
        locked = (
            BillOccurrence.objects.select_for_update().select_related("bill").get(pk=occurrence.pk)
        )
        name = locked.bill.name
        household = locked.bill.household
        if locked.status == BillOccurrence.Status.SKIPPED:
            raise ValidationError(f"{name} is skipped and cannot be paid.")
        ensure_open(household, entry_date)
        if locked.confirmed_amount is None:
            locked.confirmed_amount = locked.expected_amount
            locked.status = BillOccurrence.Status.CONFIRMED
            locked.save(update_fields=["confirmed_amount", "status"])
            audit(
                household,
                actor,
                locked,
                "update",
                {"confirmed_amount": str(locked.confirmed_amount), "status": locked.status},
            )
        remaining = money(locked.target_amount - locked.amount_paid)
        if remaining <= 0:
            raise ValidationError(f"{name} is already paid.")
        expense = create_expense(
            household=household,
            actor=actor,
            entry_date=entry_date,
            account=locked.effective_account,
            category=locked.effective_category,
            amount=remaining,
            merchant=name,
            notes="Marked paid from Bills",
            bill=locked.bill,
            occurrence=locked,
        )
        update_bill_status(locked)
        return expense


def confirm_unchanged_bills(occurrences, actor):
    """Confirm each UNCONFIRMED occurrence at its expected amount; returns (confirmed, locked)."""
    confirmed = locked_count = 0
    with transaction.atomic():
        for item in occurrences:
            item = BillOccurrence.objects.select_for_update().select_related("bill").get(pk=item.pk)
            if item.status != BillOccurrence.Status.UNCONFIRMED:
                continue
            try:
                ensure_open(item.bill.household, item.due_date)
            except ValidationError:
                locked_count += 1
                continue
            item.confirmed_amount = item.expected_amount
            item.status = BillOccurrence.Status.CONFIRMED
            item.save(update_fields=["confirmed_amount", "status"])
            update_bill_status(item)
            audit(
                item.bill.household,
                actor,
                item,
                "update",
                {"confirmed_amount": str(item.confirmed_amount), "status": item.status},
            )
            confirmed += 1
    return confirmed, locked_count


INCOME_SOURCE_CAP = 300


def _latest_income_per_source(household, source=None, limit=INCOME_SOURCE_CAP):
    """Most recent Income per case-insensitive source, newest first."""
    rows = Income.objects.filter(household=household)
    if source is not None:
        rows = rows.filter(source__iexact=source.strip())
    seen: dict[str, int] = {}
    for income_id, name in rows.order_by("-date", "-pk").values_list("pk", "source").iterator():
        key = name.strip().casefold()
        if key and key not in seen:
            seen[key] = income_id
            if len(seen) >= limit:
                break
    incomes = Income.objects.in_bulk(list(seen.values()))
    return [incomes[income_id] for income_id in seen.values()]


def _income_profile(allocations):
    """Automatic-remainder profile: (remainder account, {account: fixed amount}) or None.

    The largest share (lowest account id on ties) is the remainder account. A previous split that
    used an archived account yields no profile.
    """
    if not allocations or any(a.account.archived_at for a in allocations):
        return None
    ranked = sorted(allocations, key=lambda a: (-a.amount, a.account_id))
    return ranked[0].account, {a.account: a.amount for a in ranked[1:]}


def income_source_defaults(household):
    """Per-source defaults for the income form: [(source, profile dict)], newest first."""
    incomes = _latest_income_per_source(household)
    allocations: dict[int, list] = {income.pk: [] for income in incomes}
    for allocation in IncomeAllocation.objects.filter(income_id__in=allocations).select_related(
        "account"
    ):
        allocations[allocation.income_id].append(allocation)
    result = []
    for income in incomes:
        profile = _income_profile(allocations[income.pk])
        entry = {
            "is_paycheck": income.is_paycheck,
            "is_irregular": income.is_irregular,
            "remainder_account": profile[0].pk if profile else None,
            "fixed": {str(a.pk): str(v) for a, v in profile[1].items()} if profile else {},
        }
        result.append((income.source.strip(), entry))
    return result


def default_income_split(household, source, amount):
    """Split `amount` like the most recent income from `source` (automatic remainder).

    The account that got the largest share last time takes the remainder; every other account
    gets the same fixed amount as before. If the fixed amounts exceed `amount`, everything goes
    to the remainder account. Returns None when the source is unknown, the amount is not
    positive, or the previous split used an archived account.
    """
    if not source or not source.strip() or amount is None or Decimal(amount) <= 0:
        return None
    amount = money(amount)
    latest = _latest_income_per_source(household, source, limit=1)
    if not latest:
        return None
    allocations = list(IncomeAllocation.objects.filter(income=latest[0]).select_related("account"))
    profile = _income_profile(allocations)
    if profile is None:
        return None
    remainder, fixed = profile
    left = amount - sum(fixed.values(), Decimal("0"))
    if left < 0:
        return {remainder: amount}
    split = dict(fixed)
    if left > 0:
        split[remainder] = left
    return split
