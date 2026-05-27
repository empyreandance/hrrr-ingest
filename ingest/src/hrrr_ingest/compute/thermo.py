"""Vectorized thermodynamic primitives for derived parameters and composites.

All functions operate elementwise on xarray DataArrays (numpy/dask under the
hood) so they vectorize across the full CONUS grid and any level axis. Inputs
use the operational units the catalog produces unless noted: temperature and
dewpoint in degC, pressure in hPa, mixing ratio in kg/kg.

References:
  * Bolton (1980), MWR 108: saturation vapor pressure (eq 10), LCL temperature
    (eq 15), equivalent potential temperature (eq 39).
  * Stull (2011), JAMC 50: wet-bulb temperature from T and RH.
  * Lawrence (2005), BAMS: LCL height ~= 125*(T-Td) (Espy's rule).
"""

from __future__ import annotations

import numpy as np
import xarray as xr

RD = 287.04        # J/kg/K, gas constant for dry air
RV = 461.5         # J/kg/K, gas constant for water vapor
CPD = 1005.7       # J/kg/K, specific heat of dry air at constant pressure
LV = 2.501e6       # J/kg, latent heat of vaporization (≈0C)
G = 9.80665        # m/s^2
KELVIN = 273.15
EPSILON = 0.622    # Rd/Rv
KAPPA = RD / CPD   # ≈ 0.2854 (Poisson exponent)


def saturation_vapor_pressure(t_c: xr.DataArray) -> xr.DataArray:
    """Saturation vapor pressure (hPa) over water; Bolton (1980) eq 10. T in degC."""
    return 6.112 * np.exp(17.67 * t_c / (t_c + 243.5))


def vapor_pressure_from_dewpoint(td_c: xr.DataArray) -> xr.DataArray:
    """Actual vapor pressure (hPa) from dewpoint (degC)."""
    return saturation_vapor_pressure(td_c)


def mixing_ratio(td_c: xr.DataArray, p_hpa: xr.DataArray) -> xr.DataArray:
    """Water-vapor mixing ratio (kg/kg) from dewpoint (degC) and pressure (hPa)."""
    e = vapor_pressure_from_dewpoint(td_c)
    return EPSILON * e / (p_hpa - e)


def saturation_mixing_ratio(t_c: xr.DataArray, p_hpa: xr.DataArray) -> xr.DataArray:
    """Saturation mixing ratio (kg/kg) from temperature (degC) and pressure (hPa)."""
    es = saturation_vapor_pressure(t_c)
    return EPSILON * es / (p_hpa - es)


def potential_temperature(t_c: xr.DataArray, p_hpa: xr.DataArray) -> xr.DataArray:
    """Potential temperature (K) from temperature (degC) and pressure (hPa)."""
    return (t_c + KELVIN) * (1000.0 / p_hpa) ** 0.2854


def lcl_temperature(t_c: xr.DataArray, td_c: xr.DataArray) -> xr.DataArray:
    """LCL temperature (K); Bolton (1980) eq 15. T, Td in degC."""
    t_k = t_c + KELVIN
    td_k = td_c + KELVIN
    return 1.0 / (1.0 / (td_k - 56.0) + np.log(t_k / td_k) / 800.0) + 56.0


def equivalent_potential_temperature(
    t_c: xr.DataArray, td_c: xr.DataArray, p_hpa: xr.DataArray
) -> xr.DataArray:
    """Equivalent potential temperature (K); Bolton (1980) eq 39."""
    t_k = t_c + KELVIN
    w = mixing_ratio(td_c, p_hpa)            # kg/kg
    t_lcl = lcl_temperature(t_c, td_c)
    theta_dl = t_k * (1000.0 / (p_hpa - vapor_pressure_from_dewpoint(td_c))) ** 0.2854 * (
        (t_k / t_lcl) ** (0.28 * w)
    )
    return theta_dl * np.exp((3036.0 / t_lcl - 1.78) * w * (1.0 + 0.448 * w))


def wet_bulb_temperature(t_c: xr.DataArray, rh_pct: xr.DataArray) -> xr.DataArray:
    """Wet-bulb temperature (degC) from temperature (degC) and RH (%); Stull (2011).

    Valid roughly for 5-99% RH and -20..50 degC; adequate for operational masks.
    """
    rh = rh_pct
    return (
        t_c * np.arctan(0.151977 * np.sqrt(rh + 8.313659))
        + np.arctan(t_c + rh)
        - np.arctan(rh - 1.676331)
        + 0.00391838 * rh ** 1.5 * np.arctan(0.023101 * rh)
        - 4.686035
    )


def moist_lapse_dlnp(t_k, p_hpa):
    """Pseudoadiabatic lapse rate dT/d(ln p) (K) at temperature t_k (K), p (hPa).

    Standard saturated lapse rate; used to integrate a parcel along the moist
    adiabat. Works on numpy arrays (the parcel engine runs eagerly).
    """
    t_c = t_k - KELVIN
    es = 6.112 * np.exp(17.67 * t_c / (t_c + 243.5))
    rs = EPSILON * es / (p_hpa - es)
    num = RD * t_k + LV * rs
    den = CPD + (LV ** 2 * rs * EPSILON) / (RD * t_k ** 2)
    return num / den


def lcl_pressure(t_c, td_c, p_hpa):
    """LCL pressure (hPa) from parcel T, Td (degC) and pressure (hPa)."""
    t_k = t_c + KELVIN
    t_lcl = lcl_temperature(t_c, td_c)
    return p_hpa * (t_lcl / t_k) ** (1.0 / KAPPA)


def lcl_height_espy(t_c: xr.DataArray, td_c: xr.DataArray) -> xr.DataArray:
    """LCL height above the parcel's level (m); Espy's rule, ~125*(T-Td).

    The cheap T-Td approximation chosen for v1 (avoids a full parcel ascent).
    """
    return 125.0 * (t_c - td_c)
