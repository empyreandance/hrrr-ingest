"""Shared pytest fixtures.

The integration fixture downloads a *small* slice of a real archived HRRR file
via idx range requests (a few surface fields + a few isobaric levels). It is
network-gated: tests requesting it skip automatically when offline or when the
NOAA bucket is unreachable. The fixture file is cached under tests/fixtures/
(gitignored) so repeated runs don't re-download.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# A stable, long-archived cycle so the fixture is reproducible.
FIXTURE_URL = (
    "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/"
    "hrrr.20240101/conus/hrrr.t00z.wrfprsf00.grib2"
)
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mini.grib2"

# Levels kept small to keep the fixture a few MB.
_ISOBARIC_KEEP = {"500 mb", "700 mb", "850 mb"}
_SURFACE_VARS = {
    ("CAPE", "surface"),
    ("CIN", "surface"),
    ("TMP", "2 m above ground"),
    ("DPT", "2 m above ground"),
    ("RH", "2 m above ground"),
    ("UGRD", "10 m above ground"),
    ("VGRD", "10 m above ground"),
    ("MSLMA", "mean sea level"),
    ("PRES", "surface"),  # surface pressure — required by the parcel engine
    ("RETOP", "cloud top"),  # carries the -999 no-echo sentinel (missing-value masking test)
    # extra fields so the built-in (tip-sheet) presets are demoable on the sample
    ("CAPE", "180-0 mb above ground"),
    ("CIN", "180-0 mb above ground"),
    ("CAPE", "255-0 mb above ground"),
    ("HLCY", "1000-0 m above ground"),
    ("HLCY", "3000-0 m above ground"),
    ("VUCSH", "0-1000 m above ground"),
    ("VVCSH", "0-1000 m above ground"),
    ("VUCSH", "0-6000 m above ground"),
    ("VVCSH", "0-6000 m above ground"),
    ("USTM", "0-6000 m above ground"),
    ("VSTM", "0-6000 m above ground"),
    ("HGT", "0C isotherm"),
    ("HGT", "level of adiabatic condensation from sfc"),
    ("HGT", "equilibrium level"),
    ("HGT", "surface"),  # orography (terrain height) — for AGL interpolation
    ("PWAT", "entire atmosphere (considered as a single layer)"),
}
_ISOBARIC_VARS = {"TMP", "DPT", "HGT", "UGRD", "VGRD"}


def _wanted(entry) -> bool:
    if (entry.var, entry.level) in _SURFACE_VARS:
        return True
    return entry.var in _ISOBARIC_VARS and entry.level in _ISOBARIC_KEEP


def _build_fixture() -> None:
    from hrrr_ingest.fetch import fetch_idx, fetch_messages
    from hrrr_ingest.idx import select

    entries = select(fetch_idx(FIXTURE_URL), _wanted)
    if not entries:
        raise RuntimeError("no matching messages found for fixture")
    fetch_messages(FIXTURE_URL, entries, FIXTURE_PATH)


@pytest.fixture(scope="session")
def mini_grib() -> Path:
    """Path to a small real HRRR GRIB2 slice; skips if it can't be obtained."""
    if FIXTURE_PATH.exists() and FIXTURE_PATH.stat().st_size > 0:
        return FIXTURE_PATH
    try:
        _build_fixture()
    except Exception as exc:  # offline / bucket unreachable
        pytest.skip(f"could not build GRIB fixture (network?): {exc}")
    return FIXTURE_PATH
