# HRRR Threshold Masking Tool — Project Specification

**Status:** Living document. Section 12 logs locked decisions chronologically.
**Last updated:** 2026-05-26

## 1. Overview

A forecaster-facing web application that ingests HRRR forecast data hourly and lets users define joint threshold conditions across multiple atmospheric parameters, then visualizes where those conditions are jointly met across CONUS at any forecast hour.

The tool operationalizes the mental overlay forecasters perform constantly when looking at fields like CAPE, shear, moisture, and cap erosion side by side. Existing operational products show individual fields. Composite parameters like STP are hand-tuned and inflexible. NBM gives calibrated guidance but doesn't expose ingredient overlap. The gap this tool fills is a user-configurable multi-parameter ingredient overlap tool with the forecaster's own thresholds.

The single distinguishing feature is the count-and-click model: per grid cell, count how many of the user's N thresholds are satisfied, render that count as nested contours, and on click show a side panel with each parameter's value, threshold, pass/fail status, and identification of the limiting parameter. This produces automatic driver decomposition for whatever bundle the forecaster has defined.

The same machinery serves severe weather, fire weather, winter weather, aviation, and any other domain with multi-ingredient operational decisions.

## 2. Architecture

The system has three components: an ingest worker (compute), object storage (data), and a frontend (presentation). These are decoupled so any one can be replaced without disturbing the others.

```
[HRRR S3 bucket]   →   [Ingest worker]   →   [Cloudflare R2]   →   [Cloudflare Pages]   →   [users]
   us-east-1            Hetzner CX32          object storage          static frontend
                        Ashburn VA            stable URL              stable URL
```

### 2.1 Compute (ingest worker)

- **Host:** Hetzner CX32 (8 GB RAM, 2 shared vCPUs, 80 GB SSD) in Ashburn, Virginia datacenter.
- **Cost:** approximately €8.21/month.
- **Cadence:** hourly cron at HRRR init + 65 minutes.
- **Parallelism:** two-worker parallel ingest across forecast hours.
- **Memory discipline:** dask chunking on the isobaric level axis (`chunks={'isobaricInhPa': 10}`) is mandatory. Without it, peak RSS exceeds available RAM. With it, expected peak is ~3 GB per worker.
- **Containerization:** Docker via plain `docker compose` on the VPS.
- **Operating system:** Ubuntu Server 24.04 LTS (or current LTS).

### 2.2 Storage (object storage layer)

- **Service:** Cloudflare R2.
- **Free tier:** 10 GB storage, no egress fees ever.
- **Expected usage:** 3-8 GB per cycle (compressed Zarr). One cycle retained at a time; previous cycle overwritten atomically.
- **Egress economics:** R2 has no egress charges. Frontend reads, user downloads, and inter-region replication are all free outbound. This is the architectural pivot that makes the whole system economical.
- **Atomic update pattern:** ingest writes new cycle data to a fresh path (e.g., `cycles/2026052400/`), validates completeness, updates `manifest.json` to point at the new cycle, then deletes the previous cycle's path. Frontend either gets the old cycle or the new cycle, never partial data.

### 2.3 Serving (frontend)

- **Host:** Cloudflare Pages (static hosting, free tier sufficient for expected traffic).
- **Frontend stack:** vanilla JS or lightweight React. Reads Zarr chunks directly from R2 via the R2 public bucket URL.
- **Storage origin:** the frontend has no backend. It is a static site that reads object storage. There is no API server, no FastAPI, no Node service. Cloudflare Pages + R2 covers everything.

### 2.4 Mask computation

All threshold masking runs client-side. The frontend downloads only the parameter arrays the user has selected, evaluates boolean threshold tests in the browser, sums to produce the count field, and renders as a raster overlay on the map. Click-to-inspect is an O(N) lookup in already-loaded arrays. No server-side computation per user action.

### 2.5 Monitoring

- Ingest writes a structured log file on the VPS.
- Pushover notifications on ingest failure, reusing existing Pushover infrastructure for severe weather notifications.
- Frontend exception monitoring is out of scope for v1.

## 3. Data Pipeline

### 3.1 Source

- **Bucket:** `noaa-hrrr-bdp-pds` on AWS S3, us-east-1 region.
- **License:** NOAA Open Data Sponsorship Program. No egress charges to consumers.
- **File targeted:** `hrrr.<YYYYMMDD>/conus/hrrr.t<HH>z.wrfprsf<FF>.grib2`.
- **Coverage:** every cycle hour (00-23Z), with all forecast hours within the cycle.
- **Cycle types:**
  - Standard cycles (20/day): f00 through f18 (19 forecast hours).
  - Extended cycles (4/day at 00, 06, 12, 18Z): f00 through f48 (49 forecast hours).

### 3.2 Steps per cycle

