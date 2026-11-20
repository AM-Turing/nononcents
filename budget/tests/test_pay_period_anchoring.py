"""Pay periods start on the day the paycheck lands (Phase 2)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse

from budget.models import (
    Account,
    BillOccurrence,
    Category,
    Household,
    HouseholdMembership,
    Income,
    PayPeriod,
    PaySchedule,
    RecurringBill,
)
from budget.pay_periods import plan_boundaries, sync_pay_period_boundaries
from budget.services import create_income, expected_deposit_date
from budget.views import generate_future_pay_periods

NOMINALS = [date(2026, 10, 15), date(2026, 11, 1), date(2026, 11, 15), date(2026, 12, 1)]


@pytest.fixture
def home():
    return Household.objects.create(name="Anchor test")


@pytest.fixture
def bills_account(home):
    return Account.objects.create(household=home, name="Bills", type=Account.Type.BILLS)


@pytest.fixture
def periods(home):
    """Generated (unanchored) semi-monthly periods, as in production on 2026-11-17."""
    rows = []
    for index, nominal in enumerate(NOMINALS):
        start = expected_deposit_date(nominal)
        following = NOMINALS[index + 1] if index + 1 < len(NOMINALS) else date(2026, 12, 15)
        rows.append(
            PayPeriod.objects.create(
                household=home,
                nominal_payday=nominal,
                expected_deposit_date=start,
                start_date=start,
                end_date=expected_deposit_date(following) - timedelta(days=1),
            )
        )
    return rows


def paycheck(home, account, when, amount="4688.24", **extra):
    return create_income(
        household=home,
        actor=None,
        income_date=when,
        source="Employer",
        amount=Decimal(amount),
        allocations=[(account, Decimal(amount))],
        is_paycheck=True,
        **extra,
    )


def bounds(home):
    return [
        (p.start_date, p.end_date, p.actual_deposit_date)
        for p in PayPeriod.objects.filter(household=home).order_by("start_date")
    ]


@pytest.mark.django_db
def test_early_paychecks_start_the_period_they_fund(home, bills_account, periods):
    assert [p.start_date for p in periods] == [
        date(2026, 10, 14),
        date(2026, 10, 29),
        date(2026, 11, 12),
        date(2026, 11, 30),
    ]
    paycheck(home, bills_account, date(2026, 10, 14))
    paycheck(home, bills_account, date(2026, 10, 28))
    paycheck(home, bills_account, date(2026, 11, 10))

    result = sync_pay_period_boundaries(home)

    assert bounds(home) == [
        (date(2026, 10, 14), date(2026, 10, 27), date(2026, 10, 14)),
        (date(2026, 10, 28), date(2026, 11, 9), date(2026, 10, 28)),
        (date(2026, 11, 10), date(2026, 11, 29), date(2026, 11, 10)),
        (date(2026, 11, 30), date(2026, 12, 13), None),
    ]
    assert not result.kept
    # Each paycheck is dated inside exactly the period it funds.
    for income in Income.objects.filter(household=home, is_paycheck=True):
        containing = PayPeriod.objects.filter(
            household=home, start_date__lte=income.date, end_date__gte=income.date
        )
        assert containing.count() == 1
        assert containing.get().actual_deposit_date == income.date
    # Second run is a no-op.
    assert sync_pay_period_boundaries(home).changes == []


@pytest.mark.django_db
def test_bill_occurrences_follow_the_moved_boundary(home, bills_account, periods):
    category = Category.objects.create(household=home, name="Power", type=Category.Type.BILL)
    bill = RecurringBill.objects.create(
        household=home,
        name="Power",
        default_amount=Decimal("100.00"),
        due_day=11,
        category=category,
        account=bills_account,
        effective_start=date(2026, 1, 1),
    )
    occurrence = BillOccurrence.objects.create(
        bill=bill,
        due_date=date(2026, 11, 11),
        expected_amount=Decimal("100.00"),
        pay_period=periods[1],
    )
    paycheck(home, bills_account, date(2026, 11, 10))
    result = sync_pay_period_boundaries(home)
    occurrence.refresh_from_db()
    assert occurrence.pay_period_id == periods[2].pk
    assert result.occurrences_moved == 1


@pytest.mark.django_db
def test_paycheck_outside_the_window_is_not_attributed(home, bills_account, periods):
    # 19 days after the last nominal payday (Dec 1): no period claims it.
    income = paycheck(home, bills_account, date(2026, 12, 20))
    result = plan_boundaries(home)
    assert result.changes == []
    assert result.unassigned == [income]


@pytest.mark.django_db
def test_locked_periods_never_move(home, bills_account, periods):
    paycheck(home, bills_account, date(2026, 11, 10))  # recorded before the lock
    periods[1].is_locked = True
    periods[1].save()
    result = sync_pay_period_boundaries(home)
    assert bounds(home)[1][:2] == (date(2026, 10, 29), date(2026, 11, 11))
    assert bounds(home)[2][:2] == (date(2026, 11, 12), date(2026, 11, 29))
    assert any("locked" in note for note in result.kept)


@pytest.mark.django_db
def test_removing_or_redating_the_paycheck_moves_the_boundary_back(home, bills_account, periods):
    income = paycheck(home, bills_account, date(2026, 11, 10))
    sync_pay_period_boundaries(home)
    assert bounds(home)[2][0] == date(2026, 11, 10)

    income.date = date(2026, 11, 11)
    income.save()
    sync_pay_period_boundaries(home)
    assert bounds(home)[1][1] == date(2026, 11, 10)
    assert bounds(home)[2][0] == date(2026, 11, 11)

    income.delete()
    sync_pay_period_boundaries(home)
    assert bounds(home)[2] == (date(2026, 11, 12), date(2026, 11, 29), None)
    assert bounds(home)[1][1] == date(2026, 11, 11)


@pytest.mark.django_db
def test_paycheck_cannot_also_be_irregular(home, bills_account, periods):
    with pytest.raises(ValidationError):
        paycheck(home, bills_account, date(2026, 11, 10), irregular=True)


@pytest.fixture
def member(client, home):
    user = get_user_model().objects.create_user("anchor-user", password="safe-password-123")
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    session = client.session
    session["active_household_id"] = home.pk
    session.save()
    return client


def _income_post(client, account, when, **extra):
    page = client.get(reverse("income_add"))
    token = page.context["form"]["form_token"].value()
    data = {
        "form_token": token,
        "date": when.isoformat(),
        "source": "Employer",
        "amount": "4688.72",
        f"allocation_{account.pk}": "4688.72",
        **extra,
    }
    return client.post(reverse("income_add"), data, follow=True)


@pytest.mark.django_db
def test_recording_the_paycheck_moves_the_period_start(member, home, bills_account, periods):
    response = _income_post(member, bills_account, date(2026, 11, 10), is_paycheck="on")
    assert response.status_code == 200
    assert "Pay period now starts Nov 10, 2026" in response.content.decode()
    assert bounds(home)[2][0] == date(2026, 11, 10)


@pytest.mark.django_db
def test_other_income_does_not_move_periods(member, home, bills_account, periods):
    _income_post(member, bills_account, date(2026, 11, 10))
    assert bounds(home)[2][0] == date(2026, 11, 12)


@pytest.mark.django_db
def test_history_edit_and_delete_of_a_paycheck_resync(member, home, bills_account, periods):
    income = paycheck(home, bills_account, date(2026, 11, 10))
    sync_pay_period_boundaries(home)
    delete = member.post(
        reverse("transaction_delete", args=["income", income.pk]),
        {"confirmation": f"Income: {income.source}"},
    )
    assert delete.status_code == 302, delete.content.decode()[:500]
    assert bounds(home)[2][0] == date(2026, 11, 12)


def _period_form(period, **changes):
    data = {
        "nominal_payday": period.nominal_payday.isoformat(),
        "expected_deposit_date": period.expected_deposit_date.isoformat(),
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat(),
    }
    data.update({key: value.isoformat() for key, value in changes.items()})
    return data


@pytest.mark.django_db
def test_manual_edit_moves_the_neighbouring_boundary(member, home, periods):
    response = member.post(
        reverse("pay_period_edit", args=[periods[2].pk]),
        _period_form(periods[2], start_date=date(2026, 11, 11)),
    )
    assert response.status_code == 302
    assert bounds(home)[1][1] == date(2026, 11, 10)
    assert bounds(home)[2][0] == date(2026, 11, 11)


@pytest.mark.django_db
def test_manual_edit_refuses_locked_or_emptied_neighbour(member, home, periods):
    periods[1].is_locked = True
    periods[1].save()
    locked = member.post(
        reverse("pay_period_edit", args=[periods[2].pk]),
        _period_form(periods[2], start_date=date(2026, 11, 11)),
    )
    assert locked.status_code == 200
    assert "is locked" in locked.content.decode()
    emptied = member.post(
        reverse("pay_period_edit", args=[periods[2].pk]),
        _period_form(periods[2], end_date=date(2026, 12, 20)),
    )
    assert emptied.status_code == 200
    assert "empty" in emptied.content.decode()
    assert bounds(home)[3][0] == date(2026, 11, 30)


@pytest.mark.django_db
def test_new_period_must_not_leave_a_gap(member, home, periods):
    gap = member.post(
        reverse("pay_period_add"),
        {
            "nominal_payday": "2026-12-15",
            "expected_deposit_date": "2026-12-14",
            "start_date": "2026-12-16",
            "end_date": "2026-12-30",
        },
    )
    assert gap.status_code == 200
    assert "no days are left out" in gap.content.decode()
    ok = member.post(
        reverse("pay_period_add"),
        {
            "nominal_payday": "2026-12-15",
            "expected_deposit_date": "2026-12-14",
            "start_date": "2026-12-14",
            "end_date": "2026-12-30",
        },
    )
    assert ok.status_code == 302


@pytest.mark.django_db
def test_generator_skips_a_stale_nominal_payday_instead_of_crashing(home):
    PaySchedule.objects.create(household=home, nominal_days=[1, 15])
    # Latest period's nominal payday is months behind its end date.
    PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 6, 15),
        expected_deposit_date=date(2026, 6, 14),
        start_date=date(2026, 6, 14),
        end_date=date(2026, 7, 29),
    )
    created = generate_future_pay_periods(home, date(2026, 8, 20))
    assert [(p.start_date, p.end_date) for p in created] == [
        (date(2026, 7, 30), date(2026, 8, 12)),
        (date(2026, 8, 13), date(2026, 8, 30)),
    ]
    assert created[0].nominal_payday == date(2026, 8, 1)


@pytest.mark.django_db
def test_anchor_command_dry_run_then_apply(home, bills_account, periods):
    from io import StringIO

    from django.core.management import call_command

    for when in (date(2026, 10, 28), date(2026, 11, 10)):
        create_income(
            household=home,
            actor=None,
            income_date=when,
            source="Employer",
            amount=Decimal("4688.24"),
            allocations=[(bills_account, Decimal("4688.24"))],
        )
    create_income(
        household=home,
        actor=None,
        income_date=date(2026, 11, 3),
        source="Side job",
        amount=Decimal("50.00"),
        allocations=[(bills_account, Decimal("50.00"))],
    )
    out = StringIO()
    call_command("anchor_pay_periods", household=home.pk, source=["employer"], stdout=out)
    assert "Incomes to mark as paychecks: 2" in out.getvalue()
    assert "Dry run" in out.getvalue()
    assert not Income.objects.filter(is_paycheck=True).exists()
    assert bounds(home)[1][0] == date(2026, 10, 29)

    call_command(
        "anchor_pay_periods", household=home.pk, source=["Employer"], apply=True, stdout=out
    )
    assert Income.objects.filter(is_paycheck=True).count() == 2
    assert [b[0] for b in bounds(home)] == [
        date(2026, 10, 14),
        date(2026, 10, 28),
        date(2026, 11, 10),
        date(2026, 11, 30),
    ]
