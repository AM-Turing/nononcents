from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget.models import (
    Account,
    BillOccurrence,
    Category,
    Household,
    HouseholdMembership,
    PayPeriod,
    PayPeriodBudget,
    PaySchedule,
    RecurringBill,
)
from budget.services import expected_deposit_date
from budget.views import next_nominal_payday


@pytest.fixture
def period_setup(client):
    user = get_user_model().objects.create_user("profile-member", password="safe-password-123")
    home = Household.objects.create(name="Member profile")
    other = Household.objects.create(name="Other profile")
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    session = client.session
    session["active_household_id"] = home.pk
    session.save()
    return client, user, home, other


def period_data(**overrides):
    data = {
        "nominal_payday": "2026-08-15",
        "expected_deposit_date": "2026-08-14",
        "actual_deposit_date": "",
        "start_date": "2026-08-14",
        "end_date": "2026-08-31",
        "is_exceptional": "",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_profile_member_can_add_period_and_bill_occurrences_are_assigned(period_setup):
    client, user, home, _ = period_setup
    account = Account.objects.create(household=home, name="Bills", type=Account.Type.BILLS)
    category = Category.objects.create(household=home, name="Utilities", type=Category.Type.BILL)
    bill = RecurringBill.objects.create(
        household=home,
        name="Power",
        default_amount="100.00",
        due_day=20,
        category=category,
        account=account,
        effective_start=date(2026, 1, 1),
    )
    occurrence = BillOccurrence.objects.create(
        bill=bill, due_date=date(2026, 8, 20), expected_amount="100.00"
    )

    response = client.post(reverse("pay_period_add"), period_data())

    assert response.status_code == 302
    period = PayPeriod.objects.get(household=home)
    assert PayPeriodBudget.objects.filter(pay_period=period).exists()
    occurrence.refresh_from_db()
    assert occurrence.pay_period == period


@pytest.mark.django_db
def test_period_list_and_edit_are_scoped_to_active_profile(period_setup):
    client, _, home, other = period_setup
    own = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 13),
    )
    foreign = PayPeriod.objects.create(
        household=other,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 13),
    )

    response = client.get(reverse("pay_periods"))

    assert response.status_code == 200
    assert own in response.context["periods"]
    assert foreign not in response.context["periods"]
    assert client.get(reverse("pay_period_edit", args=[foreign.pk])).status_code == 404


@pytest.mark.django_db
def test_overlapping_period_is_rejected(period_setup):
    client, _, home, _ = period_setup
    PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 15),
    )

    response = client.post(reverse("pay_period_add"), period_data(start_date="2026-08-15"))

    assert response.status_code == 200
    assert "cannot overlap" in response.content.decode()
    assert PayPeriod.objects.filter(household=home).count() == 1


@pytest.mark.django_db
def test_locked_period_cannot_be_edited_and_only_staff_can_reopen(period_setup):
    client, user, home, _ = period_setup
    period = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 13),
        is_locked=True,
    )

    assert client.get(reverse("pay_period_edit", args=[period.pk])).status_code == 302
    assert client.post(reverse("pay_period_lock", args=[period.pk])).status_code == 403
    period.refresh_from_db()
    assert period.is_locked

    user.is_staff = True
    user.save(update_fields=["is_staff"])
    assert client.post(reverse("pay_period_lock", args=[period.pk])).status_code == 302
    period.refresh_from_db()
    assert not period.is_locked


@pytest.mark.django_db
def test_member_can_generate_semi_monthly_periods_through_selected_date(period_setup):
    client, _, home, _ = period_setup
    PaySchedule.objects.create(household=home, nominal_days=[1, 15])
    current = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
    )

    response = client.post(
        reverse("pay_periods"),
        {"frequency": "semimonthly", "generation_through": "2026-09-07"},
    )

    assert response.status_code == 302
    periods = list(PayPeriod.objects.filter(household=home).order_by("start_date"))
    # Phase 2: each new period ends the day before the next expected deposit
    # (Sep 1 is a Tuesday -> Aug 31; Sep 15 is a Tuesday -> Sep 14).
    assert [(period.start_date, period.end_date) for period in periods] == [
        (date(2026, 8, 1), date(2026, 8, 14)),
        (date(2026, 8, 15), date(2026, 8, 30)),
        (date(2026, 8, 31), date(2026, 9, 13)),
    ]
    assert periods[1].nominal_payday == date(2026, 8, 15)
    assert periods[1].expected_deposit_date == date(2026, 8, 13)
    assert PayPeriodBudget.objects.filter(pay_period__in=periods[1:]).count() == 2
    assert PayPeriodBudget.objects.filter(pay_period=current).count() == 0


