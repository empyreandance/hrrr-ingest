"""Gridded parcel-theory engine (Stage B foundation).

Lifts air parcels across the CONUS grid on HRRR's isobaric levels to compute
CAPE/CIN with a virtual-temperature correction. From this we get a parcel
CAPE/CIN profile (per starting level), which drives the effective inflow layer
(Thompson et al. 2007) and, in turn, the effective-layer composites.

Accuracy over memory (per project decision 2026-05-26): this runs eagerly on
numpy arrays with fine moist-adiabat substepping rather than approximating to
fit a smaller RAM budget. Peak RSS is measured and reported.

Validation: surface-parcel CAPE here is compared gridpoint-by-gridpoint against
HRRR's own published SBCAPE, and the most-unstable CAPE against MUCAPE.
"""

from __future__ import annotations

import numpy as np

from . import thermo

# Substeps per ~25 mb layer for the sequential moist-adiabat integration.
# Validated against HRRR SBCAPE: stable from 2 upward (corr 0.981, ratio 1.23).
DEFAULT_MOIST_SUBSTEPS = 3


def _moist_from_lcl(t_lcl_k, p_lcl, p_target, nsub):
    """Integrate the parcel temperature along the moist adiabat from the LCL.

    RK2 (midpoint) over ln(p) from p_lcl (2D) to p_target (scalar level). Only
    meaningful where p_target < p_lcl (above the LCL); callers mask elsewhere.
    """
    lnp0 = np.log(p_lcl)
    dlnp = (np.log(p_target) - lnp0) / nsub
    t = t_lcl_k.astype("float32").copy()
    lnp = lnp0.astype("float32")
    for _ in range(nsub):
        p = np.exp(lnp)
        k1 = thermo.moist_lapse_dlnp(t, p)
        p_mid = np.exp(lnp + dlnp / 2.0)
        k2 = thermo.moist_lapse_dlnp(t + k1 * dlnp / 2.0, p_mid)
        t = t + k2 * dlnp
        lnp = lnp + dlnp
    return t


def lift_parcel_profile(t0_c, td0_c, p0, p_levels, nsub=DEFAULT_MOIST_SUBSTEPS):
    """Parcel temperature (K) and mixing ratio (kg/kg) on ``p_levels``.

    t0_c, td0_c, p0 are 2D parcel-origin fields (degC, degC, hPa); p_levels is a
    1D pressure axis (hPa), surface->top. Levels at/below the origin pressure are
    NaN. Below the LCL the parcel follows the dry adiabat (closed form); above
    it, the moist adiabat is integrated sequentially level-to-level (carrying
    temperature upward), with ``nsub`` substeps per ~25 mb layer. The carry is
    per-gridpoint so columns whose LCLs differ are handled correctly.
    """
    t0_k = t0_c + thermo.KELVIN
    w0 = thermo.mixing_ratio(td0_c, p0)               # conserved below LCL
    t_lcl = thermo.lcl_temperature(t0_c, td0_c)        # K
    p_lcl = p0 * (t_lcl / t0_k) ** (1.0 / thermo.KAPPA)

    nlev = len(p_levels)
    tp = np.full((nlev, *t0_c.shape), np.nan, dtype="float32")
    wp = np.full_like(tp, np.nan)
    # Carry the saturated parcel upward; starts at the LCL, advances per level.
    prev_t = t_lcl.astype("float32")
    prev_p = p_lcl.astype("float32")
    for k, pk in enumerate(p_levels):
        above_ground = pk < p0
        above_lcl = pk < p_lcl
        t_dry = t0_k * (pk / p0) ** thermo.KAPPA
        t_moist = _moist_from_lcl(prev_t, prev_p, float(pk), nsub)
        tk = np.where(above_lcl, t_moist, t_dry)
        tk = np.where(above_ground, tk, np.nan)
        tp[k] = tk
        w_sat = thermo.saturation_mixing_ratio(tk - thermo.KELVIN, pk)
        wp[k] = np.where(above_lcl, w_sat, w0)
        # advance the moist-adiabat carry to this level where we're above the LCL
        advance = above_lcl & above_ground
        prev_t = np.where(advance, tk, prev_t).astype("float32")
        prev_p = np.where(advance, np.float32(pk), prev_p).astype("float32")
    return tp, wp


def _virtual_temperature(t_k, w):
    return t_k * (1.0 + 0.61 * w)


