"""Ingest orchestrator: the top-level control flow for one HRRR cycle.

Responsibilities (spec 10.1.3):
  1. Compute the target cycle from the wall clock (or take an explicit --cycle).
  2. Iterate the cycle's forecast hours across a small process pool
     (spec 2.1: two-worker parallel).
  3. Per forecast hour: fetch -> subset -> compute -> write Zarr to R2.
  4. Validate cycle completeness.
  5. Write the cycle manifest, atomically promote the global manifest, and drop
     the previous cycle (spec 2.2 / 3.2).

On failure, fire a Pushover notification (spec 2.5) and exit non-zero.

The per-step work (fetch/subset/compute/publish) is stubbed; this module wires
the stubs together and implements the real orchestration. ``--dry-run`` walks
the full control flow without touching the network or R2, which is what the
skeleton is exercised with today.
"""

from __future__ import annotations

import argparse
import gc
import logging
import os
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from . import compute, fetch, publish, subset
from .config import Config, ConfigError
from .cycle import Cycle, current_cycle, parse_cycle_id
from .logging_setup import configure_logging
from .notify import notify_failure

logger = logging.getLogger("hrrr_ingest.orchestrator")


class IngestError(RuntimeError):
    """Raised when a cycle cannot be completed (e.g. forecast hours missing)."""


@dataclass
class ForecastHourResult:
    forecast_hour: int
    status: str  # "ok" | "failed"
    error: str | None = None
    parameters: list[dict] | None = None  # per-variable metadata for the manifest

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _init_worker(log_level: str, log_file: str | None) -> None:
    """Pool initializer: give each worker process the same JSON log handlers.

    NOTE: multiple processes append to one log file here, which is adequate at
    skeleton volume; a QueueHandler/QueueListener is the robust fix if write
    interleaving becomes a problem (TODO(phase1)).
    """
    configure_logging(level=log_level, log_file=log_file)


def process_forecast_hour(
    cycle: Cycle, forecast_hour: int, cfg: Config, dry_run: bool
) -> ForecastHourResult:
    """Run the full per-forecast-hour pipeline. Runs in a pool worker process.

    Must stay top-level and picklable for ProcessPoolExecutor. Any exception is
    captured into the result so one bad forecast hour can't crash the pool.
    """
    log = logging.getLogger("hrrr_ingest.worker")
    if dry_run:
        log.info("dry-run: would process forecast hour", extra={
            "cycle": cycle.cycle_id, "fh": forecast_hour,
            "grib_key": cycle.grib_key(forecast_hour),
        })
        return ForecastHourResult(forecast_hour, "ok")

    local_grib = None
    try:
        raw_path = fetch.fetch_grib(cycle, forecast_hour, cfg)
        local_grib = raw_path
        subset_path = subset.subset_grib(raw_path, cfg)
        local_grib = subset_path
        ds = compute.build_forecast_hour_dataset(
            subset_path, cfg, cycle_id=cycle.cycle_id, forecast_hour=forecast_hour
        )
        publish.write_forecast_hour(ds, cycle, forecast_hour, cfg)
        parameters = publish.parameter_metadata(ds)

        # Reclaim memory before this worker is recycled (spec 3.3).
        del ds
        gc.collect()
        return ForecastHourResult(forecast_hour, "ok", parameters=parameters)
    except Exception as exc:  # noqa: BLE001 - reported back to the parent
        log.exception("forecast hour failed", extra={
            "cycle": cycle.cycle_id, "fh": forecast_hour,
        })
        return ForecastHourResult(forecast_hour, "failed", error=str(exc))
    finally:
        # Always delete the downloaded GRIB so disk use stays bounded (spec 3.3).
        if local_grib is not None:
            local_grib.unlink(missing_ok=True)


