"""Effective-layer composites + DCAPE, built on the gridded parcel engine.

This is the eager (numpy) part of the pipeline: it runs the parcel engine to get
the effective inflow layer, ESRH, EBWD, DCAPE, WINDEX, and SHERBE, then assembles
the effective-layer STP/SCP (SPC mesoanalysis). Skipped automatically when the
required inputs are absent (e.g. the small test fixture).

Memory: the parcel work is done in spatial **y-tiles** (``cfg.parcel_tiles``).
Each tile loads only its slice of the 3D environment and holds only tile-sized
intermediates, so peak RSS per worker scales ~1/N_tiles — letting a smaller box
run more parallel forecast-hour workers. Results are stitched back into full
CONUS fields; the cheap 2D combinations (effective STP/SCP) run once at the end.

User-facing CAPE stays HRRR's published fields (project decision 2026-05-26);
the parcel engine's CAPE is used only to locate the effective inflow layer.
"""

from __future__ import annotations

import logging

import numpy as np
import xarray as xr

from . import composites, parcel, thermo
from .composites import (
    KT_TO_MS,
    SCP_CAPE_NORM,
    STP_CAPE_NORM,
    STP_SHEAR_MAX_MS,
    STP_SHEAR_MIN_MS,
    STP_SHEAR_NORM_MS,
)

logger = logging.getLogger("hrrr_ingest.compute.parcel_composites")

ESRH_NORM = 150.0          # m2/s2 (effective STP)
SCP_ESRH_NORM = 50.0       # m2/s2 (effective SCP)
EBWD_NORM_MS = 20.0
SCP_EBWD_MIN_MS, SCP_EBWD_MAX_MS = 10.0, 20.0

# 2D fields the tiled parcel pass produces (NaN/0 where no effective layer).
# shear_0_3km / lapse_0_3km are simple derivations (not composites) that happen
# to need the same height interpolation, so they ride along on this pass.
_TILE_FIELDS = ("base", "top", "esrh", "ebwd", "dcape", "windex", "sherb",
                "shear_0_3km", "lapse_0_3km")

_REQUIRED = (
    "t_pressure", "dpt_pressure", "hgt_pressure", "u_pressure", "v_pressure",
    "surface_pressure", "ustm_0_6km", "vstm_0_6km", "el_height",
    "mlcape_180mb", "mlcin_180mb", "mucape",
)


def _wrap(ds, arr, description, units, source="composite"):
    """Wrap a 2D numpy result as a DataArray inheriting the grid's dims/coords."""
    da = ds["surface_pressure"].copy(data=arr.astype("float32"))
    da.attrs = {"description": description, "units": units,
                "temporal_type": "instantaneous", "source": source}
    if source == "composite":
        da.attrs["composite"] = 1
    return da


def _tile_bounds(ny: int, n_tiles: int):
    edges = np.linspace(0, ny, n_tiles + 1, dtype=int)
    return list(zip(edges[:-1], edges[1:], strict=True))


