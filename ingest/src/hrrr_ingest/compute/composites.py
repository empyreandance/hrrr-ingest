"""Composite severe-weather indices (spec 4.3).

STAGE A: the algebraic and fixed-layer composites that need only level
selection on fields HRRR provides. Effective-layer STP/SCP and DCAPE (which
require the gridded parcel engine + effective inflow layer) land in a later
module.

Formulas follow SPC mesoanalysis definitions. **All normalization coefficients
and caps are named constants at the top of this module so they can be verified
and tuned in one place** — treat the numbers as the spec author's to confirm
against the SPC reference, not as settled.

Inputs use the catalog's operational units (degC, hPa, kt, m, J/kg, m2/s2);
conversions to the units each formula expects (e.g. shear in m/s) are explicit.
"""

from __future__ import annotations

import logging

import numpy as np
import xarray as xr

from . import thermo

logger = logging.getLogger("hrrr_ingest.compute.composites")

KT_TO_MS = 1.0 / 1.9438444924406

# --- SPC normalization coefficients / caps (verify against SPC reference) ---
STP_CAPE_NORM = 1500.0       # J/kg
STP_SRH_NORM = 150.0         # m2/s2
STP_SHEAR_NORM_MS = 20.0     # m/s
STP_LCL_FULL_M = 1000.0      # LCL term = 1 at/below this height
STP_LCL_ZERO_M = 2000.0      # LCL term = 0 at/above this height
STP_SHEAR_MIN_MS = 12.5      # shear term = 0 below this
STP_SHEAR_MAX_MS = 30.0      # shear term capped (=1.5) above this

SCP_CAPE_NORM = 1000.0       # J/kg (MUCAPE)
SCP_SRH_NORM = 100.0         # m2/s2 (0-3 km SRH, fixed-layer)
SCP_SHEAR_NORM_MS = 20.0     # m/s
SCP_SHEAR_MIN_MS = 10.0
SCP_SHEAR_MAX_MS = 20.0

SHIP_DENOM = 42_000_000.0
SHIP_MR_MIN, SHIP_MR_MAX = 11.0, 13.6     # g/kg
SHIP_SHEAR_MIN_MS, SHIP_SHEAR_MAX_MS = 7.0, 27.0
SHIP_T500_MAX_C = -5.5
SHIP_MUCAPE_REF = 1300.0
SHIP_LR_REF = 5.8
SHIP_FRZ_REF_M = 2400.0

MDPI_DENOM = 30.0            # K
WMSI_DENOM = 1000.0


def _lev(ds: xr.Dataset, var: str, level: int) -> xr.DataArray | None:
    if var not in ds or "isobaricInhPa" not in ds[var].dims:
        return None
    levels = np.atleast_1d(ds[var]["isobaricInhPa"].values).tolist()
    return ds[var].sel(isobaricInhPa=level) if level in levels else None


def _has(ds: xr.Dataset, *names: str) -> bool:
    return all(n in ds for n in names)


def _stamp(da: xr.DataArray, name: str, units: str) -> xr.DataArray:
    da.attrs.update({"description": name, "units": units,
                     "temporal_type": "instantaneous", "composite": 1,
                     "source": "composite"})   # computed composite index (spec 4.3)
    return da


def add_composites(ds: xr.Dataset) -> xr.Dataset:
    """Add Stage-A composite indices for whichever inputs are present."""
    added: list[str] = []
    _simple_indices(ds, added)
    _craven_brooks(ds, added)
    _ship(ds, added)
    _stp_fixed(ds, added)
    _scp_fixed(ds, added)
    _microburst(ds, added)
    logger.info("added composites", extra={"n": len(added), "vars": added})
    return ds


