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

> Driven live with Claude over SSH/Tailscale; this doc is the script.

---

## What already exists (don't redo)

The following groundwork was finished on 2026-05-28 → 2026-05-30 and is already
live — these steps are **NOT** part of the migration:

- **DNS is on Cloudflare.** `alexcooke.co` nameservers were moved from Namecheap
  to Cloudflare; all records (including `research`, `dewpoint-data`, `ohx-data`,
  apex, MX/SPF for email forwarding) are in the Cloudflare zone, **DNS only**
  (grey-cloud).
- **`hrrr-data.alexcooke.co` custom R2 domain is connected** and serving the
  live HRRR manifest. The frontend's `DATA_BASE_URL` already points at it.
- **Dual-cycle pointer is in `hrrr-ingest`.** `publish.py` writes both
  `current_cycle` and `current_extended_cycle` to the global manifest, and the
  orchestrator preserves the extended cycle's data through standard runs. The
  frontend already reads both. The Mac inherits this code via `git clone`.

So you should **not** need to touch DNS records or connect the HRRR R2 domain
during this migration. The remaining R2 domains to set up are QLCS's
(`ohx-data.alexcooke.co`) and Dewpoint's (`dewpoint-data.alexcooke.co`) — both
currently still point at the Contabo box and will be repointed in §5 and §6.

---

## 0. Before you start
- Mac Studio powered on, on the network, you have an admin account.
- R2 credentials (same ones the Contabo `.env` uses).
- Cloudflare dashboard access (R2 buckets + DNS — but DNS is mostly done).
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

> **R2 access:** The Mac writes to R2 via the S3 endpoint with R2 credentials
> (same code path as Contabo, just running natively, no Docker). The frontend
> reads via `hrrr-data.alexcooke.co`, which is already wired up — no DNS
> changes here.

- Confirm a full cycle time. On the M1 Max / 128 GB with `INGEST_WORKERS=8`
  this should land in the **~10–35 min** range — much faster than the
  ~2–3 hours we saw on CPX31. Measure once and write it down; that number
  determines the watchdog staleness threshold later (see §11).
- **Schedule it:** use the included `deploy/run-cycle-mac.sh` (native
  `uv run` retry wrapper — the macOS equivalent of `run-cycle.sh`'s Docker
  invocation) fired by a **LaunchDaemon** `com.alexcooke.hrrr-ingest` with
  `StartCalendarInterval` at **minute 5** each hour. *No `flock` needed on
  macOS* — launchd won't start a second copy while one is running, which is
  the overlap guard for free. (Plist template in the appendix.)
- After the next 00/06/12/18 Z cycle completes, **verify the dual-cycle
  pointer landed**:
  ```bash
  curl -s https://hrrr-data.alexcooke.co/manifest.json | jq
  # should show both current_cycle AND current_extended_cycle (+ their keys)
  ```
  Open the live site and check the new **"Extended forecast (48 hr)"**
  checkbox appears below the cycle-info line in the sidebar. Tick it; the FH
  slider should extend to f48 and the data should still load.

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
- **R2 + DNS:** *(DNS is already on Cloudflare per the prelude — this step is
  just creating the R2 bucket, connecting the domain, and removing the
  Contabo A-record that currently owns `ohx-data`.)*
  - Create R2 bucket **`ohx-qlcs`**; CORS `AllowedOrigins: ["https://research.alexcooke.co"]`, `AllowedMethods: ["GET","HEAD"]`.
  - Connect custom domain **`ohx-data.alexcooke.co`** to that bucket (Cloudflare → R2 → Settings → Connect Domain). Delete the existing `ohx-data` A-record (currently `157.173.196.40` → Contabo) once the connect step finishes.
  - Set **`Cache-Control: no-store`** on `data/latest.json` (Cloudflare cache rule or object metadata) so the 10-s feed is never stale.
  - The `tennqlcs/` frontend is unchanged — same `ohx-data.alexcooke.co` URLs, now served by R2.

---

