# HRRR ingest worker

Fetches HRRR `wrfprsf` GRIB2 from NOAA's S3 bucket, subsets out the hydrometeor
mixing ratios, computes derived parameters and composite indices, and publishes
each cycle to Cloudflare R2 as Zarr. See
[`docs/hrrr_threshold_tool_spec.md`](../docs/hrrr_threshold_tool_spec.md) — this
package is Phase 1 (§10.1).

> **Status:** ingest backbone works end-to-end. `fetch` → `subset` → `compute`
> (native extraction + unit conversion + spec 4.2 derivations) → `publish` (Zarr
> with the spec 3.5 BloscCodec + atomic manifest lifecycle) are implemented and
> tested against real archived HRRR data. The catalog covers a verified core of
> spec 4.1 (69 native parameters); the long tail of 4.1 and **all composite
> indices (spec 4.3) are not yet implemented** (see `catalog.py` coverage note).
> `--dry-run` walks the control flow without network or R2 access.

## Layout

```
ingest/
├── pyproject.toml          uv project; deps pinned in uv.lock
├── Dockerfile              3-stage: wgrib2 source build · uv deps · slim runtime
├── docker-compose.yml      one-shot service, triggered by host cron
├── src/hrrr_ingest/
│   ├── orchestrator.py     top-level control flow + CLI entrypoint
│   ├── config.py           env-var configuration (spec 8.2)
│   ├── cycle.py            cycle math: which cycle, which forecast hours
│   ├── idx.py              .idx inventory parser (partial fetch / fixtures)
│   ├── fetch.py            NOAA S3 download (streaming + retries)
│   ├── subset.py           wgrib2 / cfgrib hydrometeor subset
│   ├── catalog.py          parameter metadata (core of spec 4.1)
│   ├── compute/            native extraction, unit conv, derivations (4.2)
│   │   ├── units.py        operational unit conversions
│   │   └── derivations.py  wind/shear/lapse/dewpoint-depression
│   ├── publish.py          Zarr (BloscCodec) writes + manifest lifecycle
│   ├── logging_setup.py    one-JSON-object-per-line logging
│   └── notify.py           Pushover failure notifications
└── tests/
```

## Local development (Mac)

`wgrib2` isn't on the Mac dev box, so subsetting falls back to cfgrib in-memory
filtering once implemented (spec 3.4). Dependencies are managed with
[uv](https://docs.astral.sh/uv/):

```bash
cd ingest
uv sync                       # create .venv from uv.lock
uv run hrrr-ingest --dry-run  # walk the control flow for the current cycle
uv run hrrr-ingest --dry-run --cycle 2026052400   # a specific cycle
uv run pytest
```

A dry run needs no R2 credentials. A real run reads config from `.env` at the
repo root (copy from [`.env.example`](../.env.example)).

## Production (Hetzner CX32, Docker)

```bash
docker compose build
docker compose run --rm ingest                 # ingest the current cycle
docker compose run --rm ingest --cycle 2026052400   # rerun a cycle
```

The host cron (`deploy/cron.sh`, added later) triggers `docker compose run --rm
ingest` at HRRR init + 65 minutes (spec 3.2). The image builds `wgrib2` from
source so production gets the lower-memory disk-subset path (spec 3.3/3.4).
