"""Write Zarr to R2 and manage the atomic cycle/manifest lifecycle.

Spec 2.2 / 3.2: each forecast hour is written to a per-FH Zarr group inside the
cycle's path (``cycles/<cycle_id>/f<FF>``). Once every forecast hour is present
the worker writes the cycle manifest, then repoints the global ``manifest.json``
at the new cycle (a single object PUT is atomic on S3/R2 — readers get the old
or the new object, never partial), then deletes the previous cycle.

Zarr compression uses explicit Zarr 3 codecs (spec 3.5): BloscCodec zstd
clevel 5 — NOT numcodecs.Blosc.

Everything goes through fsspec, so the same code targets R2 (``s3://``) in
production and a local directory in tests via an injected :class:`Store`.
"""

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

import fsspec

from .config import Config
from .cycle import Cycle

if TYPE_CHECKING:
    import xarray as xr

logger = logging.getLogger("hrrr_ingest.publish")

MANIFEST_KEY = "manifest.json"
# 2.0: one sharded Zarr store per cycle with a forecast_hour dimension
# (cycles/<id>/data.zarr), instead of a separate group per forecast hour. This
# collapses the per-cycle object count ~100x (each cycle was ~150k tiny chunk
# objects re-uploaded ~24x/day = ~1M R2 Class-A PUTs/day) by writing the whole
# cycle locally, then uploading one sharded store. See assemble_and_upload_cycle.
SCHEMA_VERSION = "2.0"


@dataclass(frozen=True)
class Store:
    """An fsspec-backed object store root (R2 in prod, a local dir in tests)."""

    base_url: str          # "s3://bucket/prefix" (prod) or "/abs/path" (tests)
    storage_options: dict  # passed to fsspec / xarray.to_zarr; {} for local

    @classmethod
    def from_config(cls, cfg: Config) -> Store:
        # STORAGE_URL override: a local path/file:// writes locally (no R2),
        # an s3:// URL still uses the R2 credentials. Otherwise default to R2.
        if cfg.storage_url:
            url = cfg.storage_url.rstrip("/")
            opts = cfg.r2.storage_options if url.startswith("s3://") else {}
            return cls(base_url=url, storage_options=opts)
        base = f"s3://{cfg.r2.bucket}/{cfg.r2.prefix}".rstrip("/")
        return cls(base_url=base, storage_options=cfg.r2.storage_options)

    @property
    def protocol(self) -> str:
        return self.base_url.split("://", 1)[0] if "://" in self.base_url else "file"

    @property
    def _root_path(self) -> str:
        """Protocol-stripped root, for filesystem (non-zarr) operations."""
        return self.base_url.split("://", 1)[1] if "://" in self.base_url else self.base_url

    @cached_property
    def fs(self) -> fsspec.AbstractFileSystem:
        return fsspec.filesystem(self.protocol, **self.storage_options)

    def url(self, key: str) -> str:
        """Full URL for xarray.to_zarr (keeps the protocol)."""
        return f"{self.base_url}/{key.lstrip('/')}"

    def path(self, key: str) -> str:
        """Protocol-stripped path for fs read/write/delete operations."""
        return f"{self._root_path}/{key.lstrip('/')}"


def _store(cfg: Config, store: Store | None) -> Store:
    return store or Store.from_config(cfg)


# --- key helpers -----------------------------------------------------------

def cycle_prefix(cfg: Config, cycle: Cycle) -> str:
    return f"cycles/{cycle.cycle_id}/"


def forecast_hour_key(cfg: Config, cycle: Cycle, forecast_hour: int) -> str:
    return f"{cycle_prefix(cfg, cycle)}f{forecast_hour:02d}"


def cycle_store_key(cfg: Config, cycle: Cycle) -> str:
    """Key of the single per-cycle Zarr store (schema 2.0)."""
    return f"{cycle_prefix(cfg, cycle)}data.zarr"


def cycle_manifest_key(cfg: Config, cycle: Cycle) -> str:
    return f"{cycle_prefix(cfg, cycle)}{MANIFEST_KEY}"


# --- Zarr write ------------------------------------------------------------