def _simple_indices(ds: xr.Dataset, added: list[str]) -> None:
    """K-index, Total Totals, Cross Totals (degC) from 850/700/500 T and Td."""
    t850, t700, t500 = (_lev(ds, "t_pressure", lv) for lv in (850, 700, 500))
    td850, td700 = (_lev(ds, "dpt_pressure", lv) for lv in (850, 700))
    if t850 is not None and t500 is not None and td850 is not None:
        ds["cross_totals"] = _stamp(td850 - t500, "Cross Totals", "degC")
        ds["total_totals"] = _stamp(t850 + td850 - 2 * t500, "Total Totals", "degC")
        added += ["cross_totals", "total_totals"]
        if t700 is not None and td700 is not None:
            k = (t850 - t500) + td850 - (t700 - td700)
            ds["k_index"] = _stamp(k, "K-index", "degC")
            added.append("k_index")


def _craven_brooks(ds: xr.Dataset, added: list[str]) -> None:
    """Craven-Brooks significant severe parameter = MLCAPE * 0-6 km shear (m3/s3)."""
    if _has(ds, "mlcape_180mb", "shear_0_6km_magnitude"):
        shear_ms = ds["shear_0_6km_magnitude"] * KT_TO_MS
        ds["craven_brooks"] = _stamp(
            ds["mlcape_180mb"] * shear_ms, "Craven-Brooks sig severe", "m3/s3")
        added.append("craven_brooks")


def _ship(ds: xr.Dataset, added: list[str]) -> None:
    """Significant Hail Parameter (SPC). Uses 850 mb mixing ratio per spec 4.3 note.

    TODO(stage-b): SPC technically uses the MU-parcel mixing ratio; switch once
    the parcel engine can identify the MU parcel level.
    """
    t500 = _lev(ds, "t_pressure", 500)
    td850 = _lev(ds, "dpt_pressure", 850)
    needed = ("mucape", "lapse_700_500", "shear_0_6km_magnitude", "freezing_level", "orography")
    if t500 is None or td850 is None or not _has(ds, *needed):
        return

    mucape = ds["mucape"]
    mixr = (thermo.mixing_ratio(td850, xr.full_like(td850, 850.0)) * 1000.0).clip(
        SHIP_MR_MIN, SHIP_MR_MAX)                      # g/kg
    lr75 = ds["lapse_700_500"]
    shear = (ds["shear_0_6km_magnitude"] * KT_TO_MS).clip(SHIP_SHEAR_MIN_MS, SHIP_SHEAR_MAX_MS)
    t500c = t500.clip(max=SHIP_T500_MAX_C)
    frz_agl = (ds["freezing_level"] - ds["orography"]).clip(min=0)

    ship = -(mucape * mixr * lr75 * t500c * shear) / SHIP_DENOM  # -1: t500 is negative
    # SPC correction factors
    ship = ship * xr.where(mucape < SHIP_MUCAPE_REF, mucape / SHIP_MUCAPE_REF, 1.0)
    ship = ship * xr.where(lr75 < SHIP_LR_REF, lr75 / SHIP_LR_REF, 1.0)
    ship = ship * xr.where(frz_agl < SHIP_FRZ_REF_M, frz_agl / SHIP_FRZ_REF_M, 1.0)
    ds["ship"] = _stamp(ship.clip(min=0), "Significant Hail Parameter", "dimensionless")
    added.append("ship")


def _lcl_term(lcl_m: xr.DataArray) -> xr.DataArray:
    term = (STP_LCL_ZERO_M - lcl_m) / (STP_LCL_ZERO_M - STP_LCL_FULL_M)
    return term.clip(0.0, 1.0)


def _mean_layer(ds: xr.Dataset, var: str, depth_hpa: float = 100.0) -> xr.DataArray | None:
    """Mean of a 3D field over the lowest ``depth_hpa`` above the surface."""
    if var not in ds or "surface_pressure" not in ds or "isobaricInhPa" not in ds[var].dims:
        return None
    sp = ds["surface_pressure"]
    lev = ds[var]["isobaricInhPa"]
    mask = (lev <= sp) & (lev >= sp - depth_hpa)
    return ds[var].where(mask).mean("isobaricInhPa")


