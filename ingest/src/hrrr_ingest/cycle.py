"""HRRR cycle math: which cycle to ingest and which forecast hours it has.

Spec 3.1:
  * Cycles run every hour, 00-23Z.
  * Standard cycles (20/day) provide f00-f18 (19 forecast hours).
  * Extended cycles (00, 06, 12, 18Z) provide f00-f48 (49 forecast hours).

Spec 3.2: cron fires at cycle init + 65 minutes, by which time the data is
typically available. The orchestrator derives the target cycle from the wall
clock; reruns pass an explicit cycle id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Cycles initialized at these UTC hours run out to f48; all others stop at f18.
EXTENDED_INIT_HOURS = frozenset({0, 6, 12, 18})
STANDARD_MAX_FH = 18
EXTENDED_MAX_FH = 48

# Minutes after init that HRRR data is reliably available (spec 3.2).
DEFAULT_AVAILABILITY_LAG_MINUTES = 65

_CYCLE_ID_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})$")


@dataclass(frozen=True)
class Cycle:
    """A single HRRR cycle, identified by its initialization time (UTC)."""

    init: datetime

    def __post_init__(self) -> None:
        if self.init.tzinfo is None:
            raise ValueError("Cycle.init must be timezone-aware (UTC)")
        if self.init.utcoffset() != timedelta(0):
            raise ValueError("Cycle.init must be in UTC")
        if self.init.minute or self.init.second or self.init.microsecond:
            raise ValueError("Cycle.init must be truncated to the top of the hour")

    @property
    def hour(self) -> int:
        return self.init.hour

    @property
    def date_str(self) -> str:
        """YYYYMMDD, as used in the NOAA key ``hrrr.<YYYYMMDD>/``."""
        return self.init.strftime("%Y%m%d")

    @property
    def cycle_id(self) -> str:
        """YYYYMMDDHH, used as the R2 cycle path segment ``cycles/<id>/``."""
        return self.init.strftime("%Y%m%d%H")

    @property
    def is_extended(self) -> bool:
        return self.hour in EXTENDED_INIT_HOURS

    @property
    def max_forecast_hour(self) -> int:
        return EXTENDED_MAX_FH if self.is_extended else STANDARD_MAX_FH

    @property
    def forecast_hours(self) -> list[int]:
        return list(range(0, self.max_forecast_hour + 1))

    def grib_key(self, forecast_hour: int) -> str:
        """NOAA S3 object key for a forecast hour (spec 3.1).

        e.g. ``hrrr.20260524/conus/hrrr.t00z.wrfprsf06.grib2``
        """
        if forecast_hour not in self.forecast_hours:
            raise ValueError(
                f"f{forecast_hour:02d} is out of range for the "
                f"{'extended' if self.is_extended else 'standard'} "
                f"{self.cycle_id} cycle (max f{self.max_forecast_hour:02d})"
            )
        return (
            f"hrrr.{self.date_str}/conus/"
            f"hrrr.t{self.hour:02d}z.wrfprsf{forecast_hour:02d}.grib2"
        )

    def __str__(self) -> str:
        kind = "extended" if self.is_extended else "standard"
        return f"{self.cycle_id} ({kind}, f00-f{self.max_forecast_hour:02d})"


def current_cycle(
    now: datetime | None = None,
    *,
    lag_minutes: int = DEFAULT_AVAILABILITY_LAG_MINUTES,
) -> Cycle:
    """Most recent cycle expected to be available at ``now``.

    Subtracts the availability lag and truncates to the top of the hour. With
    the default 65-minute lag this returns the cycle that initialized in the
    previous hour (matching the cron at init+65min), and tolerates a cron that
    fires a little late without skipping to the wrong cycle.
    """
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")

    init = (now - timedelta(minutes=lag_minutes)).replace(
        minute=0, second=0, microsecond=0
    )
    return Cycle(init=init.astimezone(UTC))


def parse_cycle_id(cycle_id: str) -> Cycle:
    """Parse a ``YYYYMMDDHH`` cycle id (for reruns / CLI override)."""
    match = _CYCLE_ID_RE.match(cycle_id.strip())
    if not match:
        raise ValueError(f"invalid cycle id {cycle_id!r}; expected YYYYMMDDHH")
    year, month, day, hour = (int(g) for g in match.groups())
    return Cycle(init=datetime(year, month, day, hour, tzinfo=UTC))
