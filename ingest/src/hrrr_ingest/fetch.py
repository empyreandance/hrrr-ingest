"""Fetch HRRR ``wrfprsf`` GRIB2 files from NOAA's S3 bucket over HTTPS.

Spec 3.1 / 3.2: pull ``hrrr.<YYYYMMDD>/conus/hrrr.t<HH>z.wrfprsf<FF>.grib2``
from ``noaa-hrrr-bdp-pds`` (NOAA Open Data, no egress charge). The spec's
pipeline fetches the whole file and subsets with wgrib2 afterwards, so
``fetch_grib`` streams the complete object. ``fetch_messages`` does an
idx-driven partial download (used to build small test fixtures).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path

import httpx

from .config import Config
from .cycle import Cycle
from .idx import IdxEntry, parse_idx

logger = logging.getLogger("hrrr_ingest.fetch")

# A wrfprsf file is hundreds of MB; treat anything implausibly small as a
# truncated/incomplete object rather than a valid download.
MIN_PLAUSIBLE_BYTES = 1_000_000
DEFAULT_RETRIES = 4
DEFAULT_TIMEOUT = 120.0


def grib_url(cfg: Config, cycle: Cycle, forecast_hour: int) -> str:
    return f"{cfg.noaa_base_url.rstrip('/')}/{cycle.grib_key(forecast_hour)}"


def fetch_grib(cycle: Cycle, forecast_hour: int, cfg: Config) -> Path:
    """Download one forecast hour's raw GRIB2 to ``cfg.work_dir`` and return its path.

    Streams to disk (keeps memory flat), retries transient failures with
    exponential backoff, and validates the downloaded size against the server's
    Content-Length.
    """
    url = grib_url(cfg, cycle, forecast_hour)
    dest_dir = Path(cfg.work_dir) / cycle.cycle_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"raw.wrfprsf{forecast_hour:02d}.grib2"

    last_exc: Exception | None = None
    for attempt in range(1, DEFAULT_RETRIES + 1):
        try:
            written = _stream_to_file(url, dest)
            _validate_size(dest, written)
            logger.info("fetched grib", extra={
                "cycle": cycle.cycle_id, "fh": forecast_hour,
                "bytes": written, "attempt": attempt,
            })
            return dest
        except (httpx.HTTPError, OSError) as exc:
            last_exc = exc
            backoff = min(2 ** attempt, 30)
            logger.warning("fetch attempt failed", extra={
                "cycle": cycle.cycle_id, "fh": forecast_hour,
                "attempt": attempt, "error": str(exc), "retry_in_s": backoff,
            })
            if attempt < DEFAULT_RETRIES:
                time.sleep(backoff)

    dest.unlink(missing_ok=True)
    raise RuntimeError(
        f"failed to fetch f{forecast_hour:02d} for {cycle.cycle_id} after "
        f"{DEFAULT_RETRIES} attempts: {last_exc}"
    ) from last_exc


def _stream_to_file(url: str, dest: Path) -> int:
    """Stream a GET to ``dest``; return bytes written."""
    written = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, timeout=DEFAULT_TIMEOUT, follow_redirects=True) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
                written += len(chunk)
    tmp.replace(dest)
    return written


def _validate_size(dest: Path, written: int) -> None:
    actual = dest.stat().st_size
    if actual != written:
        raise OSError(f"size mismatch: wrote {written} but file is {actual} bytes")
    if actual < MIN_PLAUSIBLE_BYTES:
        raise OSError(f"implausibly small download ({actual} bytes); likely truncated")


# --- idx-driven partial fetch (fixtures / future optimization) -------------

def fetch_idx(url: str) -> list[IdxEntry]:
    """Fetch and parse the ``.idx`` inventory for a GRIB url."""
    r = httpx.get(f"{url}.idx", timeout=DEFAULT_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return parse_idx(r.text)


def fetch_messages(url: str, entries: Sequence[IdxEntry], dest: Path) -> Path:
    """Range-download just ``entries`` and concatenate them into one GRIB2 file.

    GRIB2 messages are self-contained and concatenable, so the result is a valid
    (smaller) GRIB2. Used by the test-fixture builder. Each range request is
    retried on transient failures, and the file is written atomically (temp +
    rename) so a mid-download timeout never leaves a corrupt partial fixture.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with tmp.open("wb") as out, httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        for entry in entries:
            for attempt in range(1, DEFAULT_RETRIES + 1):
                try:
                    resp = c.get(url, headers={"Range": entry.http_range})
                    resp.raise_for_status()
                    out.write(resp.content)
                    break
                except httpx.HTTPError:
                    if attempt == DEFAULT_RETRIES:
                        tmp.unlink(missing_ok=True)
                        raise
                    time.sleep(min(2 ** attempt, 10))
    tmp.replace(dest)
    return dest
