"""Tests for fetch URL construction and download validation."""

from __future__ import annotations

import httpx
import pytest

from hrrr_ingest import fetch
from hrrr_ingest.config import Config
from hrrr_ingest.cycle import parse_cycle_id


def _http_404():
    req = httpx.Request("GET", "https://example.invalid/f.grib2")
    return httpx.HTTPStatusError(
        "404", request=req, response=httpx.Response(404, request=req)
    )


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    return Config.from_env(load_dotenv_file=False, require_r2=False)


def test_grib_url(cfg):
    cycle = parse_cycle_id("2026052400")
    url = fetch.grib_url(cfg, cycle, 6)
    assert url == (
        "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/"
        "hrrr.20260524/conus/hrrr.t00z.wrfprsf06.grib2"
    )


def test_validate_size_rejects_truncated(tmp_path):
    small = tmp_path / "x.grib2"
    small.write_bytes(b"\x00" * 1024)
    with pytest.raises(OSError, match="implausibly small"):
        fetch._validate_size(small, 1024)


def test_validate_size_rejects_mismatch(tmp_path):
    f = tmp_path / "y.grib2"
    f.write_bytes(b"\x00" * 2_000_000)
    with pytest.raises(OSError, match="size mismatch"):
        fetch._validate_size(f, 999)


def test_validate_size_accepts_plausible(tmp_path):
    f = tmp_path / "z.grib2"
    f.write_bytes(b"\x00" * 2_000_000)
    fetch._validate_size(f, 2_000_000)  # no raise


def test_fetch_grib_polls_until_published(cfg, monkeypatch):
    """A 404 (not yet published) is polled, not failed: succeeds once it lands."""
    cycle = parse_cycle_id("2026052400")
    monkeypatch.setattr(fetch.time, "sleep", lambda _s: None)  # don't really wait
    calls = {"n": 0}

    def fake_stream(_url, dest):
        calls["n"] += 1
        if calls["n"] < 3:          # 404 twice, then the file publishes
            raise _http_404()
        dest.write_bytes(b"\x00" * 2_000_000)
        return 2_000_000

    monkeypatch.setattr(fetch, "_stream_to_file", fake_stream)
    out = fetch.fetch_grib(cycle, 6, cfg)
    assert out.exists()
    assert calls["n"] == 3          # polled past the two 404s


def test_fetch_grib_gives_up_after_deadline(monkeypatch, tmp_path):
    """A forecast hour that never publishes fails once the wait deadline passes."""
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("FETCH_MAX_WAIT_SECONDS", "0")  # no patience
    cfg = Config.from_env(load_dotenv_file=False, require_r2=False)
    cycle = parse_cycle_id("2026052400")
    monkeypatch.setattr(fetch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(fetch, "_stream_to_file", lambda _u, _d: (_ for _ in ()).throw(_http_404()))
    with pytest.raises(RuntimeError, match="failed to fetch f06"):
        fetch.fetch_grib(cycle, 6, cfg)


def test_fetch_grib_retries_transient_then_fails(cfg, monkeypatch):
    """Non-404 errors (network/5xx) use bounded retry, not infinite polling."""
    cycle = parse_cycle_id("2026052400")
    monkeypatch.setattr(fetch.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def always_err(_url, _dest):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(fetch, "_stream_to_file", always_err)
    with pytest.raises(RuntimeError, match="failed to fetch f06"):
        fetch.fetch_grib(cycle, 6, cfg)
    assert calls["n"] == fetch.DEFAULT_RETRIES  # bounded (4 attempts), not endless