def run_cycle(
    cycle: Cycle, cfg: Config, *, dry_run: bool = False, forecast_hours: list[int] | None = None
) -> int:
    """Ingest one cycle end to end. Returns a process exit code (0 == success).

    ``forecast_hours`` (e.g. for testing or partial reruns) limits which hours to
    process; defaults to the cycle's full set.
    """
    forecast_hours = forecast_hours or cycle.forecast_hours
    logger.info("starting cycle", extra={
        "cycle": cycle.cycle_id,
        "extended": cycle.is_extended,
        "forecast_hours": len(forecast_hours),
        "workers": cfg.workers,
        "wgrib2": subset.wgrib2_available(),
        "dry_run": dry_run,
    })

    # --- fan out forecast hours across the worker pool (spec 2.1) ---
    # max_tasks_per_child=1 recycles each worker after a forecast hour so peak
    # RSS is reclaimed between hours (spec 3.3).
    results: dict[int, ForecastHourResult] = {}
    with ProcessPoolExecutor(
        max_workers=cfg.workers,
        max_tasks_per_child=1,
        initializer=_init_worker,
        initargs=(cfg.log_level, cfg.log_file),
    ) as pool:
        futures = {
            pool.submit(process_forecast_hour, cycle, fh, cfg, dry_run): fh
            for fh in forecast_hours
        }
        for future in as_completed(futures):
            result = future.result()
            results[result.forecast_hour] = result
            logger.info("forecast hour done", extra={
                "cycle": cycle.cycle_id, "fh": result.forecast_hour,
                "status": result.status, "error": result.error,
            })

    # --- validate completeness (spec 3.2 step 3) ---
    missing = [fh for fh in forecast_hours if fh not in results]
    failed = [fh for fh, r in results.items() if not r.ok]
    if missing or failed:
        raise IngestError(
            f"cycle {cycle.cycle_id} incomplete: "
            f"missing={[f'f{h:02d}' for h in sorted(missing)]} "
            f"failed={[f'f{h:02d}' for h in sorted(failed)]}"
        )

    # --- publish: manifest + atomic promote + drop previous (spec 2.2) ---
    if dry_run:
        logger.info("dry-run: would write cycle manifest, promote, drop previous", extra={
            "cycle": cycle.cycle_id, "cycle_prefix": publish.cycle_prefix(cfg, cycle),
        })
    else:
        parameters = next((r.parameters for r in results.values() if r.parameters), None)
        publish.write_cycle_manifest(cycle, forecast_hours, cfg, parameters=parameters)
        previous = publish.read_current_cycle_id(cfg)
        publish.promote_cycle(cycle, cfg)
        if previous and previous != cycle.cycle_id:
            publish.delete_cycle(parse_cycle_id(previous), cfg)
            logger.info("dropped previous cycle", extra={"previous": previous})

    # Remove the (now-empty) scratch dir for this cycle.
    shutil.rmtree(Path(cfg.work_dir) / cycle.cycle_id, ignore_errors=True)
    logger.info("cycle complete", extra={"cycle": cycle.cycle_id})
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hrrr-ingest", description="HRRR cycle ingest worker")
    parser.add_argument(
        "--cycle", metavar="YYYYMMDDHH",
        help="explicit cycle to ingest (for reruns); default derives from the clock",
    )
    parser.add_argument(
        "--lag-minutes", type=int, default=65,
        help="availability lag used to pick the current cycle (spec 3.2; default 65)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="walk the control flow without fetching, computing, or writing to R2",
    )
    parser.add_argument(
        "--forecast-hours", metavar="LIST",
        help="comma-separated forecast hours to process (e.g. '0' or '0,3,6'); "
             "default is the cycle's full set. For testing and partial reruns.",
    )
    return parser.parse_args(argv)


def _load_config(*, dry_run: bool) -> Config:
    # R2 credentials aren't needed for a dry run, nor when STORAGE_URL points at
    # a local folder (an end-to-end test with no R2).
    storage_url = os.environ.get("STORAGE_URL")
    local_storage = bool(storage_url) and not storage_url.startswith("s3://")
    return Config.from_env(load_dotenv_file=False, require_r2=not (dry_run or local_storage))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse_args(argv)

    log = configure_logging(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        log_file=os.environ.get("LOG_FILE") or None,
    )

    try:
        cfg = _load_config(dry_run=args.dry_run)
    except ConfigError as exc:
        log.error("configuration error", extra={"error": str(exc)})
        return 2

    cycle = (
        parse_cycle_id(args.cycle)
        if args.cycle
        else current_cycle(lag_minutes=args.lag_minutes)
    )
    forecast_hours = None
    if args.forecast_hours:
        requested = [int(x) for x in args.forecast_hours.split(",") if x.strip() != ""]
        valid = set(cycle.forecast_hours)
        forecast_hours = [h for h in requested if h in valid]
        if not forecast_hours:
            log.error("no valid forecast hours requested", extra={
                "requested": requested, "cycle": cycle.cycle_id})
            return 2

    try:
        return run_cycle(cycle, cfg, dry_run=args.dry_run, forecast_hours=forecast_hours)
    except Exception as exc:  # noqa: BLE001 - top-level guard; notify and exit
        log.exception("cycle failed", extra={"cycle": cycle.cycle_id})
        notify_failure(
            cfg.pushover,
            title=f"HRRR ingest failed: {cycle.cycle_id}",
            message=str(exc),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
