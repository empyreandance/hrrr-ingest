"""Trivial derivations computed at ingest time (spec 4.2).

These are well-defined, low-risk transforms of native fields: wind speed and
direction, bulk-shear magnitude, storm-motion speed/direction, dewpoint
depression, and a couple of layer lapse rates. They operate on the assembled
dataset (after native extraction + unit conversion), are dask-friendly, and add
new ``parameter_id`` variables in place.

The harder, judgement-laden derivations in spec 4.2 (wet-bulb, theta-e, warm
cloud depth, hail growth zone depth) and all composite indices (spec 4.3) are
deferred to a focused pass.
"""

from __future__ import annotations

import logging

import numpy as np
import xarray as xr

from . import thermo

logger = logging.getLogger("hrrr_ingest.compute.derivations")


def _wind_speed(u: xr.DataArray, v: xr.DataArray, units: str) -> xr.DataArray:
    spd = np.hypot(u, v)
    spd.attrs["units"] = units
    return spd


def _wind_direction(u: xr.DataArray, v: xr.DataArray) -> xr.DataArray:
    """Meteorological direction (degrees FROM which the wind blows)."""
    direction = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
    direction.attrs["units"] = "deg"
    return direction


def _has(ds: xr.Dataset, *names: str) -> bool:
    return all(n in ds for n in names)


def add_derived(ds: xr.Dataset) -> xr.Dataset:
    """Add spec 4.2 derivations for whichever inputs are present."""
    added: list[str] = []

    # 10 m / 80 m wind speed + direction (units follow the inputs, already kt)
    for level, (u_id, v_id) in {"10m": ("u10", "v10"), "80m": ("u80", "v80")}.items():
        if _has(ds, u_id, v_id):
            u, v = ds[u_id], ds[v_id]
            units = u.attrs.get("units", "kt")
            ds[f"wind_speed_{level}"] = _describe(
                _wind_speed(u, v, units), f"{level} wind speed", "instantaneous")
            ds[f"wind_dir_{level}"] = _describe(
                _wind_direction(u, v), f"{level} wind direction", "instantaneous")
            added += [f"wind_speed_{level}", f"wind_dir_{level}"]

    # Pressure-level wind speed + direction (spec 5.1: replaces U/V in the UI)
    if _has(ds, "u_pressure", "v_pressure"):
        u, v = ds["u_pressure"], ds["v_pressure"]
        units = u.attrs.get("units", "kt")
        ds["wind_speed_pressure"] = _describe(
            _wind_speed(u, v, units), "Wind speed (3D)", "instantaneous")
        ds["wind_dir_pressure"] = _describe(
            _wind_direction(u, v), "Wind direction (3D)", "instantaneous")
        added += ["wind_speed_pressure", "wind_dir_pressure"]

    # Bulk shear magnitude (from VUCSH/VVCSH components, already kt)
    for layer in ("0_1km", "0_6km"):
        u_id, v_id = f"vucsh_{layer}", f"vvcsh_{layer}"
        if _has(ds, u_id, v_id):
            mag = _wind_speed(ds[u_id], ds[v_id], ds[u_id].attrs.get("units", "kt"))
            ds[f"shear_{layer}_magnitude"] = _describe(
                mag, f"{layer.replace('_', '-')} bulk shear magnitude", "instantaneous")
            added.append(f"shear_{layer}_magnitude")

    # Storm motion speed + direction (from USTM/VSTM)
    if _has(ds, "ustm_0_6km", "vstm_0_6km"):
        u, v = ds["ustm_0_6km"], ds["vstm_0_6km"]
        units = u.attrs.get("units", "kt")
        ds["storm_motion_speed"] = _describe(
            _wind_speed(u, v, units), "Storm motion speed", "instantaneous")
        ds["storm_motion_dir"] = _describe(
            _wind_direction(u, v), "Storm motion direction", "instantaneous")
        added += ["storm_motion_speed", "storm_motion_dir"]

    # Dewpoint depression (T - Td) at 2 m and on pressure levels
    if _has(ds, "t2m", "d2m"):
        dd = ds["t2m"] - ds["d2m"]
        ds["dewpoint_depression_2m"] = _describe(dd, "2 m dewpoint depression", "instantaneous")
        ds["dewpoint_depression_2m"].attrs["units"] = "degC"
        added.append("dewpoint_depression_2m")
    if _has(ds, "t_pressure", "dpt_pressure"):
        dd = ds["t_pressure"] - ds["dpt_pressure"]
        ds["dewpoint_depression_pressure"] = _describe(
            dd, "Dewpoint depression (3D)", "instantaneous")
        ds["dewpoint_depression_pressure"].attrs["units"] = "degC"
        added.append("dewpoint_depression_pressure")

    # A couple of layer lapse rates from the 3D temperature field (degC/km).
    if "t_pressure" in ds and "isobaricInhPa" in ds["t_pressure"].dims:
        ds = _add_lapse_rates(ds, added)

    _add_thermo_derivations(ds, added)
    _add_layer_depths(ds, added)

    logger.info("added derivations", extra={"n": len(added), "vars": added})
    return ds


