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
from typing import TYPE_CHECKING

import fsspec

from .config import Config
from .cycle import Cycle

if TYPE_CHECKING:
    import xarray as xr

logger = logging.getLogger("hrrr_ingest.publish")

MANIFEST_KEY = "manifest.json"
SCHEMA_VERSION = "1.0"


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


def cycle_manifest_key(cfg: Config, cycle: Cycle) -> str:
    return f"{cycle_prefix(cfg, cycle)}{MANIFEST_KEY}"


# --- Zarr write ------------------------------------------------------------

def _zarr_encoding(ds: xr.Dataset) -> dict:
    """Zarr 3 Blosc-Zstd compressor for every data variable (spec 3.5)."""
    from zarr.codecs import BloscCodec, BloscShuffle

    compressor = BloscCodec(cname="zstd", clevel=5, shuffle=BloscShuffle.shuffle)
    return {var: {"compressors": (compressor,)} for var in ds.data_vars}


def write_forecast_hour(
    ds: xr.Dataset,
    cycle: Cycle,
    forecast_hour: int,
    cfg: Config,
    store: Store | None = None,
) -> None:
    """Write one forecast hour's dataset to its Zarr group (spec 3.2 step 6)."""
    st = _store(cfg, store)
    url = st.url(forecast_hour_key(cfg, cycle, forecast_hour))
    # consolidated=True is additive (per-array metadata is still written, so a
    # reader that doesn't understand v3 consolidated metadata works regardless);
    # it just saves the frontend a metadata request per array. zarr-python warns
    # that v3 consolidated metadata isn't yet in the spec — expected, so silence
    # it here rather than emit it on every forecast-hour write.
    # TODO(phase2): confirm the chosen JS Zarr reader handles this store layout.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*[Cc]onsolidated metadata.*")
        ds.to_zarr(
            url,
            mode="w",
            encoding=_zarr_encoding(ds),
            storage_options=st.storage_options or None,
            consolidated=True,
            zarr_format=3,
        )
    logger.info("wrote forecast hour zarr", extra={
        "cycle": cycle.cycle_id, "fh": forecast_hour, "url": url,
        "n_vars": len(ds.data_vars),
    })


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


def _write_json(store: Store, key: str, payload: dict) -> None:
    data = json.dumps(payload, indent=2).encode()
    path = store.path(key)
    parent = path.rsplit("/", 1)[0]
    store.fs.makedirs(parent, exist_ok=True)
    store.fs.pipe_file(path, data)