def integrate_cape_cin(tp, wp, te_c, tde_c, z, p_levels):
    """CAPE and CIN (J/kg) from a parcel profile and the environment.

    Virtual-temperature buoyancy, trapezoidally integrated over geopotential
    height. CAPE is total positive buoyant area; CIN is the negative area below
    the level of free convection (first positive-buoyancy level from the bottom).
    Inputs are numpy arrays shaped (nlev, ny, nx); ``z`` is geopotential height
    (m) and ``p_levels`` is the 1D pressure axis (hPa), ordered surface->top.
    """
    te_k = te_c + thermo.KELVIN
    we = thermo.mixing_ratio(tde_c, p_levels[:, None, None])
    tvp = _virtual_temperature(tp, wp)
    tve = _virtual_temperature(te_k, we)
    buoy = thermo.G * (tvp - tve) / tve            # m/s^2, NaN where underground

    nlev = buoy.shape[0]
    valid = np.isfinite(buoy) & np.isfinite(z)
    cape = np.zeros(buoy.shape[1:], dtype="float32")
    cin = np.zeros_like(cape)
    reached_lfc = np.zeros(cape.shape, dtype=bool)

    for k in range(nlev - 1):
        layer = valid[k] & valid[k + 1]
        dz = np.where(layer, z[k + 1] - z[k], 0.0)
        b0 = np.where(layer, np.nan_to_num(buoy[k]), 0.0)
        b1 = np.where(layer, np.nan_to_num(buoy[k + 1]), 0.0)
        mean_b = 0.5 * (b0 + b1)
        contrib = mean_b * dz

        # LFC = first level (from the bottom) with positive buoyancy.
        reached_lfc = reached_lfc | (layer & (buoy[k] > 0))
        cape += np.where(contrib > 0, contrib, 0.0)
        # CIN accrues only below the LFC, where buoyancy is negative.
        cin += np.where((~reached_lfc) & (contrib < 0), contrib, 0.0)

    return cape, cin


# --- per-level parcel profile -> effective inflow layer (Thompson 2007) -----

EFF_CAPE_MIN = 100.0   # J/kg
EFF_CIN_MIN = -250.0   # J/kg
EFF_MAX_START_HPA = 500.0   # don't search for inflow above ~500 mb


def parcel_cape_cin_by_start(te_c, tde_c, z, p_levels, sp, nsub=DEFAULT_MOIST_SUBSTEPS):
    """CAPE/CIN for a parcel lifted from each candidate starting level.

    Returns (cape_prof, cin_prof) shaped (n_start, ny, nx) where n_start covers
    the levels with p >= EFF_MAX_START_HPA that are above ground; non-candidate
    starts are NaN. Inputs are surface->top ordered numpy arrays.
    """
    n = len(p_levels)
    cape_prof = np.full((n, *sp.shape), np.nan, dtype="float32")
    cin_prof = np.full_like(cape_prof, np.nan)
    for i, pi in enumerate(p_levels):
        if pi < EFF_MAX_START_HPA:
            continue
        above = pi < sp                       # this start level is above ground
        p0 = np.full(sp.shape, pi, dtype="float32")
        tp, wp = lift_parcel_profile(te_c[i], tde_c[i], p0, p_levels, nsub=nsub)
        cape_i, cin_i = integrate_cape_cin(tp, wp, te_c, tde_c, z, p_levels)
        cape_prof[i] = np.where(above, cape_i, np.nan)
        cin_prof[i] = np.where(above, cin_i, np.nan)
    return cape_prof, cin_prof


def effective_inflow_layer(cape_prof, cin_prof, z, p_levels):
    """Effective inflow layer base/top heights (m) from the parcel profile.

    A level qualifies when CAPE >= 100 J/kg and CIN >= -250 J/kg; the layer is
    the contiguous qualifying band from the lowest qualifying level upward
    (Thompson et al. 2007). Returns (base_z, top_z, has_eff) as 2D arrays.
    """
    qualifies = (cape_prof >= EFF_CAPE_MIN) & (cin_prof >= EFF_CIN_MIN)
    qualifies = np.nan_to_num(qualifies.astype(float)).astype(bool)
    ever = np.cumsum(qualifies, axis=0) > 0                 # at/above the base
    broke = np.cumsum((ever & ~qualifies).astype(int), axis=0) > 0
    active = qualifies & ~broke                             # contiguous band

    has_eff = active.any(axis=0)
    base_idx = np.argmax(active, axis=0)
    top_idx = active.shape[0] - 1 - np.argmax(active[::-1], axis=0)
    base_z = np.take_along_axis(z, base_idx[None], 0)[0]
    top_z = np.take_along_axis(z, top_idx[None], 0)[0]
    base_z = np.where(has_eff, base_z, np.nan)
    top_z = np.where(has_eff, top_z, np.nan)
    return base_z, top_z, active, has_eff


