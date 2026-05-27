"""Unit tests for thermodynamic primitives against known values."""

from __future__ import annotations

import numpy as np
import xarray as xr

from hrrr_ingest.compute import thermo


def _a(x):
    return xr.DataArray(np.array(x, dtype="float64"), dims=("p",))


def test_saturation_mixing_ratio():
    # T=Td=20C at 1000 hPa -> ~14.9 g/kg
    w = thermo.mixing_ratio(_a([20.0]), _a([1000.0])) * 1000
    assert 14.0 < float(w[0]) < 15.5


def test_wet_bulb_stull_reference():
    # Stull (2011) worked example: T=20C, RH=50% -> ~13.7C
    tw = thermo.wet_bulb_temperature(_a([20.0]), _a([50.0]))
    assert abs(float(tw[0]) - 13.7) < 0.3


def test_theta_e_increases_with_moisture():
    dry = thermo.equivalent_potential_temperature(_a([20.0]), _a([0.0]), _a([1000.0]))
    moist = thermo.equivalent_potential_temperature(_a([20.0]), _a([18.0]), _a([1000.0]))
    assert float(moist[0]) > float(dry[0])
    # saturated 25C/1000hPa is a high-300s value
    sat = thermo.equivalent_potential_temperature(_a([25.0]), _a([25.0]), _a([1000.0]))
    assert 350 < float(sat[0]) < 365


def test_lcl_height_espy():
    # Espy: 125 * (T - Td)
    z = thermo.lcl_height_espy(_a([20.0]), _a([10.0]))
    assert float(z[0]) == 1250.0


def test_lcl_temperature_below_temperature():
    t_lcl = thermo.lcl_temperature(_a([30.0]), _a([20.0]))
    assert float(t_lcl[0]) < 30.0 + thermo.KELVIN  # LCL is cooler than the parcel