def _add_thermo_derivations(ds: xr.Dataset, added: list[str]) -> None:
    """Theta-e, wet-bulb, and mean-RH slabs (spec 4.2)."""
    # Surface (2 m) theta-e and wet-bulb
    if _has(ds, "t2m", "d2m") and "surface_pressure" in ds:
        ds["theta_e_2m"] = _describe(
            thermo.equivalent_potential_temperature(ds["t2m"], ds["d2m"], ds["surface_pressure"]),
            "2 m equivalent potential temperature", "instantaneous")
        ds["theta_e_2m"].attrs["units"] = "K"
        added.append("theta_e_2m")
    if _has(ds, "t2m", "rh2m"):
        ds["wet_bulb_2m"] = _describe(
            thermo.wet_bulb_temperature(ds["t2m"], ds["rh2m"]), "2 m wet-bulb temperature",
            "instantaneous")
        ds["wet_bulb_2m"].attrs["units"] = "degC"
        added.append("wet_bulb_2m")

    # 850 mb theta-e (common for downstream composites / inspection)
    if _has(ds, "t_pressure", "dpt_pressure"):
        levels = set(np.atleast_1d(ds["t_pressure"]["isobaricInhPa"].values).tolist())
        if 850 in levels:
            t850 = ds["t_pressure"].sel(isobaricInhPa=850)
            td850 = ds["dpt_pressure"].sel(isobaricInhPa=850)
            ds["theta_e_850"] = _describe(
                thermo.equivalent_potential_temperature(t850, td850, xr.full_like(t850, 850.0)),
                "850 mb equivalent potential temperature", "instantaneous")
            ds["theta_e_850"].attrs["units"] = "K"
            added.append("theta_e_850")

    # Mean RH over common slabs (spec 4.2)
    if "rh_pressure" in ds and "isobaricInhPa" in ds["rh_pressure"].dims:
        for lo, hi, name in ((500, 700, "mean_rh_700_500"), (500, 850, "mean_rh_850_500")):
            slab = ds["rh_pressure"].sel(isobaricInhPa=slice(hi, lo))
            if slab.sizes.get("isobaricInhPa", 0) >= 2:
                ds[name] = _describe(slab.mean("isobaricInhPa"),
                                     f"Mean RH {lo}-{hi} mb", "instantaneous")
                ds[name].attrs["units"] = "%"
                added.append(name)


def _add_layer_depths(ds: xr.Dataset, added: list[str]) -> None:
    """Hail growth zone depth and warm cloud depth (spec 4.2)."""
    if _has(ds, "hgt_m10c", "hgt_m20c"):
        # Spec 4.2 writes "263 K HGT - 253 K HGT", but the -10C level sits BELOW
        # the -20C level, so that is negative. Depth is the (positive) -20C minus
        # -10C height. Flagged as a spec sign correction.
        depth = (ds["hgt_m20c"] - ds["hgt_m10c"]).clip(min=0)
        ds["hail_growth_zone_depth"] = _describe(
            depth, "Hail growth zone depth (-10C to -20C)", "instantaneous")
        ds["hail_growth_zone_depth"].attrs["units"] = "m"
        added.append("hail_growth_zone_depth")
    if _has(ds, "lcl_height", "freezing_level"):
        # Warm cloud depth: LCL up to the 0 degC level (both MSL heights here).
        wcd = (ds["freezing_level"] - ds["lcl_height"]).clip(min=0)
        ds["warm_cloud_depth"] = _describe(wcd, "Warm cloud depth (LCL to 0C)", "instantaneous")
        ds["warm_cloud_depth"].attrs["units"] = "m"
        added.append("warm_cloud_depth")


def _add_lapse_rates(ds: xr.Dataset, added: list[str]) -> xr.Dataset:
    """Layer lapse rates from 3D temperature + geopotential height (degC/km)."""
    t, gh = ds["t_pressure"], ds.get("hgt_pressure")
    levels = set(np.atleast_1d(t["isobaricInhPa"].values).tolist())
    for top, bot, name in ((500, 700, "lapse_700_500"), (500, 850, "lapse_850_500")):
        if {top, bot} <= levels and gh is not None:
            dt = t.sel(isobaricInhPa=bot) - t.sel(isobaricInhPa=top)  # warmer below
            dz_km = (gh.sel(isobaricInhPa=top) - gh.sel(isobaricInhPa=bot)) / 1000.0
            lapse = dt / dz_km
            ds[name] = _describe(lapse, f"{bot}-{top} mb lapse rate", "instantaneous")
            ds[name].attrs["units"] = "degC/km"
            added.append(name)
    return ds


def _describe(da: xr.DataArray, description: str, temporal_type: str) -> xr.DataArray:
    da.attrs["description"] = description
    da.attrs["temporal_type"] = temporal_type
    da.attrs["ui_visible"] = 1
    da.attrs["derived"] = 1
    da.attrs["source"] = "derived"   # computed from HRRR fields (spec 4.2)
    return da
