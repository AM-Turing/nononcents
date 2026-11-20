"""Activity log: sign-ins, failed sign-ins, filters and plain-language rows."""

import ipaddress
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from budget import activity_log
from budget.models import (
    Account,
    AuditEvent,
    Category,
    Expense,
    Household,
    HouseholdMembership,
)
from budget.services import create_expense


@pytest.fixture
def setup(db):
    home = Household.objects.create(name="Test Household")
    other = Household.objects.create(name="Other Budget")
    staff = get_user_model().objects.create_user(
        "admin", password="a-secure-password-123", is_staff=True
    )
    HouseholdMembership.objects.create(household=home, user=staff)
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    return {
        "home": home,
        "other": other,
        "staff": staff,
        "spending": spending,
        "groceries": groceries,
    }


def sign_in(client, setup, password="a-secure-password-123", profile=None, username="admin"):
    return client.post(
        reverse("login"),
        {
            "profile": (profile or setup["home"]).pk,
            "username": username,
            "password": password,
        },
        REMOTE_ADDR="198.51.100.7",
    )


def test_successful_sign_in_is_recorded(client, setup):
    response = sign_in(client, setup)
    assert response.status_code == 302
    event = AuditEvent.objects.get(action=AuditEvent.Action.LOGIN)
    assert event.actor == setup["staff"] and event.household == setup["home"]
    assert event.changes == {"profile": "Test Household", "ip": "198.51.100.7"}


@pytest.mark.parametrize(
    ("username", "password", "profile_key", "reason"),
    [
        ("admin", "wrong-password", "home", "Wrong username or password"),
        ("admin", "a-secure-password-123", "other", "No access to the selected profile"),
    ],
)
def test_failed_sign_in_records_username_and_reason_never_password(
    client, setup, username, password, profile_key, reason
):
    response = sign_in(
        client, setup, password=password, profile=setup[profile_key], username=username
    )
    assert response.status_code == 200
    event = AuditEvent.objects.get(action=AuditEvent.Action.LOGIN_FAILED)
    assert event.household == setup[profile_key]
    assert event.changes["username"] == username
    assert event.changes["reason"] == reason
    assert password not in str(event.changes)


def test_failed_sign_in_with_unknown_username_is_recorded_as_unknown_user(client, setup):
    typed = "my-Secret-pass-77"  # people type passwords into the username box
    sign_in(client, setup, password="whatever-123", username=typed)
    event = AuditEvent.objects.get(action=AuditEvent.Action.LOGIN_FAILED)
    assert event.changes["username"] == ""
    assert event.changes["unknown_user"] is True
    assert typed not in str(event.changes)
    row = activity_log.activity_rows(setup["home"])[0]
    assert row["what"] == "Unknown user for Test Household"
    assert typed not in str(row)


@override_settings(TRUSTED_PROXY_NETWORKS=[ipaddress.ip_network("172.20.0.0/16")])
def test_address_comes_from_the_proxy_only_when_trusted(client, setup):
    client.post(
        reverse("login"),
        {"profile": setup["home"].pk, "username": "admin", "password": "nope-nope-123"},
        REMOTE_ADDR="172.20.0.1",
        HTTP_X_REAL_IP="203.0.113.9",
    )
    client.post(
        reverse("login"),
        {"profile": setup["home"].pk, "username": "admin", "password": "nope-nope-123"},
        REMOTE_ADDR="198.51.100.20",
        HTTP_X_REAL_IP="203.0.113.9",
    )
    addresses = sorted(e.changes["ip"] for e in AuditEvent.objects.all())
    assert addresses == ["198.51.100.20", "203.0.113.9"]


def _expense(setup, merchant, amount="12.89"):
    return create_expense(
        household=setup["home"],
        actor=setup["staff"],
        entry_date=date(2026, 11, 3),
        account=setup["spending"],
        category=setup["groceries"],
        amount=Decimal(amount),
        merchant=merchant,
    )


def test_rows_say_what_in_plain_language(setup):
    expense = _expense(setup, "Snackbar")
    rows = activity_log.activity_rows(setup["home"])
    created = next(row for row in rows if row["action"] == "Added")
    assert created["what"] == "Expense $12.89 · Snackbar · Nov 3, 2026 · Spending"
    assert created["who"] == "admin"
    expense.delete()
    rows = activity_log.activity_rows(setup["home"])
    assert rows[0]["what"].endswith("(since deleted)")
    AuditEvent.objects.create(
        household=setup["home"],
        actor=setup["staff"],
        record_type="budget.Expense",
        record_id="999",
        action="delete",
        changes={"date": "2026-11-17", "description": "Grocer", "amount": "28.52"},
    )
    rows = activity_log.activity_rows(setup["home"])
    assert rows[0]["what"] == "Expense $28.52 · Grocer · Nov 17, 2026"
    assert rows[0]["action"] == "Deleted"


def test_one_action_on_many_records_is_one_row(setup):
    expenses = [_expense(setup, "snackbar") for _ in range(10)]
    AuditEvent.objects.all().delete()
    for expense in expenses:
        AuditEvent.objects.create(
            household=setup["home"],
            actor=setup["staff"],
            record_type="budget.Expense",
            record_id=str(expense.pk),
            action="update",
            changes={"merchant": ["snackbar", "Snackbar"]},
        )
    rows = activity_log.activity_rows(setup["home"])
    assert len(rows) == 1
    assert rows[0]["what"] == "10 expenses"
    assert rows[0]["details"] == ["merchant: snackbar → Snackbar"]
    assert rows[0]["action"] == "Changed"


