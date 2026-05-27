"""Subset the raw GRIB2 down to the v1 variable set.

Spec 3.4 / 4.5: exclude the five hydrometeor mixing ratios. The preferred path
is wgrib2's ``-not_match`` to a smaller GRIB on disk (lower peak RSS and faster
cfgrib indexing); where wgrib2 is unavailable (Mac dev), we skip the physical
subset and let the catalog-driven selective open in ``compute`` do the
filtering — it only ever reads the fields we ask for, so the hydrometeors are
never materialized either way.

IMPORTANT (spec correction): in the real HRRR inventory the cloud-water mixing
ratio is named ``CLMR``, not ``CLWMR`` as spec 4.5 states. The pattern below
matches both so it is correct regardless.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .config import Config

logger = logging.getLogger("hrrr_ingest.subset")

# Hydrometeor mixing ratios excluded from the v1 subset. CLMR is the real
# wgrib2-inventory name; CLWMR is kept for robustness across HRRR versions.
EXCLUDE_PATTERN = ":(CLWMR|CLMR|CIMIXR|RWMR|SNMR|GRLE):"


def wgrib2_available() -> bool:
    """True if a ``wgrib2`` binary is on PATH (production VPS); else use cfgrib."""
    return shutil.which("wgrib2") is not None


def subset_grib(raw_path: Path, cfg: Config) -> Path:
    """Subset ``raw_path``, returning the path that ``compute`` should open.

    With wgrib2 present, writes a smaller GRIB and removes the raw file; without
    it, returns ``raw_path`` unchanged (filtering deferred to selective open).
    """
    if wgrib2_available():
        return _subset_with_wgrib2(raw_path, cfg)
    logger.warning(
        "wgrib2 not found; skipping physical subset (compute reads only cataloged "
        "fields, so hydrometeors are never loaded)",
        extra={"raw": str(raw_path)},
    )
    return raw_path


def _subset_with_wgrib2(raw_path: Path, cfg: Config) -> Path:
    """Run ``wgrib2 <in> -not_match PATTERN -grib_out <out>`` (spec 4.5)."""
    out_path = raw_path.with_name(raw_path.name.replace("raw.", "subset.", 1))
    cmd = ["wgrib2", str(raw_path), "-not_match", EXCLUDE_PATTERN, "-grib_out", str(out_path)]
    logger.info("subsetting with wgrib2", extra={"raw": str(raw_path), "out": str(out_path)})
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"wgrib2 subset failed (exit {exc.returncode}): {exc.stderr.strip()}"
        ) from exc

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"wgrib2 produced no output at {out_path}")

    # The subset is all we need downstream; drop the raw file to bound disk use.
    raw_path.unlink(missing_ok=True)
    logger.info("subset complete", extra={
        "out": str(out_path), "bytes": out_path.stat().st_size,
    })
    return out_path
