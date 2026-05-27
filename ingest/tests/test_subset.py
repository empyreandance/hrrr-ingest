"""Tests for the subset stage (pattern + dispatch)."""

from __future__ import annotations

import re

from hrrr_ingest import subset


def test_exclude_pattern_covers_real_clmr_name():
    # Spec 4.5 says CLWMR, but the real inventory uses CLMR; both must match.
    pat = re.compile(subset.EXCLUDE_PATTERN)
    for name in ("CLMR", "CLWMR", "CIMIXR", "RWMR", "SNMR", "GRLE"):
        assert pat.search(f":{name}:"), name


def test_exclude_pattern_does_not_match_wanted_fields():
    pat = re.compile(subset.EXCLUDE_PATTERN)
    for name in ("TMP", "CAPE", "REFC", "UGRD", "HGT"):
        assert not pat.search(f":{name}:"), name


def test_cfgrib_fallback_returns_raw_path_unchanged(monkeypatch, tmp_path):
    raw = tmp_path / "raw.wrfprsf00.grib2"
    raw.write_bytes(b"\x00")
    monkeypatch.setattr(subset, "wgrib2_available", lambda: False)
    out = subset.subset_grib(raw, cfg=None)  # cfg unused on this path
    assert out == raw and out.exists()
