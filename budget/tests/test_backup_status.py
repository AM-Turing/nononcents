import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from budget.backup_status import read_backup_status
from budget.models import Household, HouseholdMembership


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def status_file(settings, tmp_path) -> Path:
    path = tmp_path / "backup-status.json"
    settings.BACKUP_STATUS_FILE = path
    settings.BACKUP_MAX_AGE_HOURS = 26
    return path


def write(path, **overrides):
    now = timezone.now()
    payload = {
        "state": "ok",
        "finished_at": iso(now - timedelta(hours=1)),
        "message": "Backup complete",
        "last_success_at": iso(now - timedelta(hours=1)),
        "dump": "nononcents-1.dump",
        "copies_ok": 1,
        "copies_expected": 1,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload))


def test_ok_recent(status_file):
    write(status_file)
    status = read_backup_status()
    assert status.level == "ok"
    assert status.dump == "nononcents-1.dump"
    assert (status.copies_ok, status.copies_expected) == (1, 1)
    assert status.last_success_at is not None


def test_ok_but_stale(status_file):
    write(status_file, last_success_at=iso(timezone.now() - timedelta(hours=30)))
    status = read_backup_status()
    assert status.level == "error"
    assert status.headline == "No successful backup in the last 26 hours"


def test_failed_with_recent_success(status_file):
    write(status_file, state="failed", message="disk full")
    status = read_backup_status()
    assert status.level == "error"
    assert status.headline == "The last backup failed"
    assert status.detail == "disk full"


def test_never_succeeded(status_file):
    write(status_file, state="failed", last_success_at=None, message="boom")
    status = read_backup_status()
    assert status.level == "error"
    assert status.headline == "No successful backup has been recorded"
    assert status.detail == "boom"


def test_missing_file(status_file):
    status = read_backup_status()
    assert status.level == "unknown"
    assert status.headline == "Backup status is not available"


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[1, 2]",
        '"text"',
        json.dumps({"state": 5, "last_success_at": 7}),
        json.dumps({"state": "ok"}),
        json.dumps({"state": "ok", "last_success_at": "yesterday"}),
        json.dumps({"state": "mystery", "last_success_at": "2026-01-01T00:00:00Z"}),
        "\xff\xfe",
    ],
)
def test_malformed_degrades(status_file, content):
    status_file.write_text(content, encoding="utf-8", errors="surrogateescape")
    status = read_backup_status()
    assert status.level in {"unknown", "error"}
    assert status.level != "ok"


def test_invalid_json_is_unknown(status_file):
    status_file.write_text("{not json")
    assert read_backup_status().level == "unknown"
    status_file.write_text("[]")
    assert read_backup_status().headline == "Backup status could not be read"


def test_wrong_types_and_bad_timestamp_are_unknown_or_error(status_file):
    write(status_file, copies_ok="x", copies_expected=True, dump=5, finished_at=12)
    status = read_backup_status()
    assert status.level == "ok"
    assert status.copies_ok is None and status.copies_expected is None
    assert status.dump is None and status.finished_at is None
    write(status_file, state=["ok"])
    assert read_backup_status().level == "unknown"


def test_unreadable_path_is_unknown(settings, tmp_path):
    settings.BACKUP_STATUS_FILE = tmp_path  # a directory
    assert read_backup_status().level == "unknown"


def test_oversized_file(status_file):
    status_file.write_text(json.dumps({"state": "ok", "message": "x" * 70_000}))
    assert read_backup_status().level == "unknown"


def test_message_truncated(status_file):
    write(status_file, state="failed", message="m" * 1000)
    assert len(read_backup_status().detail) == 300


@pytest.fixture
def users(db):
    household = Household.objects.create()
    model = get_user_model()
    staff = model.objects.create_user(username="admin", password="pw", is_staff=True)
    member = model.objects.create_user(username="member", password="pw")
    for user in (staff, member):
        HouseholdMembership.objects.create(household=household, user=user)
    return staff, member


def client_for(user):
    client = Client()
    client.force_login(user)
    return client


def test_banner_for_staff_on_error(status_file, users):
    write(status_file, state="failed", message="disk full")
    response = client_for(users[0]).get(reverse("dashboard"))
    assert response.status_code == 200
    content = response.content.decode()
    assert "backup-alert" in content
    assert "The last backup failed" in content
    assert "Backup details" in content


def test_banner_hidden_when_ok(status_file, users):
    write(status_file)
    response = client_for(users[0]).get(reverse("dashboard"))
    assert "backup-alert" not in response.content.decode()


def test_banner_hidden_for_non_staff(status_file, users):
    write(status_file, state="failed")
    response = client_for(users[1]).get(reverse("dashboard"))
    assert response.status_code == 200
    assert "backup-alert" not in response.content.decode()


def test_banner_hidden_when_logged_out(status_file, db):
    write(status_file, state="failed")
    response = Client().get(reverse("login"))
    assert response.status_code == 200
    assert "backup-alert" not in response.content.decode()


def test_admin_page_shows_backups_section(status_file, users):
    write(status_file)
    content = client_for(users[0]).get(reverse("application_admin")).content.decode()
    assert "Backups" in content
    assert "Healthy" in content
    assert "1 of 1" in content
    assert "nononcents-1.dump" in content
    assert "backup-alert" not in content