1. Cron triggers at cycle init + 65 minutes (HRRR data typically becomes available 50-60 minutes post-init).
2. For each forecast hour in the cycle (parallel across two workers):
   1. Fetch `wrfprsf<FF>.grib2` from the NOAA S3 bucket via HTTPS.
   2. Subset the GRIB2 file using wgrib2's `-not_match` to exclude only the five hydrometeor mixing ratios.
   3. Open with cfgrib through xarray, using dask chunking on the isobaric level axis.
   4. Compute trivial derivations (Section 4.2) and composite indices (Section 4.3).
   5. Apply unit conversions to operational units before writing.
   6. Write to a per-forecast-hour Zarr group inside the cycle's path.
3. Once all forecast hours are written, the worker validates that the cycle is complete and writes the cycle manifest.
4. Update the global `manifest.json` to point at the new cycle (atomic rename).
5. Delete the previous cycle's data from R2.

### 3.3 Memory discipline

Mandatory practices for fitting in 8 GB:

- Open xarray datasets with explicit chunking: `chunks={'isobaricInhPa': 10}` for isobaric data; appropriate chunking for other type-of-level groups.
- Use `del` and `gc.collect()` between large intermediate arrays in compute steps.
- Process derived parameter families sequentially, not in parallel within a single worker.
- Stream Zarr writes per variable rather than holding the full output dataset in memory before flushing.
- Profile peak RSS during early development with the existing `vps_sizing_benchmark.py` as a reference point.

### 3.4 wgrib2 vs cfgrib filtering

Production runs on Linux where `wgrib2` is straightforward to install via the source build or via the eccodes alternative. Local Mac development uses cfgrib's in-memory filtering as a fallback because wgrib2 isn't in Homebrew core. The ingest code should detect wgrib2 availability and fall back to cfgrib-level filtering with a logged warning. Both paths produce equivalent datasets; the disk-subset path is preferred when available because it reduces peak memory.

### 3.5 Zarr compression

Use explicit Zarr 3 codecs, not numcodecs `Blosc` (which is incompatible with Zarr 3 and produces a type error). The current Zarr 3 syntax for Blosc-Zstd compression:

```python
from zarr.codecs import BloscCodec, BloscShuffle

compressor = BloscCodec(
    cname='zstd',
    clevel=5,
    shuffle=BloscShuffle.shuffle,
)
encoding = {var: {'compressors': (compressor,)} for var in ds.data_vars}
ds.to_zarr(path, mode='w', encoding=encoding)
```

For additional compression on float32 data that originated from GRIB's 16-bit packed representation, layer `numcodecs.Quantize` before the Blosc codec to recover the packing compactness without losing operational precision. Defer Quantize tuning until after v1 ships and storage costs are observed.

## 4. Parameter Catalog

### 4.1 Native HRRR fields (post-subset)

#### Convective composite diagnostics

| Field | HRRR ID | Level | Temporal | Notes |
|---|---|---|---|---|
| Composite reflectivity | REFC | entire atm | instantaneous | |
| Echo top | RETOP | cloud top | instantaneous | |
| Vertically integrated liquid | VIL | entire atm | instantaneous | Hail proxy |
| 1 km AGL reflectivity | REFD | 1000 m AGL | instantaneous | |
| 4 km AGL reflectivity | REFD | 4000 m AGL | instantaneous | Mid-level rotation proxy |
| -10°C reflectivity | REFD | 263 K | instantaneous | Hail growth zone |
| Max 1 km reflectivity | MAXREF | 1000 m AGL | hourly_max | |
| -10°C max reflectivity | REFD | 263 K | hourly_max | |
| 2-5 km updraft helicity | MXUPHL | 5000-2000 m AGL | hourly_max | Supercell |
| 0-2 km updraft helicity | MXUPHL | 2000-0 m AGL | hourly_max | Tornado/QLCS mesovortex |
| 0-3 km updraft helicity | MXUPHL | 3000-0 m AGL | hourly_max | Low-level rotation |
| 2-5 km min UH | MNUPHL | 5000-2000 m AGL | hourly_min | Anticyclonic discrimination |
| 0-2 km min UH | MNUPHL | 2000-0 m AGL | hourly_min | |
| 0-3 km min UH | MNUPHL | 3000-0 m AGL | hourly_min | |
| 0-2 km relative vorticity | RELV | 2000-0 m AGL | hourly_max | |
| 0-1 km relative vorticity | RELV | 1000-0 m AGL | hourly_max | |
| Max updraft | MAXUVV | 100-1000 mb AGL | hourly_max | |
| Max downdraft | MAXDVV | 100-1000 mb AGL | hourly_max | |
| Mean vertical velocity 0.5-0.8 sigma | DZDT | 0.5-0.8 sigma | hourly_average | |
| Max column hail | HAIL | entire atm | hourly_max | |
| Max hail at 0.1 sigma | HAIL | 0.1 sigma | hourly_max | |
| Total column graupel | TCOLG | entire atm | hourly_max | |
| Lightning flash | LTNG | entire atm | instantaneous | Non-dimensional |

#### Parcel-based thermodynamic parameters

