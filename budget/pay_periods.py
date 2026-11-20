"""Pay periods anchored to the day the paycheck actually lands.

Rule: a pay period starts on the date its paycheck is
deposited and ends the day before the next period starts. A paycheck funds the period
whose nominal payday is nearest to the deposit date, looking up to
``PAYCHECK_EARLY_DAYS`` before and ``PAYCHECK_LATE_DAYS`` after the nominal payday. An
early deposit therefore belongs to the period it funds, not to the one it lands in, and
each paycheck counts in exactly one period.

Periods with no recorded paycheck keep their stored start (the expected deposit date for
generated periods). Locked periods never change. Period boundaries are derived data: no
financial row stores its period except ``BillOccurrence.pay_period``, which is
reassigned after every boundary change.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import BillOccurrence, Income, PayPeriod, PayPeriodBudget

PAYCHECK_EARLY_DAYS = 10
PAYCHECK_LATE_DAYS = 5


@dataclass
class PeriodChange:
    period: PayPeriod
    before: dict
    after: dict


@dataclass
class SyncResult:
    changes: list[PeriodChange] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    unassigned: list[Income] = field(default_factory=list)
    occurrences_moved: int = 0


def _state(period: PayPeriod) -> dict:
    return {
        "start_date": period.start_date,
        "end_date": period.end_date,
        "actual_deposit_date": period.actual_deposit_date,
    }


def paycheck_period(periods: list[PayPeriod], deposit: date) -> PayPeriod | None:
    """Period funded by a paycheck deposited on ``deposit``; ties go to the later payday."""
    candidates = [
        period
        for period in periods
        if -PAYCHECK_LATE_DAYS <= (period.nominal_payday - deposit).days <= PAYCHECK_EARLY_DAYS
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda period: (
            abs((period.nominal_payday - deposit).days),
            -period.nominal_payday.toordinal(),
        ),
    )


def assign_paychecks(household, periods: list[PayPeriod]):
    """Map period pk -> earliest paycheck deposit date; also return unassigned paychecks."""
    deposits: dict[int, date] = {}
    unassigned = []
    for income in Income.objects.filter(household=household, is_paycheck=True).order_by(
        "date", "pk"
    ):
        period = paycheck_period(periods, income.date)
        if period is None:
            unassigned.append(income)
            continue
        deposits.setdefault(period.pk, income.date)
    return deposits, unassigned


def reassign_bill_occurrence_periods(household) -> int:
    periods = list(PayPeriod.objects.filter(household=household).order_by("start_date"))
    moved = 0
    for occurrence in BillOccurrence.objects.filter(bill__household=household):
        matching = next(
            (p for p in periods if p.start_date <= occurrence.due_date <= p.end_date), None
        )
        if occurrence.pay_period_id != (matching.pk if matching else None):
            occurrence.pay_period = matching
            occurrence.save(update_fields=["pay_period"])
            moved += 1
    return moved


def plan_boundaries(household) -> SyncResult:
    """Compute the anchored boundaries without saving anything.

    Returned changes carry unsaved period objects with the new values applied.
    """
    periods = list(PayPeriod.objects.filter(household=household).order_by("start_date"))
    deposits, unassigned = assign_paychecks(household, periods)
    result = SyncResult(unassigned=unassigned)
    before = {period.pk: _state(period) for period in periods}

    for index, period in enumerate(periods):
        if period.is_locked:
            continue
        deposit = deposits.get(period.pk)
        if deposit is None:
            # Paycheck removed or never recorded: fall back to the expected deposit date
            # only when this period had been anchored; otherwise leave history alone.
            desired = period.expected_deposit_date if period.actual_deposit_date else None
            period.actual_deposit_date = None
        else:
            desired = deposit
            period.actual_deposit_date = deposit
        if desired is None or desired == period.start_date or index == 0:
            if index == 0 and desired is not None and desired != period.start_date:
                period.start_date = desired  # first period has no predecessor to adjust
            continue
        previous = periods[index - 1]
        following = periods[index + 1] if index + 1 < len(periods) else None
        if previous.is_locked:
            result.kept.append(
                f"{period}: kept start because the previous period {previous} is locked."
            )
            continue
        last_day = following.start_date - timedelta(days=1) if following else period.end_date
        if desired <= previous.start_date or desired > last_day:
            result.kept.append(
                f"{period}: deposit {desired:%b %-d, %Y} would empty a neighbouring period; "
                "start kept."
            )
            continue
        period.start_date = desired
        previous.end_date = desired - timedelta(days=1)

    for period in periods:
        if _state(period) != before[period.pk]:
            result.changes.append(PeriodChange(period, before[period.pk], _state(period)))
    return result


def sync_pay_period_boundaries(household, actor=None) -> SyncResult:
    """Anchor every unlocked period to its recorded paycheck and keep periods contiguous."""
    from .services import audit

    with transaction.atomic():
        list(PayPeriod.objects.select_for_update().filter(household=household))
        result = plan_boundaries(household)
        # Save in an order that never violates the unique start constraint: periods
        # moving earlier first (ascending), then periods moving later (descending).
        earlier = sorted(
            (c for c in result.changes if c.after["start_date"] <= c.before["start_date"]),
            key=lambda c: c.after["start_date"],
        )
        later = sorted(
            (c for c in result.changes if c.after["start_date"] > c.before["start_date"]),
            key=lambda c: c.after["start_date"],
            reverse=True,
        )
        for change in earlier + later:
            change.period.save(update_fields=["start_date", "end_date", "actual_deposit_date"])
            audit(
                household,
                actor,
                change.period,
                "update",
                {
                    "reason": "paycheck anchoring",
                    **{
                        key: value.isoformat() if value else None
                        for key, value in change.after.items()
                    },
                },
            )
        if result.changes:
            result.occurrences_moved = reassign_bill_occurrence_periods(household)
    return result


def create_period_budget(period: PayPeriod) -> PayPeriodBudget:
    """New period budget copied from the most recent earlier period's budget, else zero."""
    previous = (
        PayPeriodBudget.objects.filter(
            pay_period__household=period.household,
            pay_period__start_date__lt=period.start_date,
        )
        .order_by("-pay_period__start_date")
        .first()
    )
    if previous is None:
        return PayPeriodBudget.objects.create(pay_period=period)
    return PayPeriodBudget.objects.create(
        pay_period=period,
        spending_target=previous.spending_target,
        expected_paycheck=previous.expected_paycheck,
    )


