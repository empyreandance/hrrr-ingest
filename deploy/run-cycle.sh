#!/usr/bin/env bash
#
# Cron entrypoint for one ingest cycle, WITH RETRIES.
#
# Why retries: on a shared/slower box a transient cause — a momentary resource
# spike, a brief NOAA/network blip, or an HRRR file that's a few minutes late —
# can fail a cycle. Rather than lose that hour's publish, we retry a few times
# with a short delay; a transient cause has usually cleared by the next attempt.
# A genuinely broken cycle still gives up after ATTEMPTS so we never wedge on it:
# the next hourly tick moves on to the newer cycle, and the last good cycle stays
# live the whole time. Every failure still fires a Pushover alert from inside the
# worker, and you can always rerun a specific cycle by hand:
#   deploy/run-cycle.sh --cycle YYYYMMDDHH
#
# Tunables (host environment overrides; sensible defaults below):
#   INGEST_ATTEMPTS     total attempts per cycle   (default 3)
#   INGEST_RETRY_DELAY  seconds between attempts   (default 180)
#
# deploy/cron.sh invokes this under `flock`, so retries never overlap the next
# hourly tick (a tick that finds a run still going just skips).

set -uo pipefail

INGEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../ingest" && pwd)
ATTEMPTS=${INGEST_ATTEMPTS:-3}
DELAY=${INGEST_RETRY_DELAY:-180}

cd "$INGEST_DIR"
for n in $(seq 1 "$ATTEMPTS"); do
  echo "[run-cycle] attempt ${n}/${ATTEMPTS} at $(date -u +%FT%TZ)"
  if docker compose run --rm ingest "$@"; then
    echo "[run-cycle] success on attempt ${n}"
    exit 0
  fi
  rc=$?
  echo "[run-cycle] attempt ${n} failed (exit ${rc})"
  if [ "${n}" -lt "${ATTEMPTS}" ]; then
    echo "[run-cycle] retrying in ${DELAY}s"
    sleep "${DELAY}"
  fi
done
echo "[run-cycle] all ${ATTEMPTS} attempts failed for this cycle (last good cycle stays live)"
exit 1