## 6. Dewpoint EFI — repoint off Contabo
Its compute is already free (GitHub Actions) and the workflow **already uploads
to R2**; it just *also* rsyncs to Contabo, and the domain points at Contabo.
- Find the workflow's R2 target bucket: `research/.github/workflows/dewpoint-efi-update.yml`.
- Connect **`dewpoint-data.alexcooke.co`** to that R2 bucket (custom domain in
  Cloudflare → R2 → Settings → Connect Domain). Delete the existing
  `dewpoint-data` A-record (currently `157.173.196.40` → Contabo) once the
  connect step finishes.
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
- **HRRR:** a fresh cycle every ~hour on `research.alexcooke.co/hrrr/`. Note
  the actual cycle duration in `~/hrrr-ingest/ingest/logs/ingest.log`; you'll
  need this number to set the watchdog staleness threshold in §11.
- **Dual-cycle pointer:** after the first 00/06/12/18 Z run on the Mac, confirm
  `manifest.json` carries `current_extended_cycle` and the frontend's
  "Extended forecast (48 hr)" toggle appears + works.
- **QLCS:** `ohx-data.alexcooke.co/data/latest.json` updating every ~10 s; viewer live.
- **Dewpoint:** still updating on its daily schedule, now from R2.
- Watch the Mac's logs; confirm it auto-restarts after a manual reboot test.

## 9. Ship to Ohio
Partner plugs in **power + ethernet**. It auto-resumes (LaunchDaemons + autorestart).
Verify remotely via Tailscale. New IP/city is irrelevant — everything is outbound.

## 10. Cancel Contabo → bill ~$0
After a few days stable on the Mac with all three migrated, cancel the Contabo
VPS. **Trade-off:** cancelling saves ~$5/mo but the HRRR failover becomes
"rebuild on a fresh VPS (~30 min, see [runbook.md](runbook.md))" instead of a
1-command re-enable. If you'd rather keep an instant hot standby, keep Contabo
(~$5/mo) with its HRRR cron disabled. *(Or skip Contabo entirely and rely on
the GHA watchdog from §11 — that's a free pay-as-you-go safety net.)*

---

## 11. Post-migration safety net: GHA watchdog *(do this after §8)*

If/when the Mac is offline for a sustained period (power outage in Ohio,
hardware fault, ISP blip), we want a free pay-as-you-go cloud fallback that
keeps each site moving forward — slower than the Mac's hourly cadence, but
better than stale data.

Pattern: a `.github/workflows/watchdog.yml` in **each** tool's repo
(`hrrr-ingest`, `dewpoint-efi-mclimate`, the QLCS repo) that:

1. Runs hourly at minute 20 (~15 min after the Mac's cron).
2. Curls the tool's public manifest, reads its `updated` timestamp.
3. If older than `~2× the normal cycle duration`, runs the ingest itself,
   tagging the output (e.g. `"writer": "github-actions-watchdog"` in
   manifest.json) so the frontend can show a small "backup mode" banner.

GHA's `ubuntu-latest` runners are 4 vCPU / 16 GB — roughly the CPX31 profile,
so cycle times in fallback mode are 2–3 hours per HRRR cycle (vs the Mac's
~30 min). The site still moves forward, just slower; forecasters see the
banner and know to expect slightly older data.

Cost: $0 on public repos (unlimited Actions minutes); the secret
(`HRRR_R2_ACCESS_KEY_ID` / `HRRR_R2_SECRET_ACCESS_KEY`) is already in the org
from the WPC ERO mirror workflow.

Effort: ~1–2 h per tool, ~4–6 h total for all three.

**Why post-migration, not pre:** the staleness threshold needs to be calibrated
against the Mac's actual cycle durations, which you'll only know after a few
days on the new hardware. Setting it up too early risks tripping the watchdog
during a normal slow Mac run.

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
Two paths:
- **Long-term:** stand up a worker on any VPS in ~30 min — clone `hrrr-ingest`,
  drop in `.env`, `docker compose build`, `deploy/cron.sh install`. Fully
  covered in [runbook.md](runbook.md) ("First-time setup" / "Migrate to a new
  host"). The website keeps serving the last good cycle from R2 the whole time.
- **Short-term:** if the §11 watchdog is installed, the site keeps updating
  from GitHub Actions automatically while you build the new host. Banner
  tells users to expect slower refresh.
