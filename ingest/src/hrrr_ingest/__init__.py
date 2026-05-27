"""HRRR Threshold Tool ingest worker.

Fetches HRRR ``wrfprsf`` GRIB2 files from NOAA's S3 bucket, subsets out the
hydrometeor mixing ratios, computes derived parameters and composite indices,
and publishes each cycle to Cloudflare R2 as Zarr. See
``docs/hrrr_threshold_tool_spec.md`` for the full specification.
"""

__version__ = "0.1.0"