def add_parcel_composites(
    ds: xr.Dataset, n_tiles: int = 4, ascent_nsub: int = 3, descent_nsub: int = 6
) -> xr.Dataset:
    """Add effective inflow layer, ESRH, EBWD, DCAPE, WINDEX, SHERBE, eff STP/SCP."""
    missing = [v for v in _REQUIRED if v not in ds]
    if missing:
        logger.info("skipping parcel composites (missing inputs)", extra={"missing": missing})
        return ds

    levels = np.atleast_1d(ds["t_pressure"]["isobaricInhPa"].values)
    order = np.argsort(-levels)                      # surface -> top
    plev = levels[order].astype("float32")
    ny, nx = ds["surface_pressure"].shape
    has_windex = all(v in ds for v in ("freezing_level", "orography", "t2m", "d2m"))
    has_sherb = all(v in ds for v in ("orography", "t2m", "lapse_700_500"))
    # 0-3 km bulk shear + lapse rate: derived, need winds/temp at 3 km AGL.
    has_kine3 = all(v in ds for v in ("orography", "t2m", "u10", "v10"))

    out = {f: np.full((ny, nx), np.nan, dtype="float32") for f in _TILE_FIELDS}
    coverage = 0.0
    for y0, y1 in _tile_bounds(ny, max(1, n_tiles)):
        tile = _run_tile(
            ds, slice(y0, y1), order, plev, ascent_nsub, descent_nsub,
            has_windex, has_sherb, has_kine3)
        for f in _TILE_FIELDS:
            out[f][y0:y1] = tile[f]
        coverage += tile["coverage"] * (y1 - y0)

    ds["effective_inflow_base"] = _wrap(ds, out["base"], "Effective inflow base height", "m")
    ds["effective_inflow_top"] = _wrap(ds, out["top"], "Effective inflow top height", "m")
    ds["esrh"] = _wrap(ds, out["esrh"], "Effective storm-relative helicity", "m2/s2")
    ds["ebwd"] = _wrap(ds, out["ebwd"], "Effective bulk wind difference", "m/s")
    ds["dcape"] = _wrap(ds, out["dcape"], "Downdraft CAPE", "J/kg")
    if has_windex:
        ds["windex"] = _wrap(ds, out["windex"], "WINDEX", "kt")
    if has_sherb:
        ds["sherb"] = _wrap(ds, out["sherb"], "SHERBE composite", "dimensionless")
    if has_kine3:
        ds["shear_0_3km_magnitude"] = _wrap(
            ds, out["shear_0_3km"], "0-3 km bulk shear magnitude", "kt", source="derived")
        ds["lapse_0_3km"] = _wrap(
            ds, out["lapse_0_3km"], "0-3 km lapse rate", "degC/km", source="derived")

    _effective_stp_scp(ds)
    logger.info("added parcel composites",
                extra={"n_tiles": n_tiles, "eff_layer_coverage_pct": float(100 * coverage / ny)})
    return ds


def _run_tile(ds, ys, order, plev, ascent_nsub, descent_nsub,
              has_windex, has_sherb, has_kine3) -> dict:
    """Run the full parcel suite on one y-tile; return its 2D result arrays."""
    sub = ds.isel(y=ys)

    def a3(name):
        return sub[name].transpose("isobaricInhPa", "y", "x").values[order]

    te, tde, z = a3("t_pressure"), a3("dpt_pressure"), a3("hgt_pressure")
    u, v = a3("u_pressure") * KT_TO_MS, a3("v_pressure") * KT_TO_MS
    sp = sub["surface_pressure"].values
    cu, cv = sub["ustm_0_6km"].values * KT_TO_MS, sub["vstm_0_6km"].values * KT_TO_MS
    el = sub["el_height"].values

    cape_prof, cin_prof = parcel.parcel_cape_cin_by_start(te, tde, z, plev, sp, nsub=ascent_nsub)
    base_z, top_z, active, has_eff = parcel.effective_inflow_layer(cape_prof, cin_prof, z, plev)
    esrh = parcel.effective_srh(u, v, cu, cv, active)
    ebwd = parcel.effective_bulk_shear(u, v, z, base_z, el)
    dc = parcel.dcape(te, tde, z, plev, sp, nsub=descent_nsub)

    nan = np.full(sp.shape, np.nan, dtype="float32")
    res = {
        "base": base_z, "top": top_z,
        "esrh": np.where(has_eff, esrh, 0.0),
        "ebwd": np.where(has_eff, ebwd, 0.0),
        "dcape": dc,
        "windex": nan, "sherb": nan, "shear_0_3km": nan.copy(), "lapse_0_3km": nan.copy(),
        "coverage": float(has_eff.mean()),
    }
    if has_windex:
        res["windex"] = _windex_tile(sub, te, tde, z, plev)
    if has_sherb:
        res["sherb"] = _sherb_tile(sub, te, z, res["ebwd"])
    if has_kine3:
        z3 = sub["orography"].values + 3000.0
        # 0-3 km bulk shear: |V(3 km AGL) - V(10 m)|, returned in kt to match other shear.
        u10, v10 = sub["u10"].values * KT_TO_MS, sub["v10"].values * KT_TO_MS
        u3 = parcel.interp_to_height(u, z, z3)
        v3 = parcel.interp_to_height(v, z, z3)
        res["shear_0_3km"] = (np.hypot(u3 - u10, v3 - v10) / KT_TO_MS).astype("float32")
        # 0-3 km lapse rate (degC/km) from 2 m temp to 3 km AGL temp.
        t3 = parcel.interp_to_height(te, z, z3)
        res["lapse_0_3km"] = ((sub["t2m"].values - t3) / 3.0).astype("float32")
    return res


