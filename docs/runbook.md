# Ingest runbook

Operational guide for the HRRR Threshold Tool ingest worker. Written to be
followed by future-you with no chat history (spec §8.5). If you're new to this,
read the "Mental model" section first.

> **First time deploying the whole tool?** Follow
> [deploy-checklist.md](deploy-checklist.md) start to finish — it sequences
> GitHub, R2, the website, and this worker. This runbook is the deeper reference
> for the ingest/R2 half and for ongoing operations.

## Mental model

The ingest worker is a **one-shot program**: each time it runs it figures out
the latest HRRR cycle, downloads + processes every forecast hour, writes the
results to Cloudflare R2 (cloud storage), and exits. A **cron job** on the
server runs it once an hour. Nothing stays running between cycles except cron.

Everything the worker needs is in environment variables (a `.env` file). The
worker is a Docker container, so the server only needs Docker — no Python, no
system packages.

## Recommended server (VPS)

The full physics suite (parcel lifting for effective-layer STP/SCP, DCAPE) needs
more than the original 8 GB / 2-CPU plan. Measured cost per forecast hour:
~150–170 s and ~2.3 GB RAM per worker (with `PARCEL_TILES=8`).

**Recommended:** Hetzner **CPX31** (4 vCPU, 8 GB RAM, ~€16/mo) in a US location
(Ashburn, VA — `ash`). With `INGEST_WORKERS=3` and `PARCEL_TILES=8` it processes
the 49-hour extended cycles in ~30–40 min, comfortably inside the hourly window.
Standard 19-hour cycles take ~10 min.

Sizing rule of thumb:
- `INGEST_WORKERS` ≈ number of vCPUs.
- `PARCEL_TILES` so that `WORKERS × 2.3 GB × (8 / PARCEL_TILES)` fits in RAM with
  headroom. (More tiles = less RAM per worker, slightly slower.)
- More headroom? Hetzner **CPX41** (8 vCPU, 16 GB, ~€30/mo), `INGEST_WORKERS=6`.

## First-time setup (provision a new host)

1. **Rent the box.** Hetzner Cloud → create server → Ubuntu 24.04 LTS, type
   CPX31, location Ashburn. Add your SSH key.

2. **Install Docker** (on the server, as root):
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

3. **Get the code** (this repo is the worker only; the website lives separately
   in the `research` repo):
   ```bash
   git clone https://github.com/empyreandance/hrrr-ingest.git
   cd hrrr-ingest/ingest
   ```

4. **Configure secrets.** Copy the template and fill it in:
   ```bash
   cp ../.env.example .env
   nano .env        # set R2_* credentials, PUSHOVER_* (optional), INGEST_WORKERS, PARCEL_TILES
   ```
   The R2 values come from Cloudflare → R2 → Manage API Tokens (create an S3
   access key) and your bucket name + account endpoint URL. This key is for the
   *worker to write*; making the bucket *readable by the website* is a separate
   step — see "R2 public access + CORS" below.

5. **Build the image** (compiles wgrib2 from source the first time — takes a few
   minutes):
   ```bash
   docker compose build
   ```

6. **Smoke test** — process the current cycle once, by hand, without touching R2:
   ```bash
   docker compose run --rm ingest --dry-run
   ```
   Optionally do a real **single-hour** run to a local folder (no R2 needed) to
   confirm the full fetch→compute→write path before wiring R2:
   ```bash
   STORAGE_URL=/data/test docker compose run --rm -e STORAGE_URL ingest \
       --cycle <YYYYMMDDHH> --forecast-hours 0
   ```
   Then a real single run (writes the current cycle to R2):
   ```bash
   docker compose run --rm ingest
   ```
   Check the log (`./logs/ingest.log`) ends with `"cycle complete"` and that the
   cycle appears in your R2 bucket under `cycles/<id>/` with a `manifest.json`.

7. **Schedule it.** Install the cron job (runs at HRRR init + 65 min, every hour):
   ```bash
   ../deploy/cron.sh install
   ```
   Verify with `crontab -l`.

8. **Watch for 48 hours** (spec §10.1.8). Confirm each cycle completes and the
   manifest flips to the newest cycle. Failures fire a Pushover alert if
   configured.

## R2 public access + CORS (so the website can read the data)

The worker writes to R2 with a secret S3 key. The **website**
(`https://research.alexcooke.co/hrrr/`, served by GitHub Pages from the
`research` repo) reads the same bucket from the visitor's browser — that needs
the bucket to be *publicly readable* and to send the right *CORS* headers. Do
this once, in the Cloudflare dashboard. It's the most common "the map is blank in
production but works locally" cause, so don't skip it.

