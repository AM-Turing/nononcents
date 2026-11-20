import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.urls import reverse

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_settings(extra_env: dict[str, str], code: str = "") -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("POSTGRES_", "DJANGO_SECRET_KEY", "DJANGO_DEBUG"))
    }
    env["DJANGO_SETTINGS_MODULE"] = "nononcents.settings"
    env.update(extra_env)
    script = "import django; django.setup(); from django.conf import settings; " + code
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


POSTGRES_ENV = {"POSTGRES_DB": "x", "POSTGRES_USER": "x", "POSTGRES_PASSWORD": "x"}


@pytest.mark.django_db
def test_health_ok_is_public_and_not_cached(client):
    response = client.get(reverse("health"))

    assert response.status_code == 200
    assert response.content == b"ok"
    assert response["Cache-Control"] == "no-store"
    assert "sessionid" not in response.cookies


def test_health_returns_503_when_database_fails(client):
    with patch("budget.views.connection.cursor", side_effect=OperationalError("secret detail")):
        response = client.get(reverse("health"))

    assert response.status_code == 503
    assert response.content == b"database unavailable"
    assert response["Cache-Control"] == "no-store"


def test_production_requires_secret_key():
    result = run_settings(POSTGRES_ENV)

    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr


def test_production_rejects_dev_fallback_secret():
    result = run_settings({**POSTGRES_ENV, "DJANGO_SECRET_KEY": "unsafe-development-only-key"})

    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr


def test_production_debug_defaults_false():
    result = run_settings(
        {**POSTGRES_ENV, "DJANGO_SECRET_KEY": "a-real-secret"}, "print(settings.DEBUG)"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_development_settings_work_without_env():
    result = run_settings({}, "print(settings.DEBUG)")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
