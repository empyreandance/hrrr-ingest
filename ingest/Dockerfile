# syntax=docker/dockerfile:1
#
# HRRR ingest worker image.
#
# Stages: wgrib2 (compile) -> deps (uv) -> runtime.
#
# wgrib2 gives the lower-memory disk-subset path (spec 3.4), but it is OPTIONAL:
# the worker auto-falls back to cfgrib if wgrib2 is absent (proven end-to-end).
# The compile below is NON-FATAL — if it fails on a given box, the build still
# succeeds with an empty /out and the runtime uses cfgrib (a warning is logged).
#
# NOTE: targets linux/amd64; not built on the Mac dev box (no Docker locally).

# ---------------------------------------------------------------------------
# Stage 1: build wgrib2 from source -> /out/wgrib2 (or empty /out on failure)
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS wgrib2

# The NOAA "latest" tarball is not version-stamped in its URL. Pin a sha256 for
# a reproducible build once a version is chosen; override the URL via --build-arg.
ARG WGRIB2_URL="https://www.ftp.cpc.ncep.noaa.gov/wd51we/wgrib2/wgrib2.tgz"
ARG WGRIB2_SHA256=""

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gfortran wget ca-certificates file zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN wget -q "${WGRIB2_URL}" -O wgrib2.tgz \
    && if [ -n "${WGRIB2_SHA256}" ]; then \
         echo "${WGRIB2_SHA256}  wgrib2.tgz" | sha256sum -c -; \
       fi \
    && tar xzf wgrib2.tgz

# The tarball extracts to grib2/. The bundled makefile builds its own jasper/
# libpng/zlib (needed to decode HRRR's JPEG2000-packed fields); heavy optional
# integrations (NetCDF, IPOLATES, spectral) stay off for a fast, self-contained build.
# Non-fatal: `;` (not `&&`) + an explicit check, so a compile failure can't break
# the image — /out just ends up empty and the runtime uses the cfgrib fallback.
WORKDIR /build/grib2
RUN mkdir -p /out && touch /out/.keep ; \
    CC=gcc FC=gfortran make \
        USE_NETCDF3=0 USE_NETCDF4=0 USE_REGEX=1 USE_TIGGE=1 USE_MYSQL=0 \
        USE_IPOLATES=0 USE_SPECTRAL=0 USE_UDF=0 USE_OPENMP=0 USE_PROJ4=0 ; \
    if [ -x wgrib2/wgrib2 ] && wgrib2/wgrib2 -version ; then \
        cp wgrib2/wgrib2 /out/wgrib2 ; echo "wgrib2 built OK" ; \
    else \
        echo "WARNING: wgrib2 build failed/unusable — image will use the cfgrib fallback" ; \
    fi

# ---------------------------------------------------------------------------
# Stage 2: resolve the locked Python environment
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS deps

# Pinned uv, copied from the official image (matches the dev box: uv 0.11.12).
COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install dependencies first (cached layer), then the project itself, so a
# source-only change does not re-resolve the whole environment.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-install-project --no-dev

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Runtime libs: eccodes C library for cfgrib; libgfortran/libgomp in case the
# wgrib2 binary links them dynamically. tzdata so UTC cycle math is unambiguous.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libeccodes0 \
        libgfortran5 \
        libgomp1 \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy the wgrib2 binary if the chosen stage built one (empty otherwise).
COPY --from=wgrib2 /out/ /usr/local/bin/
COPY --from=deps /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /app

# One-shot run: compute the current cycle from system time, ingest it, exit.
# The host cron (deploy/cron.sh) triggers this at HRRR init + 65 min.
ENTRYPOINT ["python", "-m", "hrrr_ingest.orchestrator"]
