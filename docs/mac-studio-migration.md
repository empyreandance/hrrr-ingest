# Mac Studio migration — consolidate all paid compute, drop the cloud bill

Goal: move the two tools that need a server — **HRRR** and **QLCS** — onto the
**Mac Studio (M1 Max / 128 GB)**, serve everything from **R2 + GitHub Pages**,
repoint **Dewpoint EFI** off the Contabo box, and then **cancel Contabo** so the
monthly cloud bill goes to ~$0 (R2 is ~$2–3/mo for everything; storage pennies).

The other 7 tools already run free on GitHub Actions — they are not touched.

**Where/when:** do the whole thing in California this weekend with the Mac in
front of you, run it under your eye for ~a week, then it ships to Ohio and
**auto-resumes** on plug-in (it's outbound-only; nothing connects *to* it).

**Time:** ~2.5–3.5 h hands-on (QLCS is the variable), plus background waits.

> Driven live with Claude over SSH/Tailscale; this doc is the script. Items
> marked **(confirm live)** are small specifics we nail down while doing it.

---

## 0. Before you start
- Mac Studio powered on, on the network, you have an admin account.
- R2 credentials (same ones the Contabo `.env` uses).
- Cloudflare dashboard access (for R2 buckets + the DNS repoints).
- GitHub access (the `research` + `hrrr-ingest` repos).
- The Contabo box reachable (we copy the QLCS code off it, then retire it).

---

## 1. Harden the Mac for unattended + remote operation
So it survives reboots/power blips and you can manage it from anywhere.

```bash
# Homebrew (if not already installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Remote access from anywhere (no port-forwarding, survives the move to Ohio)
brew install --cask tailscale     # then open it, sign in; note the Tailscale name/IP
```
- **Enable SSH:** System Settings → General → Sharing → **Remote Login = On**.
- **Never sleep + auto-recover from power loss:**
  ```bash
  sudo pmset -a sleep 0 displaysleep 0 disksleep 0 powernap 0 autorestart 1
  ```
- **FileVault: turn OFF** (System Settings → Privacy & Security → FileVault).
  Rationale: an encrypted disk stops at a password prompt on reboot *before* our
  jobs can start — fatal for a headless box in Ohio. This is a dedicated
  appliance with no personal data, so off is the right call. *(If you'd rather
  keep it on, the box can't auto-recover from a power cut without someone typing
  the password — decide now.)*
- We run everything as **LaunchDaemons** (in `/Library/LaunchDaemons`, run as
  root at boot **without anyone logged in**), so no auto-login is needed.

## 2. Shared tooling
```bash
brew install eccodes git rclone uv
```
`eccodes` is the C library both cfgrib (HRRR) and pygrib (QLCS) need.

## 3. rclone → R2 (used by QLCS sync + verification)
```bash
rclone config
#  name: r2 | type: s3 | provider: Cloudflare
#  access_key_id / secret_access_key: your R2 keys
#  endpoint: https://817fc9f791704e3418596f7427194bc2.r2.cloudflarestorage.com
#  (leave region blank)
rclone lsd r2:                      # should list your buckets
```

---

## 4. HRRR worker on the Mac (native, no Docker)
```bash
git clone https://github.com/empyreandance/hrrr-ingest.git ~/hrrr-ingest
cd ~/hrrr-ingest/ingest
cp ../.env.example .env
# edit .env:
#   R2_* = same creds as Contabo;  STORAGE_URL= (blank, -> R2)
#   INGEST_WORKERS=8        (128 GB, so RAM is no constraint)
#   PARCEL_TILES=4          (low tiles = faster; ~3.5 GB/worker x8 = ~28 GB, fine)
#   WORK_DIR=/tmp/hrrr-ingest   LOG_FILE=~/hrrr-ingest/ingest/logs/ingest.log
uv sync
# if cfgrib can't find eccodes:  export ECCODES_DIR="$(brew --prefix eccodes)"

# smoke test
uv run python -m hrrr_ingest.orchestrator --dry-run
uv run python -m hrrr_ingest.orchestrator --forecast-hours 0   # writes f00 to R2
```
- Confirm a full cycle time (should be ~10–35 min — measure once).
- **Schedule it:** a native retry wrapper (`deploy/run-cycle-mac.sh`, **confirm
  live** — same retry logic as `run-cycle.sh` but `uv run` instead of Docker)
  fired by a **LaunchDaemon** `com.alexcooke.hrrr-ingest` with
  `StartCalendarInterval` at **minute 5** each hour. *No `flock` needed on macOS*
  — launchd won't start a second copy while one is running, which is the overlap
  guard for free. (Plist template in the appendix.)

---

## 5. QLCS on the Mac
It's a self-contained poller (`backend/update.py --loop`) that pulls MRMS + HRRR
from S3, renders PNGs + `latest.json` into `docs/data/`, served as static files.
Same shape as HRRR → **R2-push**, no inbound serving.

```bash
# copy the code off Contabo (skip the 217 MB of historical frames)
rsync -az --exclude 'docs/data/history' root@157.173.196.40:/opt/ohx-qlcs/ ~/ohx-qlcs/
cd ~/ohx-qlcs
python3 -m venv venv && source venv/bin/activate
pip install numpy pygrib        # + any other imports update.py needs (confirm live)
python backend/update.py        # single cycle: confirm it pulls MRMS + writes docs/data/latest.json
```
- **Daemon:** LaunchDaemon `com.alexcooke.ohx-qlcs` running `update.py --loop`
  (`RunAtLoad=true`, `KeepAlive=true` → starts on boot, restarts if it crashes).
- **R2-sync sidecar:** LaunchDaemon `com.alexcooke.ohx-qlcs-sync` running a loop
  that `rclone sync ~/ohx-qlcs/docs/ r2:ohx-qlcs/` every ~10 s. (No edit to
  `update.py` — it just writes files; the sidecar ships them.)
- **R2 + DNS:**
  - Create R2 bucket **`ohx-qlcs`**; CORS `AllowedOrigins: ["https://research.alexcooke.co"]`, `AllowedMethods: ["GET","HEAD"]`.
  - Connect custom domain **`ohx-data.alexcooke.co`** to that bucket (Cloudflare → R2 → Settings → Connect Domain). This **replaces** the current DNS record that points at Contabo.
  - Set **`Cache-Control: no-store`** on `data/latest.json` (Cloudflare cache rule or object metadata) so the 10-s feed is never stale.
  - The `tennqlcs/` frontend is unchanged — same `ohx-data.alexcooke.co` URLs, now served by R2.

---

## 6. Dewpoint EFI — repoint off Contabo
Its compute is already free (GitHub Actions) and the workflow **already uploads
to R2**; it just *also* rsyncs to Contabo, and the domain points at Contabo.
- Find the workflow's R2 target bucket: `research/.github/workflows/dewpoint-efi-update.yml` (**confirm live**).
- Connect **`dewpoint-data.alexcooke.co`** to that R2 bucket (custom domain), replacing the Contabo DNS record.
- Delete the `rsync ... root@157.173.196.40:/var/www/dewpoint-data/` step (and the `VPS_SSH_KEY` bits) from the workflow.
- Confirm `dewpoint-efi/index.html` (`R2_BASE`) still loads — same URL, now R2.

---

## 7. Cutover (stop the duplicates on Contabo)
Once the Mac is producing all three and the DNS has flipped:
```bash
ssh root@157.173.196.40 '
  cd /root/hrrr-ingest && deploy/cron.sh uninstall      # stop HRRR on Contabo
  systemctl disable --now ohx-qlcs.service              # stop QLCS on Contabo
'
```
Verify nothing is double-writing R2.

## 8. Shakeout (the week in California)
- HRRR: a fresh cycle every hour (`research.alexcooke.co/hrrr/`).
- QLCS: `ohx-data.alexcooke.co/data/latest.json` updating every ~10 s; viewer live.
- Dewpoint: still updating on its daily schedule, now from R2.
- Watch the Mac's logs; confirm it auto-restarts after a manual reboot test.

## 9. Ship to Ohio
Partner plugs in **power + ethernet**. It auto-resumes (LaunchDaemons + autorestart).
Verify remotely via Tailscale. New IP/city is irrelevant — everything is outbound.

## 10. Cancel Contabo → bill ~$0
After a few days stable on the Mac with all three migrated, cancel the Contabo
VPS. **Trade-off:** cancelling saves ~$5/mo but the HRRR failover becomes
"rebuild on a fresh VPS (~30 min, see [runbook.md](runbook.md))" instead of a
1-command re-enable. If you'd rather keep an instant hot standby, keep Contabo
(~$5/mo) with its HRRR cron disabled. Your call.

---

## Appendix

### LaunchDaemon: HRRR (hourly, serialized)
`/Library/LaunchDaemons/com.alexcooke.hrrr-ingest.plist` — `StartCalendarInterval`
`{Minute: 5}`, `ProgramArguments` → `deploy/run-cycle-mac.sh`, full PATH incl.
`/opt/homebrew/bin`, `StandardOut/ErrorPath` → a log. (Exact plist generated live.)

### LaunchDaemon: QLCS daemon + sync
`com.alexcooke.ohx-qlcs.plist` (`RunAtLoad`+`KeepAlive`, runs `update.py --loop`)
and `com.alexcooke.ohx-qlcs-sync.plist` (the rclone-sync loop). Generated live.

### Failover if the Mac dies (and Contabo is gone)
Stand up a worker on any VPS in ~30 min: clone `hrrr-ingest`, drop in `.env`,
`docker compose build`, `deploy/cron.sh install`. Fully covered in
[runbook.md](runbook.md) ("First-time setup" / "Migrate to a new host"). The
website keeps serving the last good cycle from R2 the whole time.
