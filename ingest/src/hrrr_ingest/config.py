"""Runtime configuration, loaded entirely from environment variables.

Per spec 8.2, every operational knob lives in environment variables (loaded
from a gitignored ``.env`` in development). No secrets or paths are hard-coded.
See ``.env.example`` at the repo root for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or invalid."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"required environment variable {name!r} is not set")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"environment variable {name!r} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class R2Config:
    """Cloudflare R2 (S3-compatible) connection settings."""

    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    # Key prefix under which cycles and the manifest live, e.g. "" or "hrrr/".
    prefix: str = ""

    @property
    def storage_options(self) -> dict:
        """fsspec/s3fs storage_options for ``xarray.Dataset.to_zarr``."""
        return {
            "key": self.access_key_id,
            "secret": self.secret_access_key,
            "client_kwargs": {"endpoint_url": self.endpoint_url},
        }


@dataclass(frozen=True)
class PushoverConfig:
    """Pushover credentials for failure notifications (spec 2.5). Optional."""

    token: str | None = None
    user_key: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.user_key)


@dataclass(frozen=True)
class Config:
    """Top-level ingest configuration."""

    r2: R2Config
    pushover: PushoverConfig
    # Base URL for the NOAA HRRR bucket over HTTPS (spec 3.1).
    noaa_base_url: str
    # Number of parallel forecast-hour workers (spec 2.1: two-worker parallel).
    workers: int
    # Dask chunk size along the isobaric level axis (spec 3.3, mandatory).
    isobaric_chunk: int
    # Number of spatial (y-axis) tiles for the parcel engine. Higher = lower
    # peak RSS per worker (parcel intermediates and per-tile GRIB load shrink
    # ~1/N), at a small loop-overhead cost. Lets a smaller box run more workers.
    parcel_tiles: int
    # Optional storage override. When set to a local path or file:// URL, the
    # whole pipeline writes Zarr + manifests there instead of R2 — lets you run
    # an end-to-end test with no R2/server. An s3:// URL uses the R2 credentials.
    storage_url: str | None
    # Structured log file destination on the VPS (spec 2.5).
    log_file: str
    log_level: str
    # Scratch directory for downloaded/subset GRIB before it is opened.
    work_dir: str = field(default="/tmp/hrrr-ingest")

    @classmethod
    def from_env(cls, *, load_dotenv_file: bool = True, require_r2: bool = True) -> Config:
        """Build a Config from the process environment (and ``.env`` if present).

        ``require_r2=False`` lets a dry run proceed without R2 credentials; the
        R2 fields are then filled with empty placeholders and must not be used.
        """
        if load_dotenv_file:
            load_dotenv()
        r2_get = _require if require_r2 else (lambda name: _optional(name, ""))
        return cls(
            r2=R2Config(
                endpoint_url=r2_get("R2_ENDPOINT_URL"),
                access_key_id=r2_get("R2_ACCESS_KEY_ID"),
                secret_access_key=r2_get("R2_SECRET_ACCESS_KEY"),
                bucket=r2_get("R2_BUCKET"),
                prefix=_optional("R2_PREFIX", ""),
            ),
            pushover=PushoverConfig(
                token=os.environ.get("PUSHOVER_TOKEN"),
                user_key=os.environ.get("PUSHOVER_USER_KEY"),
            ),
            noaa_base_url=_optional(
                "NOAA_HRRR_BASE_URL",
                "https://noaa-hrrr-bdp-pds.s3.amazonaws.com",
            ),
            workers=_int("INGEST_WORKERS", 2),
            isobaric_chunk=_int("ISOBARIC_CHUNK", 10),
            parcel_tiles=_int("PARCEL_TILES", 8),
            storage_url=os.environ.get("STORAGE_URL") or None,
            log_file=_optional("LOG_FILE", "/var/log/hrrr-ingest/ingest.log"),
            log_level=_optional("LOG_LEVEL", "INFO"),
            work_dir=_optional("WORK_DIR", "/tmp/hrrr-ingest"),
        )
