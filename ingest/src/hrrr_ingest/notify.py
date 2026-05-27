"""Pushover failure notifications (spec 2.5).

Reuses the existing Pushover infrastructure used for severe-weather alerts.
Notifications are best-effort: a failed notification must never mask the
original ingest error, so send failures are logged and swallowed.
"""

from __future__ import annotations

import logging

import httpx

from .config import PushoverConfig

logger = logging.getLogger("hrrr_ingest.notify")

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def notify_failure(cfg: PushoverConfig, title: str, message: str) -> None:
    """Send a Pushover failure notification if configured.

    No-op (with a logged note) when Pushover credentials are absent.
    """
    if not cfg.enabled:
        logger.info("pushover not configured; skipping notification", extra={"title": title})
        return
    try:
        response = httpx.post(
            _PUSHOVER_URL,
            data={
                "token": cfg.token,
                "user": cfg.user_key,
                "title": title,
                "message": message,
                "priority": 1,
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except Exception as exc:  # best-effort; never raise from here
        logger.error("failed to send pushover notification", extra={"error": str(exc)})