| Field | HRRR ID | Level | Notes |
|---|---|---|---|
| Surface-based CAPE | CAPE | surface | SBCAPE |
| Surface-based CIN | CIN | surface | SBCIN |
| Mean-layer CAPE (180 mb) | CAPE | 180-0 mb AGL | Conventional MLCAPE |
| Mean-layer CIN (180 mb) | CIN | 180-0 mb AGL | Conventional MLCIN |
| Mean-layer CAPE (90 mb) | CAPE | 90-0 mb AGL | Shallow mean-layer variant |
| Mean-layer CIN (90 mb) | CIN | 90-0 mb AGL | |
| Most-unstable CAPE | CAPE | 255-0 mb AGL | MUCAPE |
| Most-unstable CIN | CIN | 255-0 mb AGL | MUCIN |
| Low-level CAPE | CAPE | 0-3000 m AGL | 0-3 km CAPE |
| MU parcel source pressure | PLPL | 255-0 mb AGL | |
| Surface Lifted Index | LFTX | 500-1000 mb | |
| Best 4-layer Lifted Index | 4LFTX | 180-0 mb AGL | |
| LCL height (surface parcel) | HGT | level of adiabatic condensation | SBLCL |
| Equilibrium level | HGT | EL | |
| PBL height | HPBL | surface | Mixing depth |

#### Kinematic composites

| Field | HRRR ID | Level | Notes |
|---|---|---|---|
| 0-1 km SRH | HLCY | 1000-0 m AGL | Critical tornado parameter |
| 0-3 km SRH | HLCY | 3000-0 m AGL | |
| Storm motion U | USTM | 0-6000 m AGL | Bunkers right |
| Storm motion V | VSTM | 0-6000 m AGL | |
| 0-1 km bulk shear U | VUCSH | 0-1000 m AGL | |
| 0-1 km bulk shear V | VVCSH | 0-1000 m AGL | |
| 0-6 km bulk shear U | VUCSH | 0-6000 m AGL | |
| 0-6 km bulk shear V | VVCSH | 0-6000 m AGL | |

#### Pressure-level 3D fields (50 mb to 1013.2 mb, every 25 mb)

| Field | HRRR ID |
|---|---|
| Geopotential height | HGT |
| Temperature | TMP |
| Relative humidity | RH |
| Dewpoint | DPT |
| Specific humidity | SPFH |
| Vertical velocity (pressure coords) | VVEL |
| U-wind component | UGRD |
| V-wind component | VGRD |
| Absolute vorticity | ABSV |

Hydrometeor mixing ratios (CLMR, CIMIXR, RWMR, SNMR, GRLE) are present at every 25 mb level in the raw file but are excluded from the v1 subset. **Note:** cloud-water mixing ratio appears in the real HRRR `wrfprsf` inventory as `CLMR` (the wgrib2/NCEP abbreviation), not `CLWMR` (the GRIB2 master-table name). The subset pattern must use `CLMR`; see §4.5.

#### Moisture

| Field | HRRR ID | Level | Notes |
|---|---|---|---|
| Precipitable water | PWAT | entire atm | |
| 2 m specific humidity | SPFH | 2 m AGL | |
| 2 m dewpoint | DPT | 2 m AGL | |
| 2 m relative humidity | RH | 2 m AGL | |
| 2 m potential temperature | POT | 2 m AGL | |
| RH with respect to PWAT | RHPW | entire atm | Layer-integrated moisture indicator |
| Moisture availability | MSTAV | 0 m underground | |

#### Levels and heights

| Field | HRRR ID | Level | Notes |
|---|---|---|---|
| 0°C isotherm height | HGT | 0C isotherm | Freezing level |
| 0°C isotherm RH | RH | 0C isotherm | |
| 0°C isotherm pressure | PRES | 0C isotherm | |
| Highest freezing level height | HGT | top freezing | |
| -10°C height | HGT | 263 K | |
| -20°C height | HGT | 253 K | |
| -12 to -17°C layer thickness | LAYTH | 261-256 K | DGZ proxy |
| Cloud ceiling | HGT | cloud ceiling | |
| Cloud base / top heights and pressures | HGT, PRES | cloud base, cloud top | |

#### Surface and near-surface

| Field | HRRR ID | Notes |
|---|---|---|
| MSLP | MSLMA | MAPS reduction |
| Surface pressure | PRES | |
| Surface temperature | TMP | |
| 2 m temperature | TMP | |
| 10 m wind U/V | UGRD, VGRD | |
| 10 m max wind speed | WIND | hourly_max |
| 10 m max U/V components | MAXUW, MAXVW | hourly_max, time-of-max |
| Surface gust | GUST | |
| 80 m U/V wind | UGRD, VGRD | Wind energy / LLJ |
| Visibility | VIS | |
| Surface roughness | SFCR | |
| Friction velocity | FRICV | |

#### Precipitation

