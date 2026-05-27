"""Unit tests for Stage-A composite formulas with known inputs."""

from __future__ import annotations

import numpy as np
import xarray as xr

from hrrr_ingest.compute.composites import KT_TO_MS, add_composites


def _val(da):
    return float(np.asarray(da.values).ravel()[0])


def _grid(value):
    return (("y", "x"), np.array([[value]], dtype="float32"))


def _iso(values):
    return (("isobaricInhPa", "y", "x"), np.array(values, dtype="float32").reshape(-1, 1, 1))


def _base_dataset():
    # Levels include the lowest 100 mb (1000/925) so the mean-layer parcel (MLLCL)
    # is defined, plus 850/700/500 chosen so K=29, TT=45, CT=20.
    return xr.Dataset(
        {
            "t_pressure": _iso([18.0, 16.0, 15.0, 8.0, -10.0]),
            "dpt_pressure": _iso([12.0, 11.0, 10.0, 2.0, -25.0]),
            "mlcape_180mb": _grid(2000.0),
            "mucape": _grid(2500.0),
            "sbcape": _grid(1800.0),
            "srh_0_1km": _grid(150.0),
            "srh_0_3km": _grid(300.0),
            "shear_0_6km_magnitude": _grid(40.0),   # kt
            "surface_pressure": _grid(1000.0),
        },
        coords={"isobaricInhPa": [1000, 925, 850, 700, 500]},
    )


def test_simple_indices_known_values():
    ds = add_composites(_base_dataset())
    assert abs(_val(ds["k_index"]) - 29.0) < 1e-3
    assert abs(_val(ds["total_totals"]) - 45.0) < 1e-3
    assert abs(_val(ds["cross_totals"]) - 20.0) < 1e-3


def test_craven_brooks():
    ds = add_composites(_base_dataset())
    expected = 2000.0 * (40.0 * KT_TO_MS)  # MLCAPE * shear(m/s)
    assert abs(_val(ds["craven_brooks"]) - expected) < 1.0


def test_stp_and_scp_present_and_nonnegative():
    ds = add_composites(_base_dataset())
    assert _val(ds["stp_fixed"]) >= 0
    assert _val(ds["scp_fixed"]) >= 0
    # with SRH01=150 (=norm) and strong shear, STP fixed should be a real positive
    assert _val(ds["stp_fixed"]) > 0


def test_composites_attrs_marked():
    ds = add_composites(_base_dataset())
    for v in ("k_index", "craven_brooks", "stp_fixed", "scp_fixed", "mdpi"):
        assert ds[v].attrs.get("composite") == 1


def test_skips_gracefully_without_inputs():
    ds = xr.Dataset({"t2m": _grid(10.0)})
    out = add_composites(ds)  # no isobaric fields -> no composites, no error
    assert not any(out[v].attrs.get("composite") for v in out.data_vars)
