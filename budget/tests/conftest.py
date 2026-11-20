import datetime as dt
import os
import zoneinfo

import pytest
import time_machine
from django.conf import settings as django_settings

from budget.forms import UnusualDateGuard

# Unit tests run on a fixed "today" so figures never depend on the real date (several test
# modules read the date at import time, so the clock is frozen here, before they are imported).
# TEST_TODAY=YYYY-MM-DD picks another day (to check date independence); TEST_TODAY=real turns
# the freeze off. The clock keeps ticking from noon local time.
_fixed = os.environ.get("TEST_TODAY", "2026-11-19")
if _fixed != "real":
    _noon = dt.datetime.combine(
        dt.date.fromisoformat(_fixed),
        dt.time(12, 0),
        tzinfo=zoneinfo.ZoneInfo(django_settings.TIME_ZONE),
    )
    time_machine.travel(_noon, tick=True).start()


@pytest.fixture(autouse=True)
def _relax_unusual_date_guard(request, monkeypatch):
    """Most fixtures use fixed historical dates; only the integrity tests exercise the guard."""
    if request.module.__name__.endswith("test_ledger_integrity"):
        return
    monkeypatch.setattr(UnusualDateGuard, "_check_unusual_date", lambda self, value: None)


@pytest.fixture(autouse=True)
def _plain_static_files(settings):
    """Production uses hashed static names from collectstatic; tests render without that step."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