def _windex_tile(sub, te, tde, z, plev):
    """WINDEX (McCann 1994, kt) on one tile."""
    mr3 = thermo.mixing_ratio(tde, plev[:, None, None]) * 1000.0          # g/kg
    orog = sub["orography"].values
    hm_km = np.clip((sub["freezing_level"].values - orog) / 1000.0, 0.0, None)
    ql = thermo.mixing_ratio(sub["d2m"].values, sub["surface_pressure"].values) * 1000.0
    qm = parcel.interp_to_height(mr3, z, sub["freezing_level"].values)
    rq = np.clip(ql / 12.0, None, 1.0)
    gamma = np.where(hm_km > 0.1, sub["t2m"].values / hm_km, 0.0)
    arg = hm_km * rq * (gamma ** 2 - 30.0 + ql - 2.0 * qm)
    return 5.0 * np.sqrt(np.clip(arg, 0.0, None))


def _sherb_tile(sub, te, z, ebwd_ms):
    """SHERBE = (EBWD/27) * (LR0-3km/5.2) * (LR700-500/5.6) on one tile."""
    t3km = parcel.interp_to_height(te, z, sub["orography"].values + 3000.0)
    lr03 = (sub["t2m"].values - t3km) / 3.0
    sherb = (ebwd_ms / 27.0) * (lr03 / 5.2) * (sub["lapse_700_500"].values / 5.6)
    return np.clip(sherb, 0.0, None)


def _ebwd_term_stp(ebwd: xr.DataArray) -> xr.DataArray:
    term = (ebwd / EBWD_NORM_MS).clip(0.0, STP_SHEAR_MAX_MS / STP_SHEAR_NORM_MS)
    return term.where(ebwd >= STP_SHEAR_MIN_MS, 0.0)


def _ebwd_term_scp(ebwd: xr.DataArray) -> xr.DataArray:
    term = (ebwd / EBWD_NORM_MS).clip(0.0, SCP_EBWD_MAX_MS / EBWD_NORM_MS)
    return term.where(ebwd >= SCP_EBWD_MIN_MS, 0.0)


def _cin_term(mlcin: xr.DataArray) -> xr.DataArray:
    # SPC effective STP: (200 + MLCIN)/150, =1 above -50 J/kg, 0 below -200.
    return ((200.0 + mlcin) / 150.0).clip(0.0, 1.0)


def _effective_stp_scp(ds: xr.Dataset) -> None:
    """Effective-layer STP and SCP (SPC mesoanalysis) — cheap 2D combinations."""
    mllcl = composites._mllcl(ds)
    esrh, ebwd = ds["esrh"], ds["ebwd"]

    if mllcl is not None and "mlcape_180mb" in ds and "mlcin_180mb" in ds:
        stp = (
            (ds["mlcape_180mb"] / STP_CAPE_NORM)
            * composites._lcl_term(mllcl)
            * (esrh / ESRH_NORM)
            * _ebwd_term_stp(ebwd)
            * _cin_term(ds["mlcin_180mb"])
        )
        ds["stp_effective"] = _wrap(
            ds, stp.clip(min=0).values, "Significant Tornado Parameter (effective)",
            "dimensionless")

    if "mucape" in ds:
        scp = (ds["mucape"] / SCP_CAPE_NORM) * (esrh / SCP_ESRH_NORM) * _ebwd_term_scp(ebwd)
        ds["scp_effective"] = _wrap(
            ds, scp.clip(min=0).values, "Supercell Composite Parameter (effective)",
            "dimensionless")
