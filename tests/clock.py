"""One fixed day for a whole test run. The app's dates are UTC, and every test runs at noon UTC on
this day (conftest.py), so no test's result depends on the time of day or on a run crossing
midnight. Noon also keeps the local date the same as the UTC date in any timezone but the far
Pacific."""

from datetime import datetime, timezone

NOON = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
TODAY = NOON.date()
