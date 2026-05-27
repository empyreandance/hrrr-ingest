"""Parse the wgrib2 ``.idx`` inventory that NOAA ships beside each GRIB2 file.

Each line is ``msg:byte_offset:date:VAR:level:step:`` e.g.::

    1:0:d=2024010100:HGT:50 mb:anl:
    2:731414:d=2024010100:TMP:50 mb:anl:

The inventory gives the byte offset of every GRIB message, which lets us issue
HTTP range requests for just the messages we want — used here to build small
test fixtures, and available as a future bandwidth optimization over the
spec's fetch-whole-file-then-subset path.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class IdxEntry:
    """One GRIB message's inventory record, with its byte range in the file."""

    msg: int
    start: int
    var: str
    level: str
    step: str
    # Exclusive end byte; None for the final message (range "start-" = to EOF).
    end: int | None

    @property
    def http_range(self) -> str:
        """HTTP Range header value for this message, e.g. ``bytes=0-731413``."""
        return f"bytes={self.start}-{'' if self.end is None else self.end - 1}"


def parse_idx(text: str) -> list[IdxEntry]:
    """Parse inventory text into entries with computed byte ranges."""
    rows: list[tuple[int, int, str, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # var/level/step are positional but may themselves contain spaces;
        # split into exactly the leading fields we need.
        parts = line.split(":")
        if len(parts) < 6:
            continue
        msg = int(parts[0])
        start = int(parts[1])
        var, level, step = parts[3], parts[4], parts[5]
        rows.append((msg, start, var, level, step))

    entries: list[IdxEntry] = []
    for i, (msg, start, var, level, step) in enumerate(rows):
        end = rows[i + 1][1] if i + 1 < len(rows) else None
        entries.append(IdxEntry(msg=msg, start=start, var=var, level=level, step=step, end=end))
    return entries


def select(
    entries: Sequence[IdxEntry], predicate: Callable[[IdxEntry], bool]
) -> list[IdxEntry]:
    """Filter entries by an arbitrary predicate (e.g. var/level matching)."""
    return [e for e in entries if predicate(e)]
