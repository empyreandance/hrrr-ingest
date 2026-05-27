# Deploy checklist (first time live)

A start-to-finish, do-this-in-order list to take the tool live. Written for
someone who hasn't done this before — every step says *where* to click and *how
to know it worked*.

There are **three moving parts**, and they connect like this:

```
  Ingest worker (a VPS)  --writes-->  R2 bucket (storage)  <--reads--  Website
   repo: hrrr-ingest                  Cloudflare R2                    research/hrrr/
   runs hourly on a server                                             research.alexcooke.co/hrrr/
```

Two GitHub repos are involved:
- **`empyreandance/research`** — your existing site (served by GitHub Pages from
  `main` at `research.alexcooke.co`). The tool's **website** lives in its `hrrr/`
  subfolder, so it's reachable at **`https://research.alexcooke.co/hrrr/`**.
  Publishing = commit to `main` and push; there's no build step.
- **`empyreandance/hrrr-ingest`** — the **worker** (Python/Docker). The VPS
  clones this and runs it hourly. Nothing here is web-served.

So the order below is: (1) repos on GitHub, (2) the R2 bucket in the middle,
(3) point the website at the bucket + publish, (4) stand up the worker that fills
the bucket, (5) verify and watch. Budget ~1–2 hours the first time.

Accounts needed: **GitHub** + **Cloudflare** (both already set up) and a VPS host
— **Hetzner Cloud** (~€16/mo). Have a card ready for Hetzner and R2.

---

## Part 1 — Repos on GitHub

- [ ] **Worker repo** `empyreandance/hrrr-ingest` exists and contains `ingest/`,
      `deploy/`, `docs/`, `.env.example`. (Created during the guided deploy.)
- [ ] **Website folder** `hrrr/` exists in `empyreandance/research` on `main`
      (the static site: `index.html`, `src/`, `presets/`, …).
- [ ] Add a link to the new tool from your site's tools list
      (`maps-and-tools.html` / `index.html`) pointing at `/hrrr/`.

---

## Part 2 — Create the storage bucket (Cloudflare R2)

This is the shared layer both other parts talk to.

- [ ] **Create the bucket.** Cloudflare dashboard → **R2** → *Create bucket*
      (e.g. `hrrr-tool`), location near your users.
- [ ] **Make an S3 API token for the worker.** R2 → *Manage API Tokens* →
      *Create API token* → **Object Read & Write**, scoped to this bucket. Save
      the **Access Key ID**, **Secret Access Key**, and the **endpoint**
      (`https://<account_id>.r2.cloudflarestorage.com`). These go in the VPS
      `.env` in Part 4.
- [ ] **Turn on public read + CORS** so the website can fetch the data. Follow
      **"R2 public access + CORS"** in [runbook.md](runbook.md) — exact clicks,
      the CORS JSON, and a `curl` test. **Set `AllowedOrigins` to
      `https://research.alexcooke.co`** (scheme + host only — no `/hrrr/` path).
- [ ] **Write down the bucket's PUBLIC url** — `https://pub-<hash>.r2.dev` or a
      custom domain. (*Not* the `r2.cloudflarestorage.com` one; that's the private
      API endpoint.) You need it in Part 3.

---

## Part 3 — Point the website at the data + publish

The `research` site is already a live GitHub Pages site, so there's **nothing to
"enable"** — publishing is just a push to `main`.

- [ ] **Set the data URL.** Edit `hrrr/src/config.js` in the `research` repo:
      replace `REPLACE_WITH_YOUR_R2_PUBLIC_URL` with the public bucket URL from
      Part 2 (no trailing slash). Commit and push to `main`.
- [ ] **Watch it go live.** Within ~1 minute, open
      **`https://research.alexcooke.co/hrrr/`**. The map + UI load. It shows **no
      data yet** — expected until Part 4 fills the bucket. If you instead see a
      CORS or 401 error in the browser console (Right-click → Inspect → Console),
      re-check Part 2.

> Heads-up: your site runs through Jekyll. Plain `.html`/`.js`/`.css`/`.json`
> files (all of the tool's) are copied verbatim, so it "just works" — same as
> your `tennqlcs/` folder tool.

---

## Part 4 — Stand up the ingest worker (VPS)

The piece that runs hourly and fills the bucket. Full walkthrough (rent box →
install Docker → configure → smoke test → cron) is in [runbook.md](runbook.md)
under **"First-time setup"**. In short:

- [ ] Rent a **Hetzner CPX31** (4 vCPU / 8 GB) in Ashburn, Ubuntu 24.04.
- [ ] Install Docker, then
      `git clone https://github.com/empyreandance/hrrr-ingest.git`,
      `cd hrrr-ingest/ingest`, `cp ../.env.example .env`.
- [ ] Fill `.env` with the **R2 worker key** from Part 2 (`R2_ENDPOINT_URL`,
      `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`) and set
      `INGEST_WORKERS=3`, `PARCEL_TILES=8`.
- [ ] `docker compose build`, then `docker compose run --rm ingest --dry-run`,
      then one real run: `docker compose run --rm ingest`.
- [ ] Confirm the log ends with `"cycle complete"` and the bucket now has
      `manifest.json` + `cycles/<id>/`.
- [ ] Install the hourly cron: `../deploy/cron.sh install`.

---

## Part 5 — Verify end to end, then watch

- [ ] **Reload `https://research.alexcooke.co/hrrr/`.** Once the first cycle is in
      R2, the map populates. Build an ingredient or load a preset and confirm the
      count map renders and the forecast-hour slider works.
- [ ] **Let it run 48 hours** (spec §10.1.8) and confirm each hourly cycle
      completes and the site shows the newest valid time.

### What to watch during the soak

- **Extended cycles take longer.** The 00/06/12/18 UTC runs go out to 48
      forecast hours (others stop at 18). A 49-hour cycle is ~30–40 min of
      compute *and* NOAA publishes its later hours more slowly. If an extended
      cycle occasionally reports a missing late hour, the worker correctly keeps
      the previous complete cycle live (the manifest only flips on success) and
      the next hourly run recovers. If it happens *every* extended cycle, bump
      the cron's availability lag (`--lag-minutes`, default 65) a bit higher.
- **Disk stays flat.** The worker deletes each forecast hour's GRIB as it goes
      and clears its scratch dir on success, so `df -h` shouldn't creep upward
      cycle over cycle.
- **Failures alert you** if you set `PUSHOVER_TOKEN`/`PUSHOVER_USER_KEY` in
      `.env` — recommended so you don't have to babysit the logs.

---

## If something's wrong

| Symptom | Most likely cause | Where to look |
|---|---|---|
| Site loads, map blank, console shows CORS/401 | R2 public access or CORS not set, or `DATA_BASE_URL` points at the private S3 endpoint | Part 2 + [runbook.md](runbook.md) "R2 public access" |
| `/hrrr/` is 404 | `hrrr/` folder not pushed to `research` `main`, or no `index.html` in it | research repo on GitHub |
| Worker exits non-zero | bad R2 key, or NOAA hasn't published the cycle yet | `ingest/logs/ingest.log`; re-run with `--cycle` |
| Bad website change pushed live | revert it in the **research** repo | [runbook.md](runbook.md) "Roll back the website" |
