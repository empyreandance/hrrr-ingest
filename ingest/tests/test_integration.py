"""End-to-end smoke test: real GRIB slice -> compute -> publish -> manifest.

This is the spec 10.1.4 smoke test, run against a local object store (a temp
directory standing in for R2; the code path is identical because both go through
fsspec). It proves the whole data flow works before any server/R2 is rented.
Network-gated via the ``mini_grib`` fixture (skips when offline).
"""

from __future__ import annotations

import json

import pytest
import xarray as xr

from hrrr_ingest import publish
from hrrr_ingest.config import Config
from hrrr_ingest.cycle import parse_cycle_id
from hrrr_ingest.publish import Store


@pytest.mark.integration
def test_end_to_end_one_forecast_hour(mini_grib, tmp_path, monkeypatch):
    from hrrr_ingest import compute

    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    cfg = Config.from_env(load_dotenv_file=False, require_r2=False)
    store = Store(base_url=str(tmp_path), storage_options={})
    cycle = parse_cycle_id("2024010100")

    # compute -> write FH to local scratch -> assemble one sharded store -> manifests
    ds = compute.build_forecast_hour_dataset(mini_grib, cfg, cycle_id="2024010100", forecast_hour=0)
    publish.write_forecast_hour_local(ds, cycle, 0, cfg)
    publish.assemble_and_upload_cycle(cycle, [0], cfg, store=store)
    params = publish.parameter_metadata(ds)
    publish.write_cycle_manifest(cycle, [0], cfg, parameters=params, store=store)
    publish.promote_cycle(cycle, cfg, store=store)

    # --- read everything back the way the frontend would ---
    # 1) global manifest points at the cycle
    assert publish.read_current_cycle_id(cfg, store=store) == "2024010100"

    # 2) cycle manifest carries provenance the frontend can badge
    cyc_manifest = json.loads(store.fs.cat_file(store.path("cycles/2024010100/manifest.json")))
    by_id = {p["id"]: p for p in cyc_manifest["parameters"]}
    assert by_id["t2m"]["source"] == "hrrr"               # straight from HRRR
    assert by_id["wind_speed_10m"]["source"] == "derived"  # computed (spec 4.2)
    assert {"hrrr", "derived"} <= {p["source"] for p in cyc_manifest["parameters"]}

    # 3) the single sharded store is readable and round-trips a value
    back = xr.open_zarr(f"{tmp_path}/cycles/2024010100/data.zarr", consolidated=True)
    assert back.sizes["forecast_hour"] == 1
    assert "t2m" in back and back["t2m"].attrs["source"] == "hrrr"
    assert "isobaricInhPa" in back["t_pressure"].dims
