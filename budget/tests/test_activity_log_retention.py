"""Activity-log retention: old entries are removed automatically, once a day."""

import datetime as dt
import io
import zoneinfo

import pytest
import time_machine
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from budget import activity_log
from budget.models import (
    Account,
    ApplicationSetting,
    AuditEvent,
    Household,
    HouseholdMembership,
    LoginFailure,
)

PASSWORD = "a-secure-password-123"
NOW = dt.datetime(
    2026, 11, 20, 12, 0, tzinfo=zoneinfo.ZoneInfo(django_settings.TIME_ZONE)
).astimezone(dt.UTC)  # UTC, so "60 days" is exact across the November clock change


@pytest.fixture
def home(db):
    home = Household.objects.create(name="Demo Household")
    user = get_user_model().objects.create_user("admin", password=PASSWORD, is_staff=True)
    HouseholdMembership.objects.create(household=home, user=user)
    return home


@pytest.fixture(autouse=True)
def frozen():
    with time_machine.travel(NOW, tick=False) as traveller:
        yield traveller


def event_at(home, when, record_id="1"):
    event = AuditEvent.objects.create(
        household=home, record_type="budget.Account", record_id=record_id, action="update"
    )
    AuditEvent.objects.filter(pk=event.pk).update(occurred_at=when)
    return event.pk


def sign_in(client, home, capture):
    with capture(execute=True):
        return client.post(
            reverse("login"),
            {"profile": home.pk, "username": "admin", "password": PASSWORD},
        )


def sign_in_events(home):
    return AuditEvent.objects.filter(household=home, action="login").count()


@override_settings(ACTIVITY_LOG_RETENTION_DAYS=60)
def test_old_rows_deleted_and_recent_kept_with_exact_boundary(home):
    cutoff = NOW - dt.timedelta(days=60)
    old = event_at(home, cutoff - dt.timedelta(seconds=1), "1")
    edge = event_at(home, cutoff, "2")
    recent = event_at(home, cutoff + dt.timedelta(seconds=1), "3")
    assert activity_log.prune_activity_log() == 1
    assert set(AuditEvent.objects.values_list("pk", flat=True)) == {edge, recent}
    assert old not in AuditEvent.objects.values_list("pk", flat=True)


@override_settings(ACTIVITY_LOG_RETENTION_DAYS=0)
def test_zero_keeps_everything(home):
    event_at(home, NOW - dt.timedelta(days=5000))
    assert activity_log.prune_activity_log() == 0
    assert AuditEvent.objects.count() == 1


@override_settings(ACTIVITY_LOG_RETENTION_DAYS=60)
def test_other_models_are_untouched(home):
    event_at(home, NOW - dt.timedelta(days=500))
    account = Account.objects.create(household=home, name="Spending", type="spending")
    setting = ApplicationSetting.objects.create(household=home, key="x", value=1)
    LoginFailure.objects.create(username_key="admin", ip="198.51.100.7")
    activity_log.prune_activity_log()
    assert AuditEvent.objects.count() == 0
    assert Account.objects.filter(pk=account.pk).exists()
    assert ApplicationSetting.objects.filter(pk=setting.pk).exists()
    assert LoginFailure.objects.count() == 1
    assert Household.objects.filter(pk=home.pk).exists()


@override_settings(ACTIVITY_LOG_RETENTION_DAYS=60)
def test_sign_in_prunes_once_a_day(client, home, django_capture_on_commit_callbacks, frozen):
    old = event_at(home, NOW - dt.timedelta(days=90), "1")
    assert sign_in(client, home, django_capture_on_commit_callbacks).status_code == 302
    assert not AuditEvent.objects.filter(pk=old).exists()
    assert sign_in_events(home) == 1

    # Same day: an entry that has become old is not pruned again.
    stale = event_at(home, NOW - dt.timedelta(days=90), "2")
    client.logout()
    sign_in(client, home, django_capture_on_commit_callbacks)
    assert AuditEvent.objects.filter(pk=stale).exists()

    # Next day: pruned again.
    frozen.move_to(NOW + dt.timedelta(days=1))
    client.logout()
    sign_in(client, home, django_capture_on_commit_callbacks)
    assert not AuditEvent.objects.filter(pk=stale).exists()


@override_settings(ACTIVITY_LOG_RETENTION_DAYS=0)
def test_sign_in_with_zero_days_keeps_rows(client, home, django_capture_on_commit_callbacks):
    old = event_at(home, NOW - dt.timedelta(days=900))
    sign_in(client, home, django_capture_on_commit_callbacks)
    assert AuditEvent.objects.filter(pk=old).exists()
    # Turning retention on later prunes at the next sign-in (0 did not use up the day).
    client.logout()
    with override_settings(ACTIVITY_LOG_RETENTION_DAYS=60):
        sign_in(client, home, django_capture_on_commit_callbacks)
    assert not AuditEvent.objects.filter(pk=old).exists()


@override_settings(ACTIVITY_LOG_RETENTION_DAYS=60)
def test_failing_prune_does_not_break_sign_in(
    client, home, django_capture_on_commit_callbacks, monkeypatch
):
    def boom(*args, **kwargs):
        raise RuntimeError("prune failed")

    monkeypatch.setattr(activity_log, "prune_activity_log", boom, raising=False)
    old = event_at(home, NOW - dt.timedelta(days=90))
    response = sign_in(client, home, django_capture_on_commit_callbacks)
    assert response.status_code == 302
    assert sign_in_events(home) == 1
    assert AuditEvent.objects.filter(pk=old).exists()
    # The failed attempt is not marked as done, so the next sign-in tries again.
    monkeypatch.undo()
    client.logout()
    sign_in(client, home, django_capture_on_commit_callbacks)
    assert not AuditEvent.objects.filter(pk=old).exists()


@override_settings(ACTIVITY_LOG_RETENTION_DAYS=60)
def test_failed_sign_in_for_a_profile_also_prunes(client, home, django_capture_on_commit_callbacks):
    old = event_at(home, NOW - dt.timedelta(days=90))
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("login"), {"profile": home.pk, "username": "admin", "password": "wrong"}
        )
    assert not AuditEvent.objects.filter(pk=old).exists()


@override_settings(ACTIVITY_LOG_RETENTION_DAYS=60)
def test_command_removes_and_dry_run_deletes_nothing(home):
    event_at(home, NOW - dt.timedelta(days=90), "1")
    event_at(home, NOW - dt.timedelta(days=1), "2")
    out = io.StringIO()
    call_command("prune_activity_log", "--dry-run", stdout=out)
    assert "1" in out.getvalue()
    assert AuditEvent.objects.count() == 2
    call_command("prune_activity_log", stdout=io.StringIO())
    assert AuditEvent.objects.count() == 1


@pytest.mark.parametrize(
    ("days", "text"),
    [
        (60, "kept for 60 days"),
        (30, "kept for 30 days"),
        (0, "kept until they are removed"),
    ],
)
def test_admin_caption_states_retention(client, home, days, text):
    client.login(username="admin", password=PASSWORD)
    session = client.session
    session["active_household_id"] = home.pk
    session.save()
    with override_settings(ACTIVITY_LOG_RETENTION_DAYS=days):
        response = client.get(reverse("application_admin"))
    assert response.status_code == 200
    assert text in response.content.decode()