| Field | HRRR ID | Notes |
|---|---|---|
| Total precipitation accumulation | APCP | 0-2 hr and 1-2 hr |
| Precipitation rate | PRATE | |
| Snow water equivalent accumulation | WEASD | |
| Total snowfall | ASNOW | |
| Snow depth | SNOD | |
| Snow cover percentage | SNOWC | |
| Frozen rain accumulation | FROZR | |
| Freezing rain accumulation | FRZR | |
| Percent frozen precipitation | CPOFP | |
| Categorical snow/ice/freezing/rain | CSNOW, CICEP, CFRZR, CRAIN | binary 0/1 |
| Storm surface runoff | SSRUN | |
| Baseflow runoff | BGRUN | |

#### Radiation

| Field | HRRR ID | Notes |
|---|---|---|
| Surface downward shortwave | DSWRF | |
| Surface upward shortwave | USWRF | |
| Surface downward longwave | DLWRF | |
| Surface upward longwave | ULWRF | |
| Cloud forcing net solar | CFNSF | |
| Visible beam downward | VBDSF | |
| Visible diffuse downward | VDDSF | |
| TOA upward longwave | ULWRF (TOA) | Cloud-top temperature proxy |
| TOA upward shortwave | USWRF (TOA) | |
| Sensible / latent / ground heat flux | SHTFL, LHTFL, GFLUX | |

#### Cloud cover

| Field | HRRR ID |
|---|---|
| Total cloud cover (entire atm) | TCDC |
| Boundary layer cloud cover | TCDC |
| Low / middle / high cloud cover | LCDC / MCDC / HCDC |

#### Smoke and aerosols

| Field | HRRR ID | Notes |
|---|---|---|
| Near-surface smoke concentration | MASSDEN | 8 m AGL |
| Aerosol optical thickness | AOTK | Column |
| Column-integrated mass density | COLMD | Three species (smoke, dust, sea salt) |

#### Soil

| Field | HRRR ID | Levels |
|---|---|---|
| Soil temperature | TSOIL | 0, 0.01, 0.04, 0.1, 0.3, 0.6, 1, 1.6, 3 m below ground |
| Volumetric soil moisture | SOILW | Same levels |

### 4.2 Trivial derivations (compute at ingest)

- 10 m and 80 m wind speed and direction (from U/V)
- Pressure-level wind speed and direction at all 3D levels (replaces U/V in the threshold UI)
- 0-1 km and 0-6 km bulk shear magnitude (from VUCSH/VVCSH)
- Storm motion speed and direction (from USTM/VSTM)
- Layer lapse rates: 850-500 mb, 700-500 mb, 925-700 mb, 0-3 km, 3-8 km
- Dewpoint depression at 2 m and at all pressure levels
- Wet bulb temperature at 2 m and at pressure levels
- Surface and 850 mb theta-e
- Hail growth zone depth (253 K HGT − 263 K HGT; the −20°C level is higher than the −10°C level, so depth is the 253 K height minus the 263 K height — the original draft had this subtraction reversed)
- Warm cloud depth (LCL height to 0°C height)
- Mean RH over common slabs (700-500, 850-500). User-named arbitrary slabs computed client-side.

### 4.3 Composite indices (compute at ingest)

| Index | Notes |
|---|---|
| STP (fixed-layer) | Uses MLLCL computed from 3D parcel lift |
| STP (effective-layer) | Requires effective inflow base from 3D |
| SCP (fixed-layer) | |
| SCP (effective-layer) | |
| SHIP | MUCAPE × 850 mb mixing ratio × 700-500 lapse rate × 500 mb T × 0-6 km shear |
| Craven-Brooks | MLCAPE × 0-6 km shear |
| DCAPE | From theta-e minimum parcel descent |
| K-index | From 850/500 T, 850/700 Td |
| Total Totals | |
| Cross Totals | |
| MDPI | Microburst Day Potential Index |
| WMSI | Wet Microburst Severity Index |
| WINDEX | |
| SHERB | Sherburne composite |

### 4.4 Excluded from threshold UI

Available in the underlying data but hidden from the threshold-builder dropdown to reduce clutter:

- Vegetation type (VGTYP)
- Plant canopy water (CNWAT)
- Simulated brightness temperatures (SBT123, SBT124, SBT113, SBT114)
- Land/sea mask (LAND), ice cover (ICEC)
- Soil temperature and moisture below 0.3 m (kept in data, hidden in default UI)

### 4.5 wgrib2 subset pattern (production VPS)

```bash
wgrib2 input.grib2 \
    -not_match ":(CLMR|CIMIXR|RWMR|SNMR|GRLE):" \
    -grib_out subset.grib2
```

`-not_match` operates on wgrib2's inventory text, which names cloud-water mixing ratio `CLMR` (confirmed against a real archived `wrfprsf` file: 0 matches for `CLWMR`, 40 for `CLMR`). An earlier draft used `CLWMR`, which would have silently failed to drop cloud water. The ingest code's exclude pattern matches both names (`CLMR|CLWMR`) for robustness across HRRR versions.

