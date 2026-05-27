"""Unit conversions to operational units (spec 3.2 step 5).

cfgrib hands back SI units (K, Pa, m s**-1, gpm). Forecasters work in other
units, and presets are written against them (spec 6.1 uses kt, hPa-ish, m,
J/kg, m2/s2). Conversions are kept in one registry so the operational-unit
choices are easy to review and change in a single place.

NOTE: the specific target units (e.g. temperature in degC vs degF, winds in kt)
are an operational-preference call worth a forecaster review; they are isolated
here precisely so that review is a one-line edit per conversion.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import xarray as xr

KELVIN_OFFSET = 273.15
MS_TO_KT = 1.9438444924406  # 1 m/s in knots

# Each converter maps an SI DataArray to operational units and returns the new
# units string alongside the converted array.
Converter = Callable[["xr.DataArray"], "xr.DataArray"]


def kelvin_to_celsius(da: xr.DataArray) -> xr.DataArray:
    out = da - KELVIN_OFFSET
    out.attrs["units"] = "degC"
    return out


def pa_to_hpa(da: xr.DataArray) -> xr.DataArray:
    out = da / 100.0
    out.attrs["units"] = "hPa"
    return out


def ms_to_kt(da: xr.DataArray) -> xr.DataArray:
    out = da * MS_TO_KT
    out.attrs["units"] = "kt"
    return out


def gpm_to_m(da: xr.DataArray) -> xr.DataArray:
    # Geopotential meters are numerically ~geometric meters; relabel only.
    out = da.copy()
    out.attrs["units"] = "m"
    return out


# Conversion keys referenced by catalog entries.
CONVERTERS: dict[str, Converter] = {
    "K_to_degC": kelvin_to_celsius,
    "Pa_to_hPa": pa_to_hpa,
    "ms_to_kt": ms_to_kt,
    "gpm_to_m": gpm_to_m,
}


def apply_conversion(da: xr.DataArray, convert: str | None) -> xr.DataArray:
    """Apply a named conversion (or pass through unchanged when ``convert`` is None)."""
    if convert is None:
        return da
    try:
        return CONVERTERS[convert](da)
    except KeyError as exc:
        raise ValueError(f"unknown unit conversion {convert!r}") from exc
