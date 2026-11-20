"""Fixed "today" for browser tests, the WebKit audit and the guide screenshots.

Test-only. The clock is frozen (and keeps ticking from noon local time) only when ALL of these
hold: BROWSER_TEST_MODE=true, Django DEBUG is on, and BROWSER_TEST_TODAY=YYYY-MM-DD is set.
Otherwise nothing happens and time_machine (a dev-only dependency) is never imported.
Called from BudgetConfig.ready(), so every Django process (seed commands, runserver) sees it.
"""

import datetime as dt
import os
import zoneinfo

_traveller = None


def install(debug: bool, time_zone: str) -> bool:
    """Start the fixed clock if the explicit test-only conditions hold. Returns True if started."""
    global _traveller
    fixed = os.environ.get("BROWSER_TEST_TODAY", "").strip()
    if not fixed or os.environ.get("BROWSER_TEST_MODE") != "true" or not debug:
        return False
    if _traveller is not None:
        return True
    import time_machine  # dev requirement only

    day = dt.date.fromisoformat(fixed)
    noon = dt.datetime.combine(day, dt.time(12, 0), tzinfo=zoneinfo.ZoneInfo(time_zone))
    _traveller = time_machine.travel(noon, tick=True)
    _traveller.start()
    return True