@pytest.mark.django_db
def test_generator_requires_current_period_and_future_through_date(period_setup):
    client, _, home, _ = period_setup
    PaySchedule.objects.create(household=home, nominal_days=[1, 15])

    missing = client.post(
        reverse("pay_periods"),
        {"frequency": "semimonthly", "generation_through": "2027-12-07"},
    )

    assert missing.status_code == 200
    assert "Add the current pay period" in missing.content.decode()

    PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
    )
    past = client.post(
        reverse("pay_periods"),
        {"frequency": "semimonthly", "generation_through": "2026-08-07"},
    )
    assert past.status_code == 200
    assert "after the latest period ends" in past.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("frequency", "first_nominal", "first_end", "expected_days"),
    (
        # Latest period is Aug 1-14, nominal Sat Aug 1. Weekly cadence Aug 8 and Aug 15 both
        # deposit (Thu Aug 6, Thu Aug 13) before the new start Aug 15, so the first new
        # period is nominal Aug 15; the next nominal is Sat Aug 22 (+7), deposit Thu Aug 20,
        # so it ends Aug 19.
        ("weekly", date(2026, 8, 15), date(2026, 8, 19), 7),
        # Biweekly: Aug 1 + 14 = Aug 15 (deposit Thu Aug 13 <= start Aug 15); next Aug 29,
        # deposit Thu Aug 27, so it ends Aug 26.
        ("biweekly", date(2026, 8, 15), date(2026, 8, 26), 14),
        # Monthly on the 1st: Aug 1 -> Sep 1 deposits Aug 31, after the start Aug 15, so the
        # first period is a stub (nominal = start Aug 15) ending the day before Sep 1's
        # deposit: Aug 30. The next nominal payday is Sep 1, 17 days later.
        ("monthly", date(2026, 8, 15), date(2026, 8, 30), 17),
    ),
)
def test_profile_can_generate_supported_interval_frequencies(
    period_setup, frequency, first_nominal, first_end, expected_days
):
    client, _, home, _ = period_setup
    schedule = PaySchedule.objects.create(household=home, nominal_days=[1, 15])
    current = PayPeriod.objects.create(
        household=home,
        nominal_payday=date(2026, 8, 1),
        expected_deposit_date=date(2026, 8, 1),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
    )

    response = client.post(
        reverse("pay_periods"),
        {"frequency": frequency, "generation_through": "2026-08-15"},
    )

    assert response.status_code == 302
    schedule.refresh_from_db()
    assert schedule.frequency == frequency
    generated = (
        PayPeriod.objects.filter(household=home).exclude(pk=current.pk).earliest("start_date")
    )
    assert generated.start_date == current.end_date + timedelta(days=1)  # Aug 14 + 1 = Aug 15
    assert generated.nominal_payday == first_nominal
    assert generated.end_date == first_end
    next_nominal = next_nominal_payday(generated.nominal_payday, frequency, schedule.nominal_days)
    assert (next_nominal - generated.nominal_payday).days == expected_days


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("frequency", "first_nominal", "batches"),
    (
        # Weekly, Fridays: latest nominal Fri Aug 7 (deposit Thu Aug 6), period Aug 6-12.
        # Batch through Aug 26 starts Thu Aug 13 (nominal Fri Aug 14) and Thu Aug 20 (Aug 21);
        # a second batch through Sep 9 starts Aug 27 (Aug 28) and Sep 3 (Sep 4). Always Fridays.
        (
            "weekly",
            date(2026, 8, 7),
            [
                ("2026-08-26", [date(2026, 8, 14), date(2026, 8, 21)]),
                ("2026-09-09", [date(2026, 8, 28), date(2026, 9, 4)]),
            ],
        ),
        # Biweekly: Fri Aug 7 + 14 = Aug 21, then Sep 4, Sep 18 (one batch each).
        (
            "biweekly",
            date(2026, 8, 7),
            [
                ("2026-08-20", [date(2026, 8, 21)]),
                ("2026-09-03", [date(2026, 9, 4)]),
                ("2026-09-17", [date(2026, 9, 18)]),
            ],
        ),
        # Monthly on the 10th: Aug 10 (Mon, deposit Aug 9) -> Sep 10, Oct 10, Nov 10.
        (
            "monthly",
            date(2026, 8, 10),
            [
                ("2026-09-09", [date(2026, 9, 10)]),
                ("2026-10-09", [date(2026, 10, 10)]),
                ("2026-11-09", [date(2026, 11, 10)]),
            ],
        ),
    ),
)
def test_payday_does_not_drift_across_generated_batches(
    period_setup, frequency, first_nominal, batches
):
    client, _, home, _ = period_setup
    deposit = expected_deposit_date(first_nominal)
    # Regular first period: it runs until the day before the next payday's expected deposit.
    PayPeriod.objects.create(
        household=home,
        nominal_payday=first_nominal,
        expected_deposit_date=deposit,
        start_date=deposit,
        end_date=(
            deposit + timedelta(days=6)
            if frequency == "weekly"
            else deposit + timedelta(days=13)
            if frequency == "biweekly"
            else date(2026, 9, 8)
        ),
    )
    for through, expected in batches:
        response = client.post(
            reverse("pay_periods"), {"frequency": frequency, "generation_through": through}
        )
        assert response.status_code == 302
        known = set(
            PayPeriod.objects.filter(household=home).values_list("nominal_payday", flat=True)
        )
        assert set(expected) <= known, (frequency, through, sorted(known))
    nominal_paydays = list(
        PayPeriod.objects.filter(household=home).values_list("nominal_payday", flat=True)
    )
    if frequency == "monthly":
        assert {day.day for day in nominal_paydays} == {10}
    else:
        # Aug 7, 2026 is a Friday (weekday 4); every nominal payday must stay a Friday.
        assert {day.weekday() for day in nominal_paydays} == {4}