def _mllcl(ds: xr.Dataset) -> xr.DataArray | None:
    """Mean-layer LCL height (m AGL) via Espy on lowest-100 mb mean T/Td (spec note)."""
    mlt = _mean_layer(ds, "t_pressure")
    mltd = _mean_layer(ds, "dpt_pressure")
    if mlt is None or mltd is None:
        return None
    return thermo.lcl_height_espy(mlt, mltd)


def _stp_fixed(ds: xr.Dataset, added: list[str]) -> None:
    """Significant Tornado Parameter, fixed layer (SPC / Thompson 2003).

    SBCAPE, MLLCL (Espy, spec note), 0-1 km SRH, 0-6 km bulk shear.
    """
    if not _has(ds, "sbcape", "srh_0_1km", "shear_0_6km_magnitude"):
        return
    mllcl = _mllcl(ds)
    if mllcl is None:
        return
    cape_term = ds["sbcape"] / STP_CAPE_NORM
    srh_term = ds["srh_0_1km"] / STP_SRH_NORM
    shear_ms = ds["shear_0_6km_magnitude"] * KT_TO_MS
    shear_term = (shear_ms / STP_SHEAR_NORM_MS).clip(0.0, STP_SHEAR_MAX_MS / STP_SHEAR_NORM_MS)
    shear_term = shear_term.where(shear_ms >= STP_SHEAR_MIN_MS, 0.0)
    stp = cape_term * _lcl_term(mllcl) * srh_term * shear_term
    ds["stp_fixed"] = _stamp(stp.clip(min=0), "Significant Tornado Parameter (fixed)",
                             "dimensionless")
    added.append("stp_fixed")


def _scp_fixed(ds: xr.Dataset, added: list[str]) -> None:
    """Supercell Composite Parameter, fixed layer (SPC / Thompson 2003).

    MUCAPE, 0-3 km SRH, 0-6 km bulk shear.
    """
    if not _has(ds, "mucape", "srh_0_3km", "shear_0_6km_magnitude"):
        return
    cape_term = ds["mucape"] / SCP_CAPE_NORM
    srh_term = ds["srh_0_3km"] / SCP_SRH_NORM
    shear_ms = ds["shear_0_6km_magnitude"] * KT_TO_MS
    shear_term = (shear_ms / SCP_SHEAR_NORM_MS).clip(max=SCP_SHEAR_MAX_MS / SCP_SHEAR_NORM_MS)
    shear_term = shear_term.where(shear_ms >= SCP_SHEAR_MIN_MS, 0.0)
    scp = cape_term * srh_term * shear_term
    ds["scp_fixed"] = _stamp(scp.clip(min=0), "Supercell Composite Parameter (fixed)",
                             "dimensionless")
    added.append("scp_fixed")


def _microburst(ds: xr.Dataset, added: list[str]) -> None:
    """MDPI and WMSI from the theta-e profile (Atkins & Wakimoto 1991)."""
    if not _has(ds, "t_pressure", "dpt_pressure", "surface_pressure"):
        return
    theta_e = thermo.equivalent_potential_temperature(
        ds["t_pressure"], ds["dpt_pressure"], ds["t_pressure"]["isobaricInhPa"])
    lev = ds["t_pressure"]["isobaricInhPa"]
    sp = ds["surface_pressure"]
    low_mask = (lev <= sp) & (lev >= sp - 150.0)
    mid_mask = (lev >= 500) & (lev <= 650)
    theta_e_low = theta_e.where(low_mask).max("isobaricInhPa")
    theta_e_mid = theta_e.where(mid_mask).min("isobaricInhPa")
    delta_theta_e = theta_e_low - theta_e_mid

    ds["mdpi"] = _stamp(delta_theta_e / MDPI_DENOM, "Microburst Day Potential Index",
                        "dimensionless")
    added.append("mdpi")
    if "mucape" in ds:
        ds["wmsi"] = _stamp(ds["mucape"] * delta_theta_e / WMSI_DENOM,
                            "Wet Microburst Severity Index", "dimensionless")
        added.append("wmsi")
