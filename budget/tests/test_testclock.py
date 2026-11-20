import datetime as dt

import pytest

from budget import testclock


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(testclock, "_traveller", None)
    yield
    if testclock._traveller is not None:
        testclock._traveller.stop()


@pytest.mark.parametrize(
    ("mode", "today", "debug"),
    [
        (None, "2026-11-18", True),
        ("true", None, True),
        ("true", "2026-11-18", False),
        ("false", "2026-11-18", True),
        ("true", "", True),
    ],
)
def test_fixed_clock_is_inert_without_every_condition(monkeypatch, mode, today, debug):
    for name, value in (("BROWSER_TEST_MODE", mode), ("BROWSER_TEST_TODAY", today)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    # conftest.py already imports time_machine for the unit-test clock, so check the traveller.
    assert testclock.install(debug, "America/New_York") is False
    assert dt.date.today().year >= 2026 and testclock._traveller is None


def test_fixed_clock_freezes_today_when_enabled(monkeypatch):
    monkeypatch.setenv("BROWSER_TEST_MODE", "true")
    monkeypatch.setenv("BROWSER_TEST_TODAY", "2031-03-04")
    assert testclock.install(True, "America/New_York") is True
    from django.utils import timezone

    assert timezone.localdate() == dt.date(2031, 3, 4)
    assert dt.date.today() == dt.date(2031, 3, 4)
