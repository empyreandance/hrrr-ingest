# HRRR Threshold Tool — ingest worker

This repo is the **data engine** for the HRRR Threshold Tool: a one-shot Python
worker that, each hour, downloads the latest HRRR model cycle, computes the
parameter/composite suite, and writes it to a Cloudflare R2 bucket as Zarr. A
cron job on a small VPS runs it; nothing stays running between cycles. See
[`ingest/`](ingest/) for the code.

The **website** that reads this data lives separately, in the `research` repo
under [`hrrr/`](https://github.com/empyreandance/research/tree/main/hrrr), served
at **https://research.alexcooke.co/hrrr/**.

## Docs

- **[docs/deploy-checklist.md](docs/deploy-checklist.md)** — going live:
  start-to-finish, in-order setup (GitHub → R2 → website → VPS).
- **[docs/runbook.md](docs/runbook.md)** — day-to-day operations, recovery,
  host migration, and the R2 public-access + CORS setup.
- **[docs/mac-studio-migration.md](docs/mac-studio-migration.md)** — move HRRR +
  QLCS compute onto a Mac Studio and retire the paid VPS (drops the cloud bill
  to ~$0).
- **[docs/hrrr_threshold_tool_spec.md](docs/hrrr_threshold_tool_spec.md)** — the
  full project specification.

## Quick start (local)

```bash
cd ingest
uv sync
uv run pytest                 # 67 tests
uv run hrrr-ingest --dry-run  # walk the control flow without network/R2
```

Config is all environment variables — copy [`.env.example`](.env.example) to
`.env` (gitignored) and fill in. Nothing secret lives in this repo.