Estimated subset size: ~200-250 MB per forecast hour from ~700 MB raw. Roughly 10 GB total per extended cycle (f00-f48), 4 GB per standard cycle (f00-f18).

## 5. UI/UX Specifications

### 5.1 Wind speed and direction

Pressure-level winds are exposed as derived speed and direction. Raw UGRD/VGRD components are hidden from the threshold-builder dropdown. Internally retained for any composite that needs them.

### 5.2 Hourly-max temporal tagging

Each parameter carries a `temporal_type` field in its metadata: `instantaneous`, `hourly_max`, `hourly_min`, `hourly_average`, or `accumulation`. The UI displays a small badge next to parameters with non-instantaneous temporal types ("max/hr", "min/hr", "avg/hr", "accum"). Tooltips explain the semantic.

This matters because a threshold like `MXUPHL ≥ 100` means "UH reached 100 at some point in the past hour somewhere on the grid cell scale," which is operationally different from an instantaneous reading.

### 5.3 Categorical precipitation masks

`CSNOW`, `CICEP`, `CFRZR`, `CRAIN` are exposed as a separate "categorical" parameter class. Operator is restricted to `= 1` (precip type active and dominant) or `= 0` (not active or not dominant). They can participate in count-map logic alongside continuous parameters.

Tooltip on each: "HRRR reports the dominant precipitation type. If precipitation is mixed, only the dominant type is flagged."

### 5.4 Threshold operators

Supported operators: `>=`, `>`, `<=`, `<`, `==`, `!=`, range (between two values inclusive).

### 5.5 Hard vs soft thresholds

Each threshold in a preset is tagged `hard` or `soft`. The count map can show separate contours for "all hard met" and "all hard plus N% of soft met." Default render is "all hard met" only.

## 6. Preset System

### 6.1 JSON schema

```json
{
  "schema_version": "1.0",
  "id": "classic_plains_supercell",
  "name": "Classic Plains Supercell",
  "category": "severe_convective",
  "subcategory": "tornado",
  "description": "Strong tornado environment over the central and southern Plains. Best applied in spring afternoon convection ahead of a dryline or cold front.",
  "applicable_regions": ["central_plains", "southern_plains"],
  "applicable_seasons": ["spring", "early_summer"],
  "author": "alex.cooke",
  "version": 1,
  "created": "2026-05-23",
  "forecast_hour_window": {
    "type": "utc_hour_range",
    "start": 18,
    "end": 4
  },
  "thresholds": [
    {
      "parameter_id": "mlcape_180mb",
      "operator": ">=",
      "value": 2500,
      "units": "J/kg",
      "hardness": "hard"
    },
    {
      "parameter_id": "shear_0_6km_magnitude",
      "operator": ">=",
      "value": 40,
      "units": "kt",
      "hardness": "hard"
    },
    {
      "parameter_id": "srh_0_1km",
      "operator": ">=",
      "value": 150,
      "units": "m2/s2",
      "hardness": "hard"
    },
    {
      "parameter_id": "mllcl_height",
      "operator": "<=",
      "value": 1000,
      "units": "m",
      "hardness": "hard"
    },
    {
      "parameter_id": "lapse_700_500",
      "operator": ">=",
      "value": 7.0,
      "units": "C/km",
      "hardness": "soft"
    }
  ],
  "notes": "Add boundary-layer dewpoint above 60°F as a soft if available. Useful to verify the MU parcel source pressure is at or near the surface."
}
```

### 6.2 Built-in vs user presets

- **Built-in:** stored as individual JSON files at `presets/<id>.json` in the repo. Loaded at frontend startup via the `presets/index.json` manifest. Versioned in git.
- **User:** stored in browser localStorage. Same schema. Same UI for editing and inspecting.
- **Distinction in UI:** badge or icon on each preset card. Otherwise identical.

### 6.3 Authoring workflow

The authoring UI (preset builder, "Export as built-in JSON" button, and related controls) is hidden from regular users by default. To enable it, visit `/forge` once on your browser. That route sets `localStorage.setItem('authoringMode', '1')` and redirects to the main app. The flag persists per-browser per-domain. Regular users — who don't know the URL — see only the standard preset selector and an inline "Save as my preset" button that appears after they've configured a threshold bundle in the main UI.

The URL is intentionally obscure but not cryptographically secret. Worst case: someone discovers it and sees the "Export as built-in JSON" button. That button only produces copy-able text and grants no repo access, so there's no privilege escalation. The point of gating is to reduce UI clutter and avoid users assuming there's an official submission process.

With authoring mode enabled, the workflow is:

1. Open the tool.
2. Click "New preset".
3. Configure parameters and thresholds in the builder UI, against live HRRR data.
4. Save as user preset (goes to localStorage).
5. Tune across several cycles to verify behavior.
6. Click "Export as built-in JSON" — copies JSON to clipboard with suggested filename.
7. Paste into `presets/<id>.json` in repo, add ID to `presets/index.json`, commit.
8. Next site deploy ships it as a built-in.

