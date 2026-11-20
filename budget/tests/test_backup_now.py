"""Back up now: the app leaves a request; the host script runs it once, under the backup lock."""

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget import backup_requests
from budget.models import AuditEvent, Household, HouseholdMembership

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup-request.sh"


@pytest.fixture
def staff_client(client, db, tmp_path, settings):
    settings.BACKUP_REQUEST_DIR = tmp_path / "requests"
    home = Household.objects.create(name="Test Household")
    user = get_user_model().objects.create_user("admin", password="pw-123456789", is_staff=True)
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    return client


def test_button_explains_when_not_set_up(staff_client):
    page = staff_client.get(reverse("application_admin")).content.decode()
    assert "Back up now needs a one-time setup on the server" in page
    staff_client.post(reverse("application_admin"), {"intent": "backup_now"})
    assert not AuditEvent.objects.filter(record_type="backup.Request").exists()


def test_request_is_left_once_and_logged(staff_client, settings):
    settings.BACKUP_REQUEST_DIR.mkdir()
    assert "Back up now</button>" in staff_client.get(reverse("application_admin")).content.decode()

    staff_client.post(reverse("application_admin"), {"intent": "backup_now"})
    staff_client.post(reverse("application_admin"), {"intent": "backup_now"})  # double tap

    data = json.loads((settings.BACKUP_REQUEST_DIR / "backup-now.json").read_text())
    assert data["requested_by"] == "admin"
    assert AuditEvent.objects.filter(record_type="backup.Request").count() == 1
    page = staff_client.get(reverse("application_admin")).content.decode()
    assert "Backup requested by admin" in page
    assert "Back up now</button>" not in page
    log = staff_client.get(reverse("application_admin"), {"log": "other"}).context["activity"]
    assert log[0]["what"] == "Backup requested from Admin"


def run_script(tmp_path, *args, lock=None):
    marker = tmp_path / "ran"
    fake = tmp_path / "fake-backup.sh"
    fake.write_text(f"#!/bin/sh\necho ran >> {marker}\n")
    fake.chmod(0o755)
    env = {
        **os.environ,
        "BACKUP_REQUEST_DIR": str(tmp_path / "requests"),
        "BACKUP_LOCK_FILE": str(lock or tmp_path / "lock"),
        "BACKUP_COMMAND": str(fake),
    }
    result = subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True)
    runs = marker.read_text().count("ran") if marker.exists() else 0
    return result, runs


def test_script_setup_makes_a_drop_only_folder(tmp_path):
    result, runs = run_script(tmp_path, "--setup")
    assert result.returncode == 0 and runs == 0
    assert oct((tmp_path / "requests").stat().st_mode & 0o7777) == "0o1733"


def test_script_does_nothing_without_a_request(tmp_path):
    run_script(tmp_path, "--setup")
    result, runs = run_script(tmp_path)
    assert result.returncode == 0 and runs == 0


def test_script_runs_one_backup_per_request_and_names_who_asked(tmp_path):
    run_script(tmp_path, "--setup")
    (tmp_path / "requests" / "backup-now.json").write_text(
        '{"requested_by": "robin", "requested_at": "2026-11-19T03:00:00+00:00"}'
    )
    result, runs = run_script(tmp_path)
    assert runs == 1
    assert "Backup requested in the app by robin" in result.stdout
    assert not (tmp_path / "requests" / "backup-now.json").exists()
    _, runs_again = run_script(tmp_path)
    assert runs_again == 1


def test_script_waits_while_another_backup_holds_the_lock(tmp_path):
    run_script(tmp_path, "--setup")
    request = tmp_path / "requests" / "backup-now.json"
    request.write_text('{"requested_by": "admin", "requested_at": "2026-11-19T03:00:00+00:00"}')
    lock = tmp_path / "lock"
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        _, runs = run_script(tmp_path, lock=lock)
    assert runs == 0 and request.exists()


def test_request_helpers_refuse_when_folder_missing(settings, tmp_path):
    settings.BACKUP_REQUEST_DIR = tmp_path / "missing"
    assert not backup_requests.available()
    assert backup_requests.request_backup("admin") is False
