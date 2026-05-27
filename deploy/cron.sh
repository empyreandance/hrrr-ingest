#!/usr/bin/env bash
#
# Install/remove the hourly cron job that triggers one ingest run.
#
# HRRR cycles initialize every hour at HH:00Z and the data is reliably available
# ~50-60 min later, so we fire at minute 5 of every hour (= init + 65 min). The
# orchestrator works out which cycle that maps to (it subtracts the lag), so a
# late cron tick still picks the right cycle.
#
# Usage:
#   deploy/cron.sh install     # add the hourly job
#   deploy/cron.sh uninstall   # remove it
#   deploy/cron.sh status      # show the current job

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INGEST_DIR=$(cd "$SCRIPT_DIR/../ingest" && pwd)
MARKER="# hrrr-ingest"
CRON_LINE="5 * * * * cd $INGEST_DIR && docker compose run --rm ingest >> $INGEST_DIR/logs/cron.log 2>&1 $MARKER"

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