# --- vertical interpolation + shear/helicity over the effective layer -------

def interp_to_height(field, z, target_z):
    """Linear interpolation of a (nlev, ny, nx) field to a 2D target height (m).

    Assumes ``z`` increases with level index (surface->top order).
    """
    nlev = z.shape[0]
    below = np.nan_to_num(z, nan=-1e30) <= target_z[None]
    idx = np.clip(below.sum(axis=0) - 1, 0, nlev - 2)
    z0 = np.take_along_axis(z, idx[None], 0)[0]
    z1 = np.take_along_axis(z, (idx + 1)[None], 0)[0]
    f0 = np.take_along_axis(field, idx[None], 0)[0]
    f1 = np.take_along_axis(field, (idx + 1)[None], 0)[0]
    frac = np.clip((target_z - z0) / (z1 - z0), 0.0, 1.0)
    return f0 + frac * (f1 - f0)


def effective_srh(u_ms, v_ms, cu_ms, cv_ms, active):
    """Storm-relative helicity (m2/s2) integrated over the effective layer.

    Discrete SRH = sum_k [(u_{k+1}-c_u)(v_k-c_v) - (u_k-c_u)(v_{k+1}-c_v)] over
    layers where both bounding levels lie in the effective inflow layer.
    """
    esrh = np.zeros(u_ms.shape[1:], dtype="float32")
    for k in range(u_ms.shape[0] - 1):
        layer = active[k] & active[k + 1]
        du0, dv0 = u_ms[k] - cu_ms, v_ms[k] - cv_ms
        du1, dv1 = u_ms[k + 1] - cu_ms, v_ms[k + 1] - cv_ms
        incr = du1 * dv0 - du0 * dv1
        esrh += np.where(layer, np.nan_to_num(incr), 0.0)
    return esrh


def effective_bulk_shear(u_ms, v_ms, z, base_z, el_z):
    """Effective bulk wind difference (m/s): |V(0.5*EL) - V(base)| (SPC EBWD)."""
    top_z = base_z + 0.5 * (el_z - base_z)   # midpoint between base and EL
    u_base = interp_to_height(u_ms, z, base_z)
    v_base = interp_to_height(v_ms, z, base_z)
    u_top = interp_to_height(u_ms, z, top_z)
    v_top = interp_to_height(v_ms, z, top_z)
    return np.hypot(u_top - u_base, v_top - v_base)


# --- DCAPE ------------------------------------------------------------------

DCAPE_SEARCH_HPA = 400.0   # search the lowest 400 mb for the min-theta-e source


def dcape(te_c, tde_c, z, p_levels, sp, nsub=6):
    """Downdraft CAPE (J/kg): saturated descent from the min-theta-e level.

    Finds the minimum equivalent potential temperature in the lowest ~400 mb,
    descends a saturated parcel from there to the ground along the moist adiabat,
    and integrates the (positive) downdraft buoyancy (environment warmer than the
    descending parcel) between the source level and the surface.
    """
    n = len(p_levels)
    theta_e = thermo.equivalent_potential_temperature(te_c, tde_c, p_levels[:, None, None])
    search = (p_levels[:, None, None] >= sp[None] - DCAPE_SEARCH_HPA) & \
             (p_levels[:, None, None] < sp[None])
    src = np.argmin(np.where(search, theta_e, np.inf), axis=0)   # min-theta-e level index

    t_src = np.take_along_axis(te_c, src[None], 0)[0]
    p_src = p_levels[src]                                        # 2D
    t_lcl = t_src + thermo.KELVIN                                # parcel saturated at source

    # Descending-parcel temperature at every level (moist adiabat from source).
    tpar = np.full((n, *sp.shape), np.nan, dtype="float32")
    for k in range(n):
        tpar[k] = _moist_from_lcl(t_lcl, p_src, float(p_levels[k]), nsub)
    te_k = te_c + thermo.KELVIN
    buoy = thermo.G * (te_k - tpar) / te_k                       # >0 where env warmer

    in_col = (p_levels[:, None, None] > p_src[None]) & (p_levels[:, None, None] < sp[None])
    dd = np.zeros(sp.shape, dtype="float32")
    for k in range(n - 1):
        layer = in_col[k] & in_col[k + 1] & np.isfinite(z[k]) & np.isfinite(z[k + 1])
        dz = np.where(layer, np.abs(z[k] - z[k + 1]), 0.0)
        b = 0.5 * (np.nan_to_num(buoy[k]) + np.nan_to_num(buoy[k + 1]))
        dd += np.where(layer, np.maximum(b, 0.0) * dz, 0.0)
    return dd
