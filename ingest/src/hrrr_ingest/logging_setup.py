"""Structured logging setup.

Spec 2.5: ingest writes a structured log file on the VPS. We emit one JSON
object per line (to both the log file and stderr) so cycle runs are greppable
and machine-parseable without pulling in a logging framework. Extra context is
attached via ``logger.info("msg", extra={"context": {...}})``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed via extra={...} that isn't a reserved LogRecord attr.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", log_file: str | None = None) -> logging.Logger:
    """Configure the root ``hrrr_ingest`` logger and return it.

    Logs to stderr always, and to ``log_file`` if its directory is writable.
    """
    logger = logging.getLogger("hrrr_ingest")
    logger.setLevel(level.upper())
    logger.handlers.clear()
    logger.propagate = False

    formatter = JsonFormatter()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:  # don't let a missing log dir kill the run
            logger.warning(
                "could not open log file",
                extra={"log_file": log_file, "error": str(exc)},
            )

    return logger