def test_separate_actions_minutes_apart_are_not_grouped(setup):
    expense = _expense(setup, "snackbar")
    AuditEvent.objects.all().delete()
    for minutes in (0, 5):
        event = AuditEvent.objects.create(
            household=setup["home"],
            actor=setup["staff"],
            record_type="budget.Expense",
            record_id=str(expense.pk),
            action="update",
            changes={"merchant": ["snackbar", "Snackbar"]},
        )
        AuditEvent.objects.filter(pk=event.pk).update(
            occurred_at=timezone.now() - timedelta(minutes=minutes)
        )
    assert len(activity_log.activity_rows(setup["home"])) == 2


def test_filters_on_the_admin_page(client, setup):
    _expense(setup, "Snackbar")
    sign_in(client, setup, password="wrong-password")
    sign_in(client, setup)
    AuditEvent.objects.create(
        household=setup["home"],
        actor=None,
        record_type="budget.PayPeriod",
        record_id="99",
        action="update",
        changes={"reason": "deposit landed"},
    )

    def actions(key):
        response = client.get(reverse("application_admin"), {"log": key})
        assert response.status_code == 200
        return [row["action"] for row in response.context["activity"]]

    assert actions("sign_ins") == ["Signed in"]
    assert actions("failed") == ["Sign-in failed"]
    assert actions("transactions") == ["Added"]
    assert actions("other") == ["Changed"]
    assert len(actions("all")) == 4
    assert len(actions("not-a-filter")) == 4
    page = client.get(reverse("application_admin"), {"log": "failed"}).content.decode()
    assert "Username “admin” for Test Household" in page
    assert "Wrong username or password" in page
    assert 'aria-current="true"' in page


def test_other_profiles_activity_is_not_shown(client, setup):
    AuditEvent.objects.create(
        household=setup["other"],
        actor=None,
        record_type="budget.Expense",
        record_id="1",
        action="delete",
        changes={"amount": "5.00"},
    )
    client.force_login(setup["staff"])
    session = client.session
    session["active_household_id"] = setup["home"].pk
    session.save()
    response = client.get(reverse("application_admin"))
    assert response.context["activity"] == []
    assert Expense.objects.count() == 0


def test_automatic_cleanup_of_many_bill_dates_is_one_row(setup):
    for day in (1, 20):
        AuditEvent.objects.create(
            household=setup["home"],
            actor=None,
            record_type="budget.BillOccurrence",
            record_id=str(400 + day),
            action="delete",
            changes={"due_date": f"2027-08-{day:02d}", "reason": "cleanup"},
        )
    rows = activity_log.activity_rows(setup["home"])
    assert len(rows) == 1
    assert rows[0]["what"] == "2 bill due dates"
    assert rows[0]["who"] == "System (automatic)"
    assert rows[0]["details"] == ["reason: cleanup"]


def test_transactions_entered_together_stay_separate(setup):
    _expense(setup, "Snackbar", "5.00")
    _expense(setup, "Grocer", "6.00")
    rows = activity_log.activity_rows(setup["home"], "transactions")
    assert [row["what"].split(" · ")[1] for row in rows] == ["Grocer", "Snackbar"]


def test_deleted_bill_date_says_which_date(setup):
    AuditEvent.objects.create(
        household=setup["home"],
        actor=None,
        record_type="budget.BillOccurrence",
        record_id="421",
        action="delete",
        changes={"due_date": "2027-08-20", "reason": "cleanup"},
    )
    row = activity_log.activity_rows(setup["home"])[0]
    assert row["what"] == "Bill due Aug 20, 2027"
    assert row["details"] == ["due date: Aug 20, 2027", "reason: cleanup"]


def test_balance_correction_sign_stays_with_its_amount(setup):
    from budget.models import BalanceAdjustment

    adjustment = BalanceAdjustment.objects.create(
        household=setup["home"],
        date=date(2026, 6, 11),
        amount=Decimal("143.10"),
        account=setup["spending"],
        is_increase=False,
        reason="Opening balance correction",
    )
    AuditEvent.objects.create(
        household=setup["home"],
        actor=None,
        record_type="budget.BalanceAdjustment",
        record_id=str(adjustment.pk),
        action="create",
        changes={},
    )
    row = activity_log.activity_rows(setup["home"])[0]
    assert row["what"] == "Balance correction −\u2060$143.10 · Spending · Jun 11, 2026"


def test_blocked_sign_in_is_not_recorded(client, setup):
    for _ in range(5):
        sign_in(client, setup, password="wrong-password")
    assert AuditEvent.objects.filter(action=AuditEvent.Action.LOGIN_FAILED).count() == 5
    for _ in range(3):
        response = sign_in(client, setup)  # right password, but the account is locked
        assert response.status_code == 200
        assert "Too many sign-in attempts" in response.content.decode()
    assert AuditEvent.objects.filter(action=AuditEvent.Action.LOGIN_FAILED).count() == 5
    assert not AuditEvent.objects.filter(action=AuditEvent.Action.LOGIN).exists()
