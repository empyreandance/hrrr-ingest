"""Open the subset GRIB, extract cataloged fields, convert units, derive (4.2).

Spec 3.2 (steps 3-5) + 3.3: open with cfgrib via xarray using dask chunking on
the isobaric level axis (mandatory), select each cataloged parameter, convert to
operational units, then add the trivial derivations. Each field is opened with
its own ``filter_by_keys`` so selection is unambiguous; 3D fields are chunked at
open time so the 40-level arrays never load whole (memory discipline).

Heavy imports (xarray, cfgrib) are deferred into function bodies so importing
the package — and forking workers — stays cheap.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ..catalog import Parameter, native_parameters
from ..config import Config
from . import composites, derivations, parcel_composites
from .units import apply_conversion

if TYPE_CHECKING:
    import xarray as xr

logger = logging.getLogger("hrrr_ingest.compute")

# Coords cfgrib attaches that we keep; everything else scalar (level/time/step)
# is dropped before merge to avoid conflicts between differently-leveled fields.
_KEEP_COORDS = {"latitude", "longitude", "isobaricInhPa"}


def mask_missing(da: xr.DataArray, missing_values: tuple[float, ...]) -> xr.DataArray:
    """Replace in-band fill sentinels with NaN so the UI never shows them.

    cfgrib already masks GRIB-bitmapped missing values; this handles sentinels
    encoded as real numbers (e.g. echo top's -999 = "no echo"). No-op when the
    parameter declares no sentinels.
    """
    if not missing_values:
        return da
    return da.where(~da.isin(list(missing_values)))


def open_native_variable(
    path: Path, param: Parameter, isobaric_chunk: int
) -> xr.DataArray | None:
    """Open and convert one cataloged native field; None if it isn't present."""
    import xarray as xr

    chunks = {"isobaricInhPa": isobaric_chunk} if param.is_3d else None
    try:
        ds = xr.open_dataset(
            path,
            engine="cfgrib",
            backend_kwargs={"filter_by_keys": param.filters, "indexpath": "", "errors": "ignore"},
            chunks=chunks,
        )
    except Exception as exc:  # noqa: BLE001 - a missing field shouldn't abort the FH
        logger.warning("could not open parameter", extra={
            "parameter_id": param.parameter_id, "error": str(exc),
        })
        return None

    data_vars = list(ds.data_vars)
    if not data_vars:
        logger.warning("no message matched filters", extra={"parameter_id": param.parameter_id})
        return None
    if len(data_vars) > 1:
        logger.warning("filters matched multiple vars; using first", extra={
            "parameter_id": param.parameter_id, "vars": data_vars,
        })

    da = ds[data_vars[0]]
    da = mask_missing(da, param.missing_values)  # before conversion (sentinels are in native units)
    da = apply_conversion(da, param.convert)
    da.attrs["units"] = param.out_units
    da.attrs["description"] = param.description
    da.attrs["temporal_type"] = param.temporal_type
    da.attrs["param_class"] = param.param_class
    da.attrs["ui_visible"] = int(param.ui_visible)
    da.attrs["source"] = "hrrr"   # read directly from the HRRR GRIB (spec 4.1)

    # Drop scalar level/time coords (heightAboveGround, surface, step, valid_time,
    # ...) that would otherwise collide when merging fields from different levels.
    drop = [c for c in da.coords if c not in _KEEP_COORDS and da[c].ndim == 0]
    da = da.drop_vars(drop)
    da.name = param.parameter_id
    return da


def build_forecast_hour_dataset(
    subset_path: Path,
    cfg: Config,
    *,
    cycle_id: str | None = None,
    forecast_hour: int | None = None,
) -> xr.Dataset:
    """Assemble one forecast hour's output dataset: native fields + derivations."""
    import pandas as pd
    import xarray as xr

    arrays: list[xr.DataArray] = []
    for param in native_parameters():
        da = open_native_variable(subset_path, param, cfg.isobaric_chunk)
        if da is not None:
            arrays.append(da)

    if not arrays:
        raise RuntimeError(f"no cataloged variables found in {subset_path}")

    ds = xr.merge(arrays, compat="override", combine_attrs="drop_conflicts")
    ds = derivations.add_derived(ds)
    ds = composites.add_composites(ds)
    ds = parcel_composites.add_parcel_composites(ds, n_tiles=cfg.parcel_tiles)

    n_native = len(arrays)
    ds.attrs.update({
        "n_native_vars": n_native,
        "n_total_vars": len(ds.data_vars),
    })
    if cycle_id is not None:
        ds.attrs["cycle_id"] = cycle_id
        ds.attrs["forecast_hour"] = forecast_hour
        init = pd.Timestamp(
            year=int(cycle_id[0:4]), month=int(cycle_id[4:6]),
            day=int(cycle_id[6:8]), hour=int(cycle_id[8:10]), tz="UTC",
        )
        ds.attrs["init_time"] = init.isoformat()
        ds.attrs["valid_time"] = (init + timedelta(hours=forecast_hour or 0)).isoformat()

    logger.info("built forecast-hour dataset", extra={
        "cycle": cycle_id, "fh": forecast_hour,
        "n_native": n_native, "n_total": len(ds.data_vars),
    })
    return ds
