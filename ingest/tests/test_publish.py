"""Tests for Zarr writes and the atomic manifest lifecycle (local store)."""

from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr

from hrrr_ingest import publish
from hrrr_ingest.config import Config
from hrrr_ingest.cycle import parse_cycle_id
from hrrr_ingest.publish import Store


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # Point per-forecast-hour scratch at the test's tmp dir (schema 2.0 writes
    # each FH locally, then assembles + uploads one store).
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    return Config.from_env(load_dotenv_file=False, require_r2=False)


@pytest.fixture
def store(tmp_path):
    return Store(base_url=str(tmp_path), storage_options={})


@pytest.fixture
def sample_ds():
    ny, nx = 8, 10
    rng = np.random.rand
    ds = xr.Dataset(
        {
            "sbcape": (("y", "x"), rng(ny, nx).astype("float32") * 3000),
            "t_pressure": (("isobaricInhPa", "y", "x"), rng(3, ny, nx).astype("float32")),
        },
        coords={"isobaricInhPa": [850, 700, 500]},
    )
    ds["sbcape"].attrs["units"] = "J/kg"
    return ds


def test_cycle_store_roundtrip_sharded(cfg, store, sample_ds, tmp_path):
    """schema 2.0: per-FH local writes -> one sharded store with a forecast_hour
    dim, Blosc-compressed, that round-trips."""
    cycle = parse_cycle_id("2026052400")
    publish.write_forecast_hour_local(sample_ds, cycle, 0, cfg)
    publish.write_forecast_hour_local(sample_ds, cycle, 1, cfg)
    publish.assemble_and_upload_cycle(cycle, [0, 1], cfg, store=store)

    back = xr.open_zarr(f"{tmp_path}/cycles/2026052400/data.zarr", consolidated=True)
    assert back.sizes["forecast_hour"] == 2
    assert back["forecast_hour"].values.tolist() == [0, 1]
    assert np.allclose(back["sbcape"].isel(forecast_hour=0).values, sample_ds["sbcape"].values)

    import zarr
    arr = zarr.open_group(f"{tmp_path}/cycles/2026052400/data.zarr")["sbcape"]
    assert arr.shards is not None  # sharded (one object for the whole 2D field)
    meta = str(arr.metadata).lower()
    assert "sharding" in meta and "blosc" in meta


def test_list_cycle_ids(cfg, store):
    """list_cycle_ids returns every cycle dir present (for GC), ignoring junk."""
    assert publish.list_cycle_ids(cfg, store=store) == []  # empty store
    for cid in ("2026052723", "2026052800", "2026052806"):
        publish.write_cycle_manifest(parse_cycle_id(cid), [0], cfg, store=store)
    # a non-cycle entry under cycles/ must be ignored
    store.fs.makedirs(store.path("cycles/notacycle"), exist_ok=True)
    assert sorted(publish.list_cycle_ids(cfg, store=store)) == [
        "2026052723", "2026052800", "2026052806",
    ]


def test_manifest_lifecycle(cfg, store, sample_ds):
    prev, new = parse_cycle_id("2026052323"), parse_cycle_id("2026052400")

    # nothing promoted yet
    assert publish.read_current_cycle_id(cfg, store=store) is None

    # an existing previous cycle
    publish.promote_cycle(prev, cfg, store=store)
    assert publish.read_current_cycle_id(cfg, store=store) == "2026052323"

    # write + promote the new cycle, then drop the previous
    publish.write_forecast_hour_local(sample_ds, new, 0, cfg)
    publish.assemble_and_upload_cycle(new, [0], cfg, store=store)
    params = publish.parameter_metadata(sample_ds)
    manifest = publish.write_cycle_manifest(new, [0], cfg, parameters=params, store=store)
    assert manifest["cycle_id"] == "2026052400"
    assert manifest["forecast_hours"] == [0]
    assert "sbcape" in manifest["variables"]
    assert any(p["id"] == "sbcape" and p["source"] == "hrrr" for p in manifest["parameters"])

    previous = publish.read_current_cycle_id(cfg, store=store)
    publish.promote_cycle(new, cfg, store=store)
    assert previous == "2026052323"
    assert publish.read_current_cycle_id(cfg, store=store) == "2026052400"

    publish.delete_cycle(prev, cfg, store=store)
    assert not store.fs.exists(store.path("cycles/2026052323"))


def test_global_manifest_shape(cfg, store):
    cycle = parse_cycle_id("2026052412")
    publish.promote_cycle(cycle, cfg, store=store)
    data = json.loads(store.fs.cat_file(store.path("manifest.json")))
    assert data["current_cycle"] == "2026052412"
    assert data["cycle_manifest_key"] == "cycles/2026052412/manifest.json"
    assert data["schema_version"] == publish.SCHEMA_VERSION


def test_extended_pointer_promotion(cfg, store):
    """current_extended_cycle is set by 00/06/12/18Z runs and preserved by others."""
    extended = parse_cycle_id("2026052400")  # 00Z → extended
    standard = parse_cycle_id("2026052401")  # 01Z → standard

    # First promote the extended run: both pointers go to it.
    publish.promote_cycle(extended, cfg, store=store)
    assert publish.read_current_cycle_id(cfg, store=store) == "2026052400"
    assert publish.read_current_extended_cycle_id(cfg, store=store) == "2026052400"

    # Then promote a standard run: current advances but extended stays put.
    publish.promote_cycle(standard, cfg, store=store)
    assert publish.read_current_cycle_id(cfg, store=store) == "2026052401"
    assert publish.read_current_extended_cycle_id(cfg, store=store) == "2026052400"

    # The next extended run (06Z) updates both pointers.
    next_extended = parse_cycle_id("2026052406")
    publish.promote_cycle(next_extended, cfg, store=store)
    assert publish.read_current_cycle_id(cfg, store=store) == "2026052406"
    assert publish.read_current_extended_cycle_id(cfg, store=store) == "2026052406"
