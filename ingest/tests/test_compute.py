"""Tests for derivations (synthetic) and the full compute path (fixture)."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from hrrr_ingest.compute import build_forecast_hour_dataset, derivations, mask_missing
from hrrr_ingest.config import Config


def _da(values, units="kt"):
    da = xr.DataArray(np.array(values, dtype="float32"), dims=("p",))
    da.attrs["units"] = units
    return da


def test_wind_speed_and_direction_known_vectors():
    # u, v chosen so speed/dir are exact: (3,4)->5kt; cardinal directions.
    ds = xr.Dataset({
        "u10": _da([3.0, 0.0, -10.0, 0.0]),
        "v10": _da([4.0, -10.0, 0.0, 10.0]),
    })
    out = derivations.add_derived(ds)
    np.testing.assert_allclose(out["wind_speed_10m"].values, [5, 10, 10, 10], atol=1e-4)
    # met "from" dir: (3,4)->216.87; (0,-v)->N(0); (-u,0)->E(90); (0,+v)->S(180)
    np.testing.assert_allclose(
        out["wind_dir_10m"].values, [216.8699, 0, 90, 180], atol=1e-3)
    assert out["wind_speed_10m"].attrs["units"] == "kt"
    assert out["wind_dir_10m"].attrs["units"] == "deg"
    assert out["wind_speed_10m"].attrs["derived"] == 1


def test_dewpoint_depression():
    ds = xr.Dataset({"t2m": _da([10.0, 5.0], "degC"), "d2m": _da([4.0, 5.0], "degC")})
    out = derivations.add_derived(ds)
    np.testing.assert_allclose(out["dewpoint_depression_2m"].values, [6.0, 0.0], atol=1e-4)


def test_derivations_skip_when_inputs_absent():
    ds = xr.Dataset({"sbcape": _da([1000.0], "J/kg")})
    out = derivations.add_derived(ds)
    assert "wind_speed_10m" not in out  # no u10/v10 present


def test_mask_missing_replaces_sentinels_with_nan():
    da = xr.DataArray(np.array([5.0, -999.0, 12.0, -999.0], dtype="float32"), dims=("p",))
    out = mask_missing(da, (-999.0,))
    assert np.isnan(out.values[[1, 3]]).all()
    np.testing.assert_array_equal(out.values[[0, 2]], [5.0, 12.0])


def test_mask_missing_noop_without_sentinels():
    da = xr.DataArray(np.array([1.0, 2.0]), dims=("p",))
    assert mask_missing(da, ()).identical(da)


# --- integration: real GRIB slice through the full compute path ------------

@pytest.mark.integration
def test_build_forecast_hour_dataset_from_fixture(mini_grib):
    cfg = Config.from_env(load_dotenv_file=False, require_r2=False)
    ds = build_forecast_hour_dataset(mini_grib, cfg, cycle_id="2024010100", forecast_hour=0)

    # native fields present in the fixture slice, converted to operational units
    assert ds["t2m"].attrs["units"] == "degC"
    assert ds["mslp"].attrs["units"] == "hPa"
    assert ds["u10"].attrs["units"] == "kt"
    # 3D fields carry the isobaric axis and are dask-chunked
    assert "isobaricInhPa" in ds["t_pressure"].dims
    assert ds["t_pressure"].chunks is not None
    # derivations were added
    for v in ("wind_speed_10m", "wind_dir_10m", "dewpoint_depression_2m", "lapse_700_500"):
        assert v in ds, v
    # physical sanity
    assert -60 < float(ds["t2m"].min()) < 60
    assert 0 <= float(ds["wind_dir_10m"].max()) <= 360
    # echo top's -999 no-echo sentinel is masked to NaN (never shown to users)
    assert "retop" in ds
    assert float(ds["retop"].min()) > -999.0
    assert bool(np.isnan(ds["retop"]).any())

    # metadata stamped
    assert ds.attrs["cycle_id"] == "2024010100"
    assert ds.attrs["valid_time"].startswith("2024-01-01T00:00")