def validate_contiguous_edit(period: PayPeriod, start: date, end: date) -> list[PayPeriod]:
    """Check a manual start/end change; return unsaved neighbours to adjust.

    A new period must sit exactly next to its neighbours (no gap, no overlap). Editing an
    existing period moves the adjacent period's matching boundary so periods stay
    contiguous; a locked neighbour, or a neighbour that would become empty, refuses it.
    """
    one = timedelta(days=1)
    others = PayPeriod.objects.filter(household=period.household)
    if period.pk is None:
        if others.filter(start_date__lte=end, end_date__gte=start).exists():
            raise ValidationError("Pay periods cannot overlap another period in this profile.")
        previous = others.filter(end_date__lt=start).order_by("-end_date").first()
        following = others.filter(start_date__gt=end).order_by("start_date").first()
        if previous and previous.end_date != start - one:
            raise ValidationError(
                f"Start the new period on {previous.end_date + one:%b %-d, %Y}, the day after "
                "the previous period ends, so no days are left out."
            )
        if following and following.start_date != end + one:
            raise ValidationError(
                f"End the new period on {following.start_date - one:%b %-d, %Y}, the day "
                "before the next period starts, so no days are left out."
            )
        return []
    others = others.exclude(pk=period.pk)
    previous = others.filter(start_date__lt=period.start_date).order_by("-start_date").first()
    following = others.filter(start_date__gt=period.start_date).order_by("start_date").first()
    adjust = []
    if previous and previous.end_date != start - one:
        if previous.is_locked:
            raise ValidationError(f"The previous period {previous} is locked; keep this start.")
        if start - one < previous.start_date:
            raise ValidationError(f"That start would leave the previous period {previous} empty.")
        previous.end_date = start - one
        adjust.append(previous)
    if following and following.start_date != end + one:
        if following.is_locked:
            raise ValidationError(f"The next period {following} is locked; keep this end.")
        if end + one > following.end_date:
            raise ValidationError(f"That end would leave the next period {following} empty.")
        following.start_date = end + one
        adjust.append(following)
    return adjust