### 6.4 Durability for user presets

No user accounts. Three-layer durability:

1. localStorage for session-fast access.
2. Auto-download of full preset library as JSON on every save (file in Downloads folder).
3. URL hash encoding for sharing: encode bundle as base64 in URL fragment, send link to colleague, they open and import.

Optional Chrome/Edge upgrade: File System Access API. User picks a presets folder once; app reads and writes JSON files there directly. Firefox/Safari fall back to layers 1-3.

## 7. Visualization

CONUS map with the count field overlaid as colored contours. Strongest color for full match (N/N), lighter stepping down through N-1, N-2. User sets the floor for what counts get rendered.

Map library: pending decision. Candidates are Mapbox GL JS (requires commercial token), MapLibre GL JS (open-source fork of Mapbox), Leaflet (mature, widely used, less performant for raster overlays). MapLibre is the current lean; final pick deferred to v1 build.

Click-to-inspect side panel:
- Coordinates and forecast hour
- Each selected parameter's value at that grid cell
- Each parameter's threshold and pass/fail (color-coded green/red)
- Count of thresholds met
- Identification of the limiting parameter(s) (those failing the threshold)

Forecast hour selector with scrubbing through available f00 through f48 (or whatever the current cycle provides).

## 8. Implementation Principles

These are non-negotiable design constraints, established early to make migration and maintenance painless.

### 8.1 Docker-first

The ingest worker is a Docker container from day one. Deploys are `docker compose up -d`. No host-specific package installs or non-portable scripts. This enables trivial migration between hosts (Hetzner, OCI, Mac, mini-PC, anywhere).

### 8.2 All configuration via environment variables

R2 endpoint, R2 credentials, R2 bucket name, NOAA bucket URL, cron timing, log destinations — every operational knob lives in environment variables loaded from a `.env` file that is gitignored. No secrets in the repo. No hard-coded paths.

### 8.3 R2 as the stable abstraction layer

Forecasters point their bookmarks at the Cloudflare Pages frontend URL. The frontend reads from R2. R2 doesn't move when ingest does. This means:

- The ingest worker is replaceable without breaking the frontend or notifying users.
- Migration from Hetzner to anywhere else is "provision new host, configure R2 credentials, start cron."
- The R2 bucket and the Cloudflare Pages URL are the only things that need to remain stable across the life of the tool.

### 8.4 No vendor lock-in in the ingest code

The ingest pipeline uses standard tools: Python, xarray, cfgrib, wgrib2, dask, Zarr. The R2 API is S3-compatible, so the storage layer can be swapped for any S3-compatible store (AWS S3, MinIO, etc.) by changing environment variables. The deployment target can be any Linux host with Docker.

### 8.5 Documented runbook

The repo contains a short runbook (`docs/runbook.md`) explaining:

- How to provision a new host.
- How to migrate from one host to another.
- How to recover from a failed cycle.
- How to roll back a frontend deploy.

The runbook is the operational source of truth and should be readable by future-Alex without needing chat history.

## 9. Model Lifecycle

HRRR is scheduled to be replaced by RRFS (Rapid Refresh Forecast System) eventually, but the transition is gradual.

### 9.1 RRFS timeline

- August 31, 2026: RRFS becomes operational alongside HRRR. RRFS replaces NAM, SREF, HREF, HiresW, and NAM MOS. HRRR remains in operations and is not retired.
- 2027-2028 (estimated): RRFSv2 (MPAS dynamical core) matures. HRRR retirement expected when RRFSv2 reaches operational status.

The tool will run on HRRR data for its entire initial deployment period.

### 9.2 Porting plan when HRRR retires

Expected effort: 3-7 working days.

Changes touch only the ingest pipeline. The Zarr storage layer, R2, frontend, and preset schema are all model-agnostic.

Work required:

1. Update the wgrib2 subset pattern for RRFS variable names where they differ.
2. Update the parameter catalog metadata to map `parameter_id` to new GRIB shortNames.
3. Update derived parameter calculations where any input variable has changed.
4. Update geographic constants (RRFS covers North America, larger than HRRR's CONUS domain).
5. Re-validate preset thresholds against RRFS behavior. Some presets may need threshold adjustments.
6. Bump schema_version on presets if any structural changes are needed (likely not).

RRFS pre-operational data is available on the AWS `noaa-rrfs` bucket starting June 9, 2026 (per NOAA Service Change Notice). Porting work can begin in prototype form anytime after that, well in advance of HRRR retirement.

## 10. Implementation Plan

Recommended build order. Each phase is independently testable before moving on.

### 10.1 Phase 1: Ingest pipeline (priority 1)

Until ingest works end-to-end, nothing else can be validated. Build this first.

1. Set up the repo structure (see Section 11).
2. Write the Dockerfile for the ingest worker (Python 3.12, eccodes, wgrib2 if available, all pinned).
3. Write the orchestrator script that:
   - Computes the current cycle from system time.
   - Iterates forecast hours with a two-worker pool.
   - Fetches, subsets, computes, writes per forecast hour.
   - Validates cycle completeness.
   - Writes the manifest atomically.
4. Write a smoke test that runs one forecast hour end-to-end against R2 (or a local minio for development).
5. Provision the Hetzner CX32 in Ashburn.
6. Deploy via `docker compose up -d`.
7. Set up cron and Pushover failure notifications.
8. Run for 48 hours, verify cycle integrity, fix issues.

### 10.2 Phase 2: Frontend skeleton (priority 2)

Once ingest is producing Zarr in R2:

1. Set up a minimal Cloudflare Pages site.
2. Read the `manifest.json` from R2 on page load.
3. Load one parameter's Zarr chunks into memory for a chosen forecast hour.
4. Render a simple visualization (Mapbox/MapLibre/Leaflet TBD) showing one threshold applied to one parameter.
5. Verify chunk fetch performance is acceptable.

### 10.3 Phase 3: Threshold builder UI (priority 3)

1. Multi-parameter threshold selector.
2. Count map computation client-side.
3. Click-to-inspect side panel.
4. Forecast hour scrubber.
5. Hard/soft threshold rendering.

### 10.4 Phase 4: Preset system (priority 4)

1. Built-in preset loader (reads `presets/index.json` from R2 or static site assets).
2. User preset save/load via localStorage.
3. URL hash sharing.
4. Auto-download backup on save.
5. `/forge` route for authoring mode.
6. "Export as built-in JSON" button.

### 10.5 Phase 5: Preset library seeding (priority 5)

Author the initial built-in preset library through the authoring workflow, using the Louisville tip sheet thresholds and SPC reference values as starting points. Tune each preset across several real cycles before committing.

Target initial library: 15-20 built-in presets covering severe weather, fire weather, winter weather, and aviation regimes.

## 11. Repository Structure

```
hrrr-threshold-tool/
├── ingest/                    Python ingest pipeline
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── src/
│   │   ├── orchestrator.py
│   │   ├── fetch.py
│   │   ├── subset.py
│   │   ├── compute/           Derived parameters and composite indices
│   │   ├── publish.py         R2 writes and manifest updates
│   │   └── catalog.py         Parameter metadata
│   ├── tests/
│   └── README.md
├── frontend/                  Cloudflare Pages frontend
│   ├── package.json
│   ├── src/
│   │   ├── lib/
│   │   ├── components/
│   │   └── routes/
│   │       └── forge/         Authoring mode entry point
│   └── public/
├── presets/                   Built-in preset JSON files
│   ├── index.json
│   └── <id>.json (one per preset)
├── deploy/                    Hetzner provisioning artifacts
│   ├── terraform/             (optional)
│   ├── ansible/               (optional)
│   └── cron.sh
├── docs/
│   ├── hrrr_threshold_tool_spec.md   This document
│   ├── runbook.md
│   └── adr/                   Architecture Decision Records
├── .env.example
├── .gitignore
└── README.md
```

## 12. Decisions Locked (changelog)

- **2026-05-23:** Project scoped to wrfprsf only as ingest source (supersets wrfsfcf with minor exceptions). Hydrometeor mixing ratios excluded from subset.
- **2026-05-23:** No user accounts. Durability via localStorage + auto-download + URL-hash sharing, with optional File System Access API upgrade.
- **2026-05-23:** Smoke/aerosol fields in scope. Soil fields in scope.
- **2026-05-23:** Hourly-max vs instantaneous distinction surfaced in UI via temporal_type tag and badge.
- **2026-05-23:** Pressure-level winds exposed as speed/direction; raw U/V hidden from threshold dropdown.
- **2026-05-23:** Categorical precip fields exposed as binary masks with restricted operator set.
- **2026-05-23:** Preset architecture: per-preset JSON files in repo, in-app authoring via "Export as built-in JSON" button. No separate admin mode.
- **2026-05-23:** Authoring UI gated behind localStorage flag set via `/forge` route. Default users see no authoring controls.
- **2026-05-23 (superseded):** ~~VPS sizing: Hetzner CX41 (16 GB, 4 vCPU) in Ashburn, two-worker parallel.~~ Superseded by 2026-05-24 entry below after cost reconsideration.
- **2026-05-24 (superseded):** ~~VPS sizing locked: Hetzner CX32 (8 GB RAM, 2 shared vCPUs, ~€8.21/month) in Ashburn, Virginia. Two-worker parallel ingest.~~ Superseded by the 2026-05-26 sizing entry below: the full §4.3 parcel suite (effective inflow layer, ESRH/EBWD, effective STP/SCP, DCAPE) is far more expensive than the original benchmark assumed.
- **2026-05-24:** Storage architecture locked: Cloudflare R2 as the canonical storage layer. Ingest worker writes Zarr cycle data to R2; frontend (Cloudflare Pages) reads directly from R2. No backend API server. R2's free egress eliminates per-user bandwidth concerns; R2's free 10 GB tier covers expected usage.
- **2026-05-24:** Zarr compression strategy locked: explicit Zarr 3 `BloscCodec(cname='zstd', clevel=5)` with optional `numcodecs.Quantize` layered before Blosc for v2 storage tuning. Default Zarr 3 codecs produced output larger than input GRIB in the benchmark and are not acceptable.
- **2026-05-24:** Portability principles locked: Docker-first ingest, all config via env vars, R2 as the stable abstraction between compute and serving, no vendor lock-in. Migration between hosts should be hours of work, not days.
- **2026-05-24:** Model lifecycle plan locked: build on HRRR for v1; defer RRFS port until HRRR retirement (estimated 2027-2028). Estimated porting effort 3-7 working days, touching only the ingest pipeline.
- **2026-05-26:** Spec correction during Phase 1 implementation: cloud-water mixing ratio is `CLMR` in the real `wrfprsf` inventory, not `CLWMR`. §4.1 and §4.5 patterns corrected; ingest matches both names. Verified against archived `hrrr.20240101/t00z.wrfprsf00` (548 non-hydrometeor messages, 85 distinct shortNames).
- **2026-05-26:** Phase 1 ingest backbone implemented and validated end-to-end against real HRRR data: fetch → subset (wgrib2 + cfgrib fallback) → compute (cfgrib open with mandatory isobaric dask chunking, unit conversion, §4.2 derivations) → publish (Zarr 3 BloscCodec + atomic manifest lifecycle). Catalog covers a verified core of ~70 native §4.1 parameters; §4.1 long tail deferred. Ingest masks known in-band fill values (e.g. echo-top no-echo) to NaN so the UI never shows sentinel numbers. Hail-growth-zone-depth subtraction sign corrected in §4.2.
- **2026-05-26:** All §4.3 composite indices implemented via a hand-rolled vectorized gridded parcel engine (no MetPy): algebraic/fixed-layer (K, Total/Cross Totals, Craven-Brooks, SHIP, MDPI, WMSI, fixed STP/SCP) and parcel-based (effective inflow layer per Thompson 2007, ESRH, EBWD, effective STP/SCP, DCAPE, WINDEX, SHERBE). SPC mesoanalysis formulas; coefficients are named constants for review. Parcel engine validated against HRRR's own fields: surface CAPE corr 0.982, MUCAPE corr 0.965 / ratio 0.96 (the ~24% bias vs HRRR SBCAPE is the virtual-temperature correction, an intentional SPC convention). Decisions: virtual-temperature CAPE; MLLCL via Espy T−Td on the lowest-100mb mean; SHIP uses 850 mb mixing ratio pending an MU-parcel upgrade; user-facing CAPE stays HRRR's published fields while the parcel engine's CAPE is internal to the effective layer.
- **2026-05-27:** Parameter provenance surfaced for the frontend: every variable carries a `source` attribute — `hrrr` (read directly from the model, §4.1), `derived` (computed from HRRR fields, §4.2), or `composite` (a composite index, §4.3) — and the per-cycle `manifest.json` now includes a `parameters` array of `{id, source, units, temporal_type, param_class, description, ui_visible, is_3d}` so the UI can badge HRRR-vs-calculated quantities (extends the §5.2 temporal badge). Even with good HRRR-vs-computed agreement, the UI must always make the distinction explicit.
- **2026-05-27:** Frontend will be hosted on the author's **GitHub Pages** site (not Cloudflare Pages as §2.3 originally said). Storage architecture is unchanged — the static site reads Zarr + manifests from R2 via the public bucket URL. Deployment detail for Phase 2: configure R2 CORS to allow the GitHub Pages origin. A local `STORAGE_URL` override now lets the full pipeline run to a folder for end-to-end testing without R2 (verified by the §10.1.4 smoke test).
- **2026-05-26:** VPS sizing (supersedes the CX32 entry; final box TBD): the optimized parcel suite measures **~146–168 s/forecast-hour and ~2.3–6.9 GB peak RSS per worker** on the full CONUS grid, tunable via spatial y-tiling (`PARCEL_TILES`, default 8 → 2.3 GB/worker). float32 + sequential moist-adiabat integration preserve accuracy. With tiling, even a 4 vCPU / 8 GB box handles the 49-FH extended cycles (4×/day) in ~35 min at ~3–4 workers; standard cycles are comfortable. `INGEST_WORKERS` and `PARCEL_TILES` to be set to (box RAM / per-worker RSS) once the box is chosen.

## 13. Open Decisions

- Map library: Mapbox GL JS vs. MapLibre GL JS vs. Leaflet. Current lean: MapLibre.
- Zarr spatial chunking strategy: optimize chunk shape for typical user viewports while minimizing overfetch.
- Initial preset library content: specific bundles and thresholds. To be authored through the in-app workflow once the tool is running.
- Deployment automation: Terraform vs. Ansible vs. plain shell scripts for the Hetzner provisioning. Defer until Phase 1 implementation.
