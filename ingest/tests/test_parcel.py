"""Unit tests for the parcel engine's deterministic pieces.

CAPE/CIN accuracy is validated separately against HRRR's own fields on a real
case; these cover the discrete logic (effective inflow layer, interpolation).
"""

from __future__ import annotations

import numpy as np
import xarray as xr

from hrrr_ingest.compute import parcel
from hrrr_ingest.compute.parcel_composites import add_parcel_composites


def _stack(values):
    return np.array(values, dtype="float64").reshape(-1, 1, 1)


def test_effective_inflow_layer_contiguous_band():
    z = _stack([100, 600, 1100, 1600, 2100])
    cape = _stack([50, 150, 200, 50, 0])      # qualifies at idx 1,2
    cin = _stack([0, 0, 0, 0, 0])             # all >= -250
    plev = np.array([1000, 900, 800, 700, 600], dtype="float64")
    base_z, top_z, active, has = parcel.effective_inflow_layer(cape, cin, z, plev)
    assert bool(has[0, 0])
    assert float(base_z[0, 0]) == 600
    assert float(top_z[0, 0]) == 1100
    assert active[:, 0, 0].tolist() == [False, True, True, False, False]


def test_effective_inflow_layer_excludes_high_cin():
    z = _stack([100, 600, 1100])
    cape = _stack([200, 200, 200])
    cin = _stack([-400, 0, 0])                # base level disqualified by CIN
    plev = np.array([1000, 900, 800], dtype="float64")
    base_z, top_z, active, has = parcel.effective_inflow_layer(cape, cin, z, plev)
    assert float(base_z[0, 0]) == 600        # first qualifying level


def test_effective_inflow_layer_none():
    z = _stack([100, 600, 1100])
    cape = _stack([50, 50, 50])               # never reaches 100
    cin = _stack([0, 0, 0])
    plev = np.array([1000, 900, 800], dtype="float64")
    _, _, _, has = parcel.effective_inflow_layer(cape, cin, z, plev)
    assert not bool(has[0, 0])


def test_interp_to_height_linear():
    z = _stack([0, 1000, 2000])
    field = _stack([10, 20, 30])
    np.testing.assert_allclose(parcel.interp_to_height(field, z, np.array([[500.0]]))[0, 0], 15.0)
    np.testing.assert_allclose(parcel.interp_to_height(field, z, np.array([[1500.0]]))[0, 0], 25.0)


def test_effective_srh_zero_when_no_layer():
    u = _stack([10, 20, 30])
    v = _stack([0, 5, 10])
    active = np.zeros((3, 1, 1), dtype=bool)
    assert float(parcel.effective_srh(u, v, 5.0, 2.0, active)[0, 0]) == 0.0


def _synthetic_env(ny=12, nx=6):
    levels = [1000, 925, 850, 700, 500, 300]
    z = [100, 760, 1460, 3000, 5800, 9500]
    t = [28, 24, 20, 10, -8, -35]
    td = [22, 19, 15, 2, -18, -50]
    u = [10, 20, 30, 40, 55, 70]
    v = [5, 8, 10, 15, 20, 25]

    def iso(col):
        return (("isobaricInhPa", "y", "x"),
                np.broadcast_to(np.array(col, "float32")[:, None, None], (len(col), ny, nx)).copy())

    def g(val):
        return (("y", "x"), np.full((ny, nx), val, "float32"))

    return xr.Dataset(
        {
            "t_pressure": iso(t), "dpt_pressure": iso(td), "hgt_pressure": iso(z),
            "u_pressure": iso(u), "v_pressure": iso(v),
            "surface_pressure": g(1000.0), "ustm_0_6km": g(35.0), "vstm_0_6km": g(12.0),
            "el_height": g(12000.0), "mlcape_180mb": g(2500.0), "mlcin_180mb": g(-30.0),
            "mucape": g(3000.0), "freezing_level": g(3500.0), "orography": g(300.0),
            "t2m": g(29.0), "d2m": g(23.0), "lapse_700_500": g(7.0),
            "u10": g(8.0), "v10": g(4.0),
        },
        coords={"isobaricInhPa": levels},
    )


def test_tiling_is_equivalent_to_no_tiling():
    # Parcel work is per-column, so y-tiling must reproduce the untiled result.
    one = add_parcel_composites(_synthetic_env().copy(deep=True), n_tiles=1)
    many = add_parcel_composites(_synthetic_env().copy(deep=True), n_tiles=4)
    for v in ("esrh", "ebwd", "dcape", "stp_effective", "scp_effective", "windex", "sherb",
              "shear_0_3km_magnitude", "lapse_0_3km"):
        np.testing.assert_allclose(
            one[v].values, many[v].values, rtol=1e-5, equal_nan=True, err_msg=v)


def test_parcel_composites_produce_expected_fields():
    ds = add_parcel_composites(_synthetic_env(), n_tiles=2)
    for v in ("effective_inflow_base", "esrh", "ebwd", "dcape", "stp_effective",
              "scp_effective", "windex", "sherb", "shear_0_3km_magnitude", "lapse_0_3km"):
        assert v in ds, v
    assert float(ds["scp_effective"].max()) >= 0
    # 0-3 km shear/lapse are tagged derived (not composite) so they badge as "calc"
    assert ds["shear_0_3km_magnitude"].attrs["source"] == "derived"
