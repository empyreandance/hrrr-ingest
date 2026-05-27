#!/usr/bin/env bash
#
# Install/remove the hourly cron job that triggers one ingest run.
#
# HRRR cycles initialize every hour at HH:00Z and the data is reliably available
# ~50-60 min later, so we fire at minute 5 of every hour (= init + 65 min). The
# orchestrator works out which cycle that maps to (it subtracts the lag), so a
# late cron tick still picks the right cycle. Each run goes through
# run-cycle.sh, which retries a failed cycle a few times so a transient hiccup
# doesn't cost the hour; runs are serialized with flock so a slow cycle (or its
# retries) never overlaps the next tick (see LOCK_FILE / RUN_SCRIPT below).
#
# Usage:
#   deploy/cron.sh install     # add the hourly job
#   deploy/cron.sh uninstall   # remove it
#   deploy/cron.sh status      # show the current job

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INGEST_DIR=$(cd "$SCRIPT_DIR/../ingest" && pwd)
MARKER="# hrrr-ingest"
LOCK_FILE="$INGEST_DIR/.ingest.lock"
RUN_SCRIPT="$SCRIPT_DIR/run-cycle.sh"
# run-cycle.sh runs one cycle WITH retries (so a transient failure doesn't cost
# the hour). `flock -n` wraps it to make the whole attempt mutually exclusive: if
# a previous cycle is still going (e.g. a long 49-hour extended cycle, or its
# retries, on a slower/shared CPU), this tick exits immediately instead of
# stacking a second container on top — which would contend for RAM and risk an
# OOM kill. Cost is just a skipped tick, which the next hour recovers.
CRON_LINE="5 * * * * flock -n $LOCK_FILE $RUN_SCRIPT >> $INGEST_DIR/logs/cron.log 2>&1 $MARKER"

usage() { echo "usage: $0 {install|uninstall|status}" >&2; exit 1; }

current_without_ours() { crontab -l 2>/dev/null | grep -vF "$MARKER" || true; }

case "${1:-}" in
  install)
    mkdir -p "$INGEST_DIR/logs"
    { current_without_ours; echo "$CRON_LINE"; } | crontab -
    echo "installed hourly ingest cron (minute 5):"
    crontab -l | grep -F "$MARKER"
    ;;
  uninstall)
    current_without_ours | crontab -
    echo "removed hrrr-ingest cron job"
    ;;
  status)
    crontab -l 2>/dev/null | grep -F "$MARKER" || echo "no hrrr-ingest cron job installed"
    ;;
  *)
    usage
    ;;
esac
