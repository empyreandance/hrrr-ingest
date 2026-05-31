#!/usr/bin/env bash
#
# launchd entrypoint for one ingest cycle on the Mac Studio, WITH RETRIES.
#
# Same retry contract as deploy/run-cycle.sh (the Linux/Docker version), but
# this one invokes the package natively via `uv run` instead of through
# docker compose — the Mac runs the worker as a LaunchDaemon, not under Docker.
#
# Why retries: on a shared/slower box (or right after a router blip) a
# transient cause can fail an attempt; retrying after a short delay almost
# always succeeds. A genuinely broken cycle still gives up after ATTEMPTS so
# we never wedge on it: the next hourly tick moves on to the newer cycle, and
# the last good cycle stays live on R2 the whole time.
#
# Tunables (environment overrides; sensible defaults below):
#   INGEST_ATTEMPTS     total attempts per cycle   (default 3)
#   INGEST_RETRY_DELAY  seconds between attempts   (default 180)
#
# launchd serializes runs by default — it won't start a second copy while one
# is still running — which is the macOS equivalent of `flock` on Linux. So a
# tick that lands while the previous run is still going simply gets skipped.

set -uo pipefail

# Anchor on the script's own location so launchd's empty PATH/CWD don't break us.
INGEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../ingest" && pwd)
ATTEMPTS=${INGEST_ATTEMPTS:-3}
DELAY=${INGEST_RETRY_DELAY:-180}

# Homebrew lives at /opt/homebrew/bin on Apple Silicon; launchd starts with an
# almost-empty PATH, so make sure uv + eccodes are findable regardless.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export ECCODES_DIR="${ECCODES_DIR:-$(brew --prefix eccodes 2>/dev/null || echo /opt/homebrew/opt/eccodes)}"

cd "$INGEST_DIR"
for n in $(seq 1 "$ATTEMPTS"); do
  echo "[run-cycle-mac] attempt ${n}/${ATTEMPTS} at $(date -u +%FT%TZ)"
  if uv run python -m hrrr_ingest.orchestrator "$@"; then
    echo "[run-cycle-mac] success on attempt ${n}"
    exit 0
  fi
  rc=$?
  echo "[run-cycle-mac] attempt ${n} failed (exit ${rc})"
  if [ "${n}" -lt "${ATTEMPTS}" ]; then
    echo "[run-cycle-mac] retrying in ${DELAY}s"
    sleep "${DELAY}"
  fi
done
echo "[run-cycle-mac] all ${ATTEMPTS} attempts failed for this cycle (last good cycle stays live)"
exit 1