def forecast_hour_scratch(cfg: Config, cycle: Cycle, forecast_hour: int) -> Path:
    """Local scratch path for one forecast hour's intermediate Zarr group. Lives
    under WORK_DIR/<cycle_id>/ so the orchestrator's per-cycle rmtree cleans it."""
    return Path(cfg.work_dir) / cycle.cycle_id / "fh" / f"f{forecast_hour:02d}.zarr"


def write_forecast_hour_local(
    ds: xr.Dataset, cycle: Cycle, forecast_hour: int, cfg: Config
) -> str:
    """Write one forecast hour to LOCAL scratch (free) — NOT R2. The parent later
    concatenates every hour into one sharded store and uploads it once
    (assemble_and_upload_cycle), so per-forecast-hour writes never hit R2."""
    path = forecast_hour_scratch(cfg, cycle, forecast_hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Compress scratch with fast zstd (clevel 1): keeps local scratch ~60GB not
    # ~500GB for a 48h cycle. It's transient (reassembled into the sharded store
    # at clevel 5), so favor speed over ratio here.
    from zarr.codecs import BloscCodec, BloscShuffle
    comp = BloscCodec(cname="zstd", clevel=1, shuffle=BloscShuffle.shuffle)
    enc = {v: {"compressors": (comp,)} for v in ds.data_vars}
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*[Cc]onsolidated metadata.*")
        ds.to_zarr(str(path), mode="w", consolidated=False, zarr_format=3, encoding=enc)
    logger.info("wrote forecast hour (local scratch)", extra={
        "cycle": cycle.cycle_id, "fh": forecast_hour, "n_vars": len(ds.data_vars),
    })
    return str(path)


def _sharded_encoding(
    ds: xr.Dataset, n_fh: int, isobaric_chunk: int, stats: dict
) -> dict:
    """Per-variable Zarr 3 encoding for the combined cycle store.

    Each variable is dimensioned (forecast_hour, [isobaricInhPa,] y, x). We shard
    so the object count stays tiny while keeping read granularity:
      - inner chunk: one forecast hour (x one isobaric block) -> the browser
        range-reads exactly the slice it needs out of the shard.
      - shard: spans ALL forecast hours (x one isobaric block) -> a 2D field is a
        single object; a 3D field is one object per isobaric block (not per hour).

    Data variables are packed to **scaled int16** (CF `scale_factor`/`add_offset`,
    `_FillValue=-32768` for NaN) using each field's own min/max (``stats``). That
    ~halves bytes (upload + storage) at a per-field precision of (max-min)/65534 —
    finer than display and on par with HRRR's native GRIB integer packing, so
    it's not throwing away real signal. Blosc-zstd compressor on top (spec 3.5).
    The frontend reader undoes the packing (hrrr/src/data.js).
    """
    import numpy as np
    from zarr.codecs import BloscCodec, BloscShuffle

    comp = BloscCodec(cname="zstd", clevel=5, shuffle=BloscShuffle.shuffle)
    enc = {}
    for v in ds.data_vars:
        chunks, shards = [], []
        for d in ds[v].dims:
            n = int(ds.sizes[d])
            if d == "forecast_hour":
                chunks.append(1)
                shards.append(n_fh)
            elif d == "isobaricInhPa":
                c = min(isobaric_chunk, n)
                chunks.append(c)
                shards.append(c)
            else:  # spatial (y/x) — keep whole
                chunks.append(n)
                shards.append(n)
        e = {"chunks": tuple(chunks), "shards": tuple(shards), "compressors": (comp,)}
        lo, hi = stats.get(v, (float("nan"), float("nan")))
        if np.isfinite(lo) and np.isfinite(hi) and hi > lo:
            # center the range so values map to int16 [-32767, 32767]; -32768 = NaN
            e.update(dtype="int16", _FillValue=-32768,
                     scale_factor=float((hi - lo) / 65534.0),
                     add_offset=float((hi + lo) / 2.0))
        # else (constant / all-NaN field): leave float32 — tiny, not worth packing
        enc[v] = e
    return enc


def assemble_and_upload_cycle(
    cycle: Cycle, forecast_hours: list[int], cfg: Config, store: Store | None = None
) -> str:
    """Concatenate the local per-forecast-hour scratch stores into ONE sharded
    Zarr store (forecast_hour dimension) and upload it to R2 in a single pass.

    Variables absent from some hours (e.g. accumulated fields at f00) are filled
    with NaN so every variable spans the whole forecast_hour axis.
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    st = _store(cfg, store)
    fhs = sorted(forecast_hours)
    dss = [xr.open_zarr(str(forecast_hour_scratch(cfg, cycle, fh)), consolidated=False)
           for fh in fhs]
    try:
        # Union the variable set; NaN-fill any hour missing a variable so concat
        # along forecast_hour is well-formed.
        all_vars = sorted(set().union(*(set(d.data_vars) for d in dss)))
        template = {v: next(d[v] for d in dss if v in d.data_vars) for v in all_vars}
        filled = []
        for d in dss:
            add = {v: xr.full_like(template[v], np.nan, dtype="float32")
                   for v in all_vars if v not in d.data_vars}
            filled.append((d.assign(add) if add else d)[all_vars])

        combined = xr.concat(
            filled, dim=pd.Index(fhs, name="forecast_hour"),
            coords="minimal", compat="override",
        )
        n_fh = len(fhs)
        # Rechunk so each Dask chunk == one shard: whole forecast_hour + whole
        # spatial grid, one isobaric block. Otherwise Dask's per-hour / spatially
        # split chunks would each write part of a shard in parallel, which xarray
        # rejects as corruption-prone.
        chunk_spec = {d: (cfg.isobaric_chunk if d == "isobaricInhPa" else -1)
                      for d in combined.dims}
        combined = combined.chunk(chunk_spec)
        # Per-variable global min/max (one fused Dask pass) -> scaled-int16 packing.
        import dask
        lo_ds, hi_ds = dask.compute(combined.min(skipna=True), combined.max(skipna=True))
        stats = {v: (float(lo_ds[v].values), float(hi_ds[v].values))
                 for v in combined.data_vars}
        local = Path(cfg.work_dir) / cycle.cycle_id / "combined.zarr"
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*[Cc]onsolidated metadata.*")
            combined.to_zarr(
                str(local), mode="w", zarr_format=3, consolidated=True,
                encoding=_sharded_encoding(combined, n_fh, cfg.isobaric_chunk, stats),
            )
    finally:
        for d in dss:
            d.close()

    # Upload the finished store to R2 in one shot (one PUT per store object).
    dest = st.path(cycle_store_key(cfg, cycle)).rstrip("/")
    if st.fs.exists(dest):
        st.fs.rm(dest, recursive=True)
    st.fs.put(str(local) + "/", dest + "/", recursive=True)
    logger.info("assembled + uploaded cycle store", extra={
        "cycle": cycle.cycle_id, "n_fh": n_fh, "n_vars": len(all_vars),
        "url": st.url(cycle_store_key(cfg, cycle)),
    })
    return cycle_store_key(cfg, cycle)


# --- manifest lifecycle ----------------------------------------------------

def parameter_metadata(ds: xr.Dataset) -> list[dict]:
    """Per-variable metadata for the manifest (the frontend's parameter catalog).

    ``source`` is the provenance the frontend badges with: 'hrrr' (read straight
    from the model), 'derived' (computed from HRRR fields, spec 4.2), or
    'composite' (a composite index, spec 4.3).
    """
    from .catalog import category_for

    out = []
    for v in ds.data_vars:
        a = ds[v].attrs
        source = a.get("source", "hrrr")
        out.append({
            "id": str(v),
            "source": source,
            "category": category_for(str(v), source),
            "units": a.get("units", ""),
            "temporal_type": a.get("temporal_type", "instantaneous"),
            "param_class": a.get("param_class", "continuous"),
            "description": a.get("description", ""),
            "ui_visible": bool(int(a.get("ui_visible", 1))),
            "is_3d": "isobaricInhPa" in ds[v].dims,
        })
    return sorted(out, key=lambda d: d["id"])


def write_cycle_manifest(
    cycle: Cycle,
    forecast_hours: list[int],
    cfg: Config,
    *,
    parameters: list[dict] | None = None,
    store: Store | None = None,
) -> dict:
    """Write the per-cycle manifest after all forecast hours are present.

    ``parameters`` is the per-variable metadata (see :func:`parameter_metadata`);
    the frontend reads it to build the threshold UI and badge each parameter's
    provenance (HRRR vs derived vs composite).
    """
    st = _store(cfg, store)
    parameters = parameters or []
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": cycle.cycle_id,
        "init_time": cycle.init.isoformat(),
        "is_extended": cycle.is_extended,
        "store_key": cycle_store_key(cfg, cycle),  # 2.0: single sharded store, FH dim
        "forecast_hours": sorted(forecast_hours),
        "n_forecast_hours": len(forecast_hours),
        "variables": sorted(p["id"] for p in parameters),
        "parameters": parameters,
        "created": datetime.now(UTC).isoformat(),
    }
    _write_json(st, cycle_manifest_key(cfg, cycle), manifest)
    logger.info("wrote cycle manifest", extra={
        "cycle": cycle.cycle_id, "n_parameters": len(parameters),
    })
    return manifest


def promote_cycle(cycle: Cycle, cfg: Config, store: Store | None = None) -> None:
    """Atomically repoint the global manifest.json at ``cycle`` (spec 2.2).

    The manifest carries two pointers: ``current_cycle`` is always set to the
    cycle being promoted, and ``current_extended_cycle`` is set to this cycle
    only when it's an extended run (00/06/12/18 Z). Otherwise the extended
    pointer is preserved from the previous manifest so the most recent 48 h
    forecast stays reachable between extended runs — the frontend offers a
    user-facing toggle that swaps the active cycle between the two pointers.
    """
    st = _store(cfg, store)
    prev = _read_global_manifest(st) or {}
    if cycle.is_extended:
        extended_id = cycle.cycle_id
        extended_key = cycle_manifest_key(cfg, cycle)
    else:
        extended_id = prev.get("current_extended_cycle")
        extended_key = prev.get("current_extended_cycle_manifest_key")
    global_manifest = {
        "schema_version": SCHEMA_VERSION,
        "current_cycle": cycle.cycle_id,
        "cycle_manifest_key": cycle_manifest_key(cfg, cycle),
        "current_extended_cycle": extended_id,
        "current_extended_cycle_manifest_key": extended_key,
        "updated": datetime.now(UTC).isoformat(),
    }
    _write_json(st, MANIFEST_KEY, global_manifest)
    logger.info("promoted cycle", extra={
        "cycle": cycle.cycle_id,
        "extended": extended_id,
    })


def _read_global_manifest(st: Store) -> dict | None:
    """Return the parsed global manifest.json, or None if it doesn't exist."""
    path = st.path(MANIFEST_KEY)
    if not st.fs.exists(path):
        return None
    return json.loads(st.fs.cat_file(path))


def read_current_cycle_id(cfg: Config, store: Store | None = None) -> str | None:
    """Return the cycle id the global manifest currently points at, or None."""
    st = _store(cfg, store)
    data = _read_global_manifest(st) or {}
    return data.get("current_cycle")


def read_current_extended_cycle_id(cfg: Config, store: Store | None = None) -> str | None:
    """Return the cycle id the extended pointer is on, or None."""
    st = _store(cfg, store)
    data = _read_global_manifest(st) or {}
    return data.get("current_extended_cycle")


def delete_cycle(cycle: Cycle, cfg: Config, store: Store | None = None) -> None:
    """Delete a cycle's data from R2 (used to drop the previous cycle)."""
    st = _store(cfg, store)
    path = st.path(cycle_prefix(cfg, cycle)).rstrip("/")
    if st.fs.exists(path):
        st.fs.rm(path, recursive=True)
        logger.info("deleted cycle", extra={"cycle": cycle.cycle_id})


def list_cycle_ids(cfg: Config, store: Store | None = None) -> list[str]:
    """Return every cycle id (``YYYYMMDDHH``) present under ``cycles/``.

    Used to garbage-collect cycles that are neither current nor extended, so
    orphans from failed/early runs or a prior host can't accumulate unbounded.
    """
    st = _store(cfg, store)
    base = st.path("cycles").rstrip("/")
    if not st.fs.exists(base):
        return []
    ids = []
    for entry in st.fs.ls(base, detail=False):
        name = str(entry).rstrip("/").rsplit("/", 1)[-1]
        if len(name) == 10 and name.isdigit():
            ids.append(name)
    return ids


def _write_json(store: Store, key: str, payload: dict) -> None:
    data = json.dumps(payload, indent=2).encode()
    path = store.path(key)
    parent = path.rsplit("/", 1)[0]
    store.fs.makedirs(parent, exist_ok=True)
    store.fs.pipe_file(path, data)