**Why CORS matters here:** the Zarr reader fetches each data chunk with an HTTP
`Range` request from a *different* origin (`research.alexcooke.co` → `r2.dev`).
Browsers block cross-origin reads unless the server explicitly allows them, *and*
the reader needs to see the `Content-Range`/`Content-Length` reply headers to
parse a partial chunk. Both are configured below.

1. **Turn on public read.** Cloudflare → R2 → your bucket → **Settings**.
   Pick one:
   - **Quick (r2.dev):** under "Public access", enable **R2.dev subdomain**.
     You get a URL like `https://pub-<hash>.r2.dev`. Cloudflare rate-limits this
     domain and flags it as not-for-production — fine for a low-traffic
     forecaster tool, easiest to start with.
   - **Production (custom domain):** "Public access" → **Connect Domain**, e.g.
     `data.yourdomain.com` (the domain must be on your Cloudflare account). No
     rate-limit warning, and it survives switching buckets later.

   Whichever you choose, that base URL is what goes in the website's
   `hrrr/src/config.js` (in the `research` repo) as `DATA_BASE_URL` (see the
   deploy checklist). It is **not** the `*.r2.cloudflarestorage.com` S3 endpoint —
   that one always requires a signed request and will return 401 to the browser.

2. **Add the CORS policy.** Same Settings page → **CORS policy** → Edit → paste
   (the origin is the live site, scheme + host only, no path):
   ```json
   [
     {
       "AllowedOrigins": ["https://research.alexcooke.co"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedHeaders": ["Range", "If-None-Match"],
       "ExposeHeaders": ["Content-Range", "Content-Length", "ETag", "Accept-Ranges"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```
   - `AllowedOrigins` is the **site** origin (`https://research.alexcooke.co`),
     not the data URL and not the `/hrrr/` path — just scheme + host. You can list
     more than one (e.g. add a `*.github.io` preview origin if you ever test there).
   - Keep `Range` in `AllowedHeaders` and `Content-Range`/`Content-Length` in
     `ExposeHeaders` — the Zarr chunk reads fail without them.

3. **Verify from a terminal** (substitute your public base URL). A correct setup
   returns `access-control-allow-origin` and `content-range` headers and a `206`:
   ```bash
   curl -sI -H "Origin: https://research.alexcooke.co" -H "Range: bytes=0-99" \
     https://pub-<hash>.r2.dev/manifest.json | grep -i \
     'access-control-allow-origin\|content-range\|HTTP/'
   ```
   If you see the allow-origin header echoed back and a `206 Partial Content`,
   the browser will be happy too.

## Recover from a failed cycle

A failed run exits non-zero, logs `"cycle failed"`, and (if configured) sends a
Pushover alert. The previous cycle stays live in R2 (the manifest only flips
after a *complete* new cycle), so the frontend never sees broken data.

To re-run a specific cycle by hand:
```bash
cd hrrr-ingest/ingest
docker compose run --rm ingest --cycle 2026052400      # YYYYMMDDHH (UTC)
```
If it was a transient download failure, just let the next hourly run proceed.
If the GRIB wasn't published yet, wait and re-run with `--cycle`.

## Migrate to a new host

Because R2 is the stable layer, migration is "point a new box at the same R2":

1. Provision + install Docker on the new host (steps 1–3 above).
2. Copy the **same** `.env` (same R2 credentials) to the new host.
3. `docker compose build`, smoke-test, install cron (steps 5–7).
4. Remove cron on the old host: `deploy/cron.sh uninstall`, then destroy it.

No frontend change, no user-visible downtime: both hosts (briefly) or neither
write to the same bucket; the manifest stays consistent.

## Roll back the website

The website is **not** in this repo — it's the `hrrr/` folder of the `research`
repo (`empyreandance/research`), served by GitHub Pages straight from `main` at
`https://research.alexcooke.co/hrrr/`. There's no build step: pushing to `main`
republishes within a minute. To undo a bad change, revert it in the **research**
repo and push:
```bash
cd /path/to/research
git revert <sha> && git push          # republishes the prior state
```
The ingest/R2 data is untouched by website changes (and vice-versa).

## Where things live

- Logs: `ingest/logs/ingest.log` (one JSON object per line; `grep` it).
- Scratch GRIB downloads: the `ingest-work` Docker volume (auto-managed).
- Cron entry: `crontab -l` (installed by `deploy/cron.sh`).
- Config: `ingest/.env` (gitignored — the only place secrets live).
