"""Parameter metadata catalog.

Maps each ``parameter_id`` (the stable identifier presets and the frontend use)
to how it is selected from the GRIB (cfgrib ``filter_by_keys``), its
``temporal_type`` (spec 5.2), operational output units + conversion, parameter
class (continuous/categorical, spec 5.3), and UI visibility (spec 4.4).

Selection keys were derived from a real archived HRRR ``wrfprsf`` file. Two
selection styles appear:
  * by ``shortName`` for fields eccodes names natively;
  * by GRIB2 triplet (``discipline``/``parameterCategory``/``parameterNumber``)
    for fields eccodes reports as shortName ``unknown`` because the NCEP local
    tables aren't loaded (e.g. updraft helicity, echo top, max reflectivity,
    near-surface smoke, max up/downdraft).

COVERAGE (v0.1): this is a verified CORE of the native fields in spec 4.1 —
every structural case is represented and exercised end-to-end. The long tail of
spec 4.1 (full radiation suite, all soil levels, every precip-type accumulation,
brightness temps) and ALL derived composites in spec 4.3 are NOT yet here; they
are mechanical additions following the patterns below, pending validation
against a full cycle. Derived parameters from spec 4.2 are added by
``compute.derivations`` rather than listed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TemporalType = Literal[
    "instantaneous", "hourly_max", "hourly_min", "hourly_average", "accumulation"
]
ParameterClass = Literal["continuous", "categorical"]


@dataclass(frozen=True)
class Parameter:
    """One selectable parameter and how it maps to HRRR GRIB data."""

    parameter_id: str
    filters: dict          # cfgrib backend filter_by_keys; selects exactly one field
    out_units: str         # operational units written to Zarr
    description: str
    temporal_type: TemporalType = "instantaneous"
    convert: str | None = None       # key into compute.units.CONVERTERS
    is_3d: bool = False              # carries an isobaricInhPa dimension
    param_class: ParameterClass = "continuous"
    ui_visible: bool = True          # spec 4.4: some fields kept in data, hidden in UI
    # In-band fill values (in native GRIB units) that mean "no data" and must be
    # masked to NaN so the UI never shows them. cfgrib already masks bitmapped
    # missing values; this is only for sentinels encoded as real numbers (e.g.
    # echo top's -999 = "no echo"). Verify against real data before adding.
    missing_values: tuple[float, ...] = ()
    tags: tuple[str, ...] = field(default_factory=tuple)


def _p(parameter_id: str, filters: dict, out_units: str, description: str, **kw) -> Parameter:
    return Parameter(parameter_id, filters, out_units, description, **kw)


# Convenience filter builders -------------------------------------------------

def _sn(short_name: str, type_of_level: str, *, step="instant", **extra) -> dict:
    return {"shortName": short_name, "typeOfLevel": type_of_level, "stepType": step, **extra}


def _triplet(disc: int, cat: int, num: int, type_of_level: str, *, step="instant", **extra) -> dict:
    return {
        "discipline": disc, "parameterCategory": cat, "parameterNumber": num,
        "typeOfLevel": type_of_level, "stepType": step, **extra,
    }


# Parameters ------------------------------------------------------------------

_PARAMETERS: list[Parameter] = [
    # --- parcel thermodynamics (spec 4.1) ---
    _p("sbcape", _sn("cape", "surface"), "J/kg", "Surface-based CAPE", tags=("thermo",)),
    _p("sbcin", _sn("cin", "surface"), "J/kg", "Surface-based CIN", tags=("thermo",)),
    _p("mlcape_180mb", _sn("cape", "pressureFromGroundLayer", level=18000), "J/kg",
       "Mean-layer CAPE (180 mb)", tags=("thermo",)),
    _p("mlcin_180mb", _sn("cin", "pressureFromGroundLayer", level=18000), "J/kg",
       "Mean-layer CIN (180 mb)", tags=("thermo",)),
    _p("mlcape_90mb", _sn("cape", "pressureFromGroundLayer", level=9000), "J/kg",
       "Mean-layer CAPE (90 mb)", tags=("thermo",)),
    _p("mlcin_90mb", _sn("cin", "pressureFromGroundLayer", level=9000), "J/kg",
       "Mean-layer CIN (90 mb)", tags=("thermo",)),
    _p("mucape", _sn("cape", "pressureFromGroundLayer", level=25500), "J/kg",
       "Most-unstable CAPE", tags=("thermo",)),
    _p("mucin", _sn("cin", "pressureFromGroundLayer", level=25500), "J/kg",
       "Most-unstable CIN", tags=("thermo",)),
    _p("mu_parcel_pressure", _sn("plpl", "pressureFromGroundLayer", level=25500), "hPa",
       "MU parcel source pressure", convert="Pa_to_hPa"),
    _p("pbl_height", _sn("blh", "surface"), "m", "PBL / mixing height"),

    # --- kinematic composites (spec 4.1) ---
    _p("srh_0_1km", _sn("hlcy", "heightAboveGroundLayer", level=1000), "m2/s2",
       "0-1 km storm relative helicity", tags=("kinematic",)),
    _p("srh_0_3km", _sn("hlcy", "heightAboveGroundLayer", level=3000), "m2/s2",
       "0-3 km storm relative helicity", tags=("kinematic",)),
    # storm motion + shear components are hidden; speed/dir/magnitude are derived (4.2)
    _p("ustm_0_6km", _sn("ustm", "heightAboveGroundLayer", level=0), "kt",
       "Storm motion U (0-6 km)", convert="ms_to_kt", ui_visible=False),
    _p("vstm_0_6km", _sn("vstm", "heightAboveGroundLayer", level=0), "kt",
       "Storm motion V (0-6 km)", convert="ms_to_kt", ui_visible=False),
    _p("vucsh_0_1km", _sn("vucsh", "heightAboveGroundLayer", scaledValueOfSecondFixedSurface=1000),
       "kt", "0-1 km bulk shear U", convert="ms_to_kt", ui_visible=False),
    _p("vvcsh_0_1km", _sn("vvcsh", "heightAboveGroundLayer", scaledValueOfSecondFixedSurface=1000),
       "kt", "0-1 km bulk shear V", convert="ms_to_kt", ui_visible=False),
    _p("vucsh_0_6km", _sn("vucsh", "heightAboveGroundLayer", scaledValueOfSecondFixedSurface=6000),
       "kt", "0-6 km bulk shear U", convert="ms_to_kt", ui_visible=False),
    _p("vvcsh_0_6km", _sn("vvcsh", "heightAboveGroundLayer", scaledValueOfSecondFixedSurface=6000),
       "kt", "0-6 km bulk shear V", convert="ms_to_kt", ui_visible=False),

    # --- convective diagnostics (mix of shortName + triplet) ---
    _p("refc", _sn("refc", "atmosphere"), "dBZ", "Composite reflectivity", tags=("radar",)),
    _p("retop", _triplet(0, 16, 3, "cloudTop"), "m", "Echo top",
       missing_values=(-999.0,), tags=("radar",)),  # -999 = no echo (no GRIB bitmap)
    _p("vil", _sn("veril", "atmosphere"), "kg/m2", "Vertically integrated liquid", tags=("radar",)),
    _p("refd_1km", _sn("refd", "heightAboveGround", level=1000), "dBZ", "1 km AGL reflectivity"),
    _p("refd_4km", _sn("refd", "heightAboveGround", level=4000), "dBZ", "4 km AGL reflectivity"),
    _p("refd_m10c", _sn("refd", "isothermal", level=263), "dBZ", "-10C reflectivity"),
    _p("maxref_1km", _triplet(0, 16, 198, "heightAboveGround", step="max", level=1000), "dBZ",
       "Hourly max 1 km reflectivity", temporal_type="hourly_max"),
    _p("uh_2to5km", _triplet(0, 7, 199, "heightAboveGroundLayer", step="max", level=5000), "m2/s2",
       "2-5 km updraft helicity (max)", temporal_type="hourly_max", tags=("supercell",)),
    _p("uh_0to3km", _triplet(0, 7, 199, "heightAboveGroundLayer", step="max", level=3000), "m2/s2",
       "0-3 km updraft helicity (max)", temporal_type="hourly_max"),
    _p("uh_0to2km", _triplet(0, 7, 199, "heightAboveGroundLayer", step="max", level=2000), "m2/s2",
       "0-2 km updraft helicity (max)", temporal_type="hourly_max"),
    _p("min_uh_2to5km", _triplet(0, 7, 200, "heightAboveGroundLayer", step="min", level=5000),
       "m2/s2", "2-5 km min UH", temporal_type="hourly_min"),
    _p("max_updraft", _triplet(0, 2, 220, "pressureFromGroundLayer", step="max", level=10000),
       "m/s", "Max updraft (100-1000 mb)", temporal_type="hourly_max"),
    _p("max_downdraft", _triplet(0, 2, 221, "pressureFromGroundLayer", step="max", level=10000),
       "m/s", "Max downdraft (100-1000 mb)", temporal_type="hourly_max"),
    _p("relv_0to2km", _sn("max_vo", "heightAboveGroundLayer", step="max", level=2000), "1/s",
       "0-2 km relative vorticity (max)", temporal_type="hourly_max"),
    _p("relv_0to1km", _sn("max_vo", "heightAboveGroundLayer", step="max", level=1000), "1/s",
       "0-1 km relative vorticity (max)", temporal_type="hourly_max"),
    _p("lightning", _sn("ltng", "atmosphere"), "dimensionless", "Lightning flash rate"),
    _p("max_hail_col", _sn("hail", "atmosphere", step="max"), "m",
       "Max column hail diameter", temporal_type="hourly_max"),
    _p("max_wind_10m", _sn("max_10si", "heightAboveGround", step="max", level=10), "kt",
       "10 m max wind speed", temporal_type="hourly_max", convert="ms_to_kt"),

    # --- surface / near-surface (spec 4.1) ---
    _p("mslp", _sn("mslma", "meanSea"), "hPa", "MSLP (MAPS reduction)", convert="Pa_to_hPa"),
    _p("surface_pressure", _sn("sp", "surface"), "hPa", "Surface pressure", convert="Pa_to_hPa"),
    _p("t2m", _sn("2t", "heightAboveGround", level=2), "degC", "2 m temperature",
       convert="K_to_degC"),
    _p("d2m", _sn("2d", "heightAboveGround", level=2), "degC", "2 m dewpoint", convert="K_to_degC"),
    _p("rh2m", _sn("2r", "heightAboveGround", level=2), "%", "2 m relative humidity"),
    _p("sh2m", _sn("2sh", "heightAboveGround", level=2), "kg/kg", "2 m specific humidity"),
    _p("theta2m", _sn("pt", "heightAboveGround", level=2), "K", "2 m potential temperature"),
    _p("gust", _sn("gust", "surface"), "kt", "Surface wind gust", convert="ms_to_kt"),
    _p("visibility", _sn("vis", "surface"), "m", "Surface visibility"),
    _p("orography", _sn("orog", "surface"), "m", "Model terrain height (AGL conversions)",
       ui_visible=False),
    _p("u10", _sn("10u", "heightAboveGround", level=10), "kt", "10 m U wind",
       convert="ms_to_kt", ui_visible=False),
    _p("v10", _sn("10v", "heightAboveGround", level=10), "kt", "10 m V wind",
       convert="ms_to_kt", ui_visible=False),
    _p("u80", _sn("u", "heightAboveGround", level=80), "kt", "80 m U wind",
       convert="ms_to_kt", ui_visible=False),
    _p("v80", _sn("v", "heightAboveGround", level=80), "kt", "80 m V wind",
       convert="ms_to_kt", ui_visible=False),

    # --- moisture (spec 4.1) ---
    _p("pwat", _sn("pwat", "atmosphereSingleLayer"), "kg/m2", "Precipitable water"),
    _p("rhpw", _triplet(0, 1, 242, "atmosphere"), "%", "RH with respect to PWAT"),

    # --- levels & heights (spec 4.1) ---
    _p("freezing_level", _sn("gh", "isothermZero"), "m", "0C isotherm height", convert="gpm_to_m"),
    _p("lcl_height", _sn("gh", "adiabaticCondensation"), "m",
       "LCL height (surface parcel)", convert="gpm_to_m"),
    _p("el_height", _sn("gh", "equilibrium"), "m", "Equilibrium level height", convert="gpm_to_m"),
    _p("hgt_m10c", _sn("gh", "isothermal", level=263), "m", "-10C height", convert="gpm_to_m"),
    _p("hgt_m20c", _sn("gh", "isothermal", level=253), "m", "-20C height", convert="gpm_to_m"),

    # --- categorical precip (spec 5.3): operator restricted to ==0/==1 in UI ---
    _p("cat_snow", _sn("csnow", "surface"), "0/1", "Categorical snow (dominant)",
       param_class="categorical"),
    _p("cat_icep", _sn("cicep", "surface"), "0/1", "Categorical ice pellets (dominant)",
       param_class="categorical"),
    _p("cat_frzr", _sn("cfrzr", "surface"), "0/1", "Categorical freezing rain (dominant)",
       param_class="categorical"),
    _p("cat_rain", _sn("crain", "surface"), "0/1", "Categorical rain (dominant)",
       param_class="categorical"),

    # --- levels & heights: freezing-level variants, cloud heights, DGZ ---
    _p("freezing_level_pressure", _sn("pres", "isothermZero"), "hPa", "0C isotherm pressure",
       convert="Pa_to_hPa"),
    _p("freezing_level_rh", _sn("r", "isothermZero"), "%", "0C isotherm RH"),
    _p("highest_freezing_level", _sn("gh", "highestTroposphericFreezing"), "m",
       "Highest freezing level height", convert="gpm_to_m"),
    _p("dgz_thickness", {"shortName": "layth", "stepType": "instant"}, "m",
       "DGZ layer thickness (-12 to -17C)"),
    _p("cloud_ceiling", _sn("gh", "cloudCeiling"), "m", "Cloud ceiling height", convert="gpm_to_m"),
    _p("cloud_base_height", _sn("gh", "cloudBase"), "m", "Cloud base height", convert="gpm_to_m"),
    _p("cloud_top_height", _sn("gh", "cloudTop"), "m", "Cloud top height", convert="gpm_to_m"),
    _p("cloud_base_pressure", _sn("pcdb", "cloudBase"), "hPa", "Cloud base pressure",
       convert="Pa_to_hPa"),
    _p("cloud_top_pressure", _sn("pres", "cloudTop"), "hPa", "Cloud top pressure",
       convert="Pa_to_hPa"),

    # --- additional surface / moisture (spec 4.1) ---
    _p("surface_roughness", _sn("fsr", "surface"), "m", "Surface roughness", ui_visible=False),
    _p("friction_velocity", _sn("fricv", "surface"), "m/s", "Friction velocity", ui_visible=False),
    _p("moisture_availability", _sn("mstav", "depthBelowLand"), "%", "Moisture availability"),

    # --- cloud cover (spec 4.1) ---
    _p("total_cloud_cover", _sn("tcc", "atmosphere"), "%", "Total cloud cover"),
    _p("low_cloud_cover", _sn("lcc", "lowCloudLayer"), "%", "Low cloud cover"),
    _p("mid_cloud_cover", _sn("mcc", "middleCloudLayer"), "%", "Middle cloud cover"),
    _p("high_cloud_cover", _sn("hcc", "highCloudLayer"), "%", "High cloud cover"),

    # --- radiation (spec 4.1), W/m2 ---
    _p("dswrf_sfc", _sn("sdswrf", "surface"), "W/m2", "Surface downward shortwave"),
    _p("uswrf_sfc", _sn("suswrf", "surface"), "W/m2", "Surface upward shortwave"),
    _p("dlwrf_sfc", _sn("sdlwrf", "surface"), "W/m2", "Surface downward longwave"),
    _p("ulwrf_sfc", _sn("sulwrf", "surface"), "W/m2", "Surface upward longwave"),
    _p("cfnsf", _sn("cfnsf", "surface"), "W/m2", "Cloud forcing net solar flux"),
    _p("vbdsf", _sn("vbdsf", "surface"), "W/m2", "Visible beam downward solar flux"),
    _p("vddsf", _sn("vddsf", "surface"), "W/m2", "Visible diffuse downward solar flux"),
    _p("sensible_heat_flux", _sn("ishf", "surface"), "W/m2", "Sensible heat net flux"),
    _p("latent_heat_flux", _sn("slhtf", "surface"), "W/m2", "Latent heat net flux"),
    _p("ground_heat_flux", _sn("gflux", "surface"), "W/m2", "Ground heat flux"),
    _p("ulwrf_toa", _triplet(0, 5, 4, "nominalTop"), "W/m2",
       "TOA upward longwave (cloud-top temp proxy)"),
    _p("uswrf_toa", _triplet(0, 4, 8, "nominalTop"), "W/m2", "TOA upward shortwave"),

    # --- precipitation (spec 4.1) ---
    _p("precip_total", _sn("tp", "surface", step="accum"), "mm", "Total precipitation",
       temporal_type="accumulation"),
    _p("precip_rate", _sn("prate", "surface"), "kg/m2/s", "Precipitation rate"),
    _p("snow_water_eq", _sn("sdwe", "surface", step="accum"), "mm", "Snow water equivalent accum",
       temporal_type="accumulation"),
    _p("snow_depth", _sn("sde", "surface"), "m", "Snow depth"),
    _p("snow_cover", _sn("snowc", "surface"), "%", "Snow cover percentage"),
    _p("pct_frozen_precip", _sn("cpofp", "surface"), "%", "Percent frozen precipitation"),
    _p("freezing_rain_accum", _sn("frzr", "surface", step="accum"), "mm", "Freezing rain accum",
       temporal_type="accumulation"),
    _p("frozen_rain_accum", _triplet(0, 1, 227, "surface", step="accum"), "mm",
       "Frozen rain accumulation (FROZR)", temporal_type="accumulation"),
    _p("snowfall_total", _triplet(0, 1, 29, "surface", step="accum"), "m", "Total snowfall (ASNOW)",
       temporal_type="accumulation"),
    _p("storm_surface_runoff", _sn("ssrun", "surface", step="accum"), "kg/m2",
       "Storm surface runoff", temporal_type="accumulation", ui_visible=False),
    _p("baseflow_runoff", _sn("bgrun", "surface", step="accum"), "kg/m2", "Baseflow runoff",
       temporal_type="accumulation", ui_visible=False),

    # --- smoke & aerosols (spec 4.1; GRIB-triplet selected) ---
    _p("smoke_near_surface", _triplet(0, 20, 0, "heightAboveGround", level=8), "kg/m3",
       "Near-surface smoke concentration (8 m)", tags=("aerosol",)),
    _p("aod", _triplet(0, 20, 102, "atmosphereSingleLayer"), "dimensionless",
       "Aerosol optical thickness", tags=("aerosol",)),
    _p("column_mass_density", _triplet(0, 20, 1, "atmosphereSingleLayer"), "kg/m2",
       "Column-integrated mass density", tags=("aerosol",)),

    # --- soil (spec 4.1; depthBelowLandLayer keyed by depth in cm) ---
    #     spec 4.4: hide layers below 0.3 m (>30 cm) from the default UI.
    _p("soil_temp_0cm", _sn("st", "depthBelowLandLayer", scaledValueOfFirstFixedSurface=0),
       "K", "Soil temperature 0 cm", tags=("soil",)),
    _p("soil_temp_10cm", _sn("st", "depthBelowLandLayer", scaledValueOfFirstFixedSurface=10),
       "K", "Soil temperature 10 cm", tags=("soil",)),
    _p("soil_temp_30cm", _sn("st", "depthBelowLandLayer", scaledValueOfFirstFixedSurface=30),
       "K", "Soil temperature 30 cm", tags=("soil",)),
    _p("soil_temp_100cm", _sn("st", "depthBelowLandLayer", scaledValueOfFirstFixedSurface=100),
       "K", "Soil temperature 100 cm", ui_visible=False, tags=("soil",)),
    _p("soil_moist_0cm", _sn("soilw", "depthBelowLandLayer", scaledValueOfFirstFixedSurface=0),
       "fraction", "Volumetric soil moisture 0 cm", tags=("soil",)),
    _p("soil_moist_10cm", _sn("soilw", "depthBelowLandLayer", scaledValueOfFirstFixedSurface=10),
       "fraction", "Volumetric soil moisture 10 cm", tags=("soil",)),
    _p("soil_moist_30cm", _sn("soilw", "depthBelowLandLayer", scaledValueOfFirstFixedSurface=30),
       "fraction", "Volumetric soil moisture 30 cm", tags=("soil",)),
    _p("soil_moist_100cm", _sn("soilw", "depthBelowLandLayer", scaledValueOfFirstFixedSurface=100),
       "fraction", "Volumetric soil moisture 100 cm", ui_visible=False, tags=("soil",)),

    # --- pressure-level 3D fields (spec 4.1): 50-1013 mb every 25 mb ---
    _p("hgt_pressure", _sn("gh", "isobaricInhPa"), "m", "Geopotential height (3D)",
       is_3d=True, convert="gpm_to_m"),
    _p("t_pressure", _sn("t", "isobaricInhPa"), "degC", "Temperature (3D)",
       is_3d=True, convert="K_to_degC"),
    _p("rh_pressure", _sn("r", "isobaricInhPa"), "%", "Relative humidity (3D)", is_3d=True),
    _p("dpt_pressure", _sn("dpt", "isobaricInhPa"), "degC", "Dewpoint (3D)",
       is_3d=True, convert="K_to_degC"),
    _p("spfh_pressure", _sn("q", "isobaricInhPa"), "kg/kg", "Specific humidity (3D)", is_3d=True),
    _p("vvel_pressure", _sn("w", "isobaricInhPa"), "Pa/s", "Vertical velocity (3D)", is_3d=True),
    _p("absv_pressure", _sn("absv", "isobaricInhPa"), "1/s", "Absolute vorticity (3D)", is_3d=True),
    # raw 3D winds hidden; speed/dir derived (spec 4.2 / 5.1)
    _p("u_pressure", _sn("u", "isobaricInhPa"), "kt", "U wind (3D)",
       is_3d=True, convert="ms_to_kt", ui_visible=False),
    _p("v_pressure", _sn("v", "isobaricInhPa"), "kt", "V wind (3D)",
       is_3d=True, convert="ms_to_kt", ui_visible=False),
]

CATALOG: dict[str, Parameter] = {p.parameter_id: p for p in _PARAMETERS}


def native_parameters() -> list[Parameter]:
    """All catalog parameters (native fields read directly from GRIB)."""
    return list(_PARAMETERS)


# UI category for grouping the (120+) parameters in the frontend dropdown. Keyed
# by parameter_id (covers native + derived + composite ids). Composites are all
# bucketed under "Composite indices" via the source check in category_for.
_LEVELS_HEIGHTS = {
    "freezing_level", "freezing_level_pressure", "freezing_level_rh",
    "highest_freezing_level", "lcl_height", "el_height", "hgt_m10c", "hgt_m20c",
    "dgz_thickness", "cloud_ceiling", "cloud_base_height", "cloud_top_height",
    "cloud_base_pressure", "cloud_top_pressure", "hail_growth_zone_depth",
    "warm_cloud_depth",
}
_CONVECTIVE = {
    "refc", "retop", "vil", "refd_1km", "refd_4km", "refd_m10c", "maxref_1km",
    "uh_2to5km", "uh_0to3km", "uh_0to2km", "min_uh_2to5km", "max_updraft",
    "max_downdraft", "relv_0to2km", "relv_0to1km", "lightning", "max_hail_col",
}
_SURFACE = {"mslp", "surface_pressure", "visibility", "surface_roughness", "friction_velocity"}
_PRESSURE_3D = {
    "hgt_pressure", "t_pressure", "rh_pressure", "dpt_pressure", "spfh_pressure",
    "vvel_pressure", "absv_pressure", "u_pressure", "v_pressure",
    "wind_speed_pressure", "wind_dir_pressure", "dewpoint_depression_pressure",
}


def category_for(parameter_id: str, source: str = "hrrr") -> str:
    """UI category for a parameter (for grouping the dropdown). First match wins."""
    p = parameter_id
    if source == "composite":
        return "Composite indices"
    if p in _PRESSURE_3D:
        return "Pressure levels (3D)"
    if p in _LEVELS_HEIGHTS:
        return "Levels & heights"
    if p.startswith("soil_"):
        return "Soil"
    if p in ("smoke_near_surface", "aod", "column_mass_density"):
        return "Smoke & aerosols"
    if (p.endswith("rf_sfc") or p.endswith("rf_toa") or p.endswith("_heat_flux")
            or p in ("cfnsf", "vbdsf", "vddsf")):
        return "Radiation"
    if p.endswith("cloud_cover"):
        return "Cloud cover"
    if (p.startswith(("precip", "snow", "cat_")) or p.endswith(("_rain_accum", "_runoff"))
            or p in ("pct_frozen_precip", "snowfall_total")):
        return "Precipitation"
    if p in _CONVECTIVE:
        return "Convective / radar"
    if "cape" in p or "cin" in p or p in ("mu_parcel_pressure", "pbl_height"):
        return "Instability (CAPE/CIN)"
    if (p.startswith(("srh", "shear", "storm_motion", "wind_speed", "wind_dir",
                      "ustm", "vstm", "vucsh", "vvcsh", "u10", "v10", "u80", "v80", "max_wind"))
            or p == "gust"):
        return "Kinematic (wind/shear)"
    if (p in ("pwat", "rhpw", "rh2m", "sh2m", "d2m", "moisture_availability")
            or p.startswith(("dewpoint_depression", "mean_rh", "theta_e", "wet_bulb"))):
        return "Moisture"
    if p in ("t2m", "theta2m") or p.startswith("lapse_"):
        return "Temperature"
    if p in _SURFACE:
        return "Surface"
    return "Other"
