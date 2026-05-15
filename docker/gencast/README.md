# GenCast Container

Cloud Batch container for GenCast inference.

The entrypoint is `python -m src.main`. It:

1. Downloads GenCast weights from `gs://dm_graphcast/gencast/params/`.
2. Downloads normalization stats from `gs://dm_graphcast/gencast/stats/`.
3. Downloads the required ECMWF GRIB files into `AIFS/raw/ifs_ic/grib`.
4. Downloads the GenCast SST IC into `gencast/raw/sst_ic`.
5. Runs `gencast/utils/run_gencast.py`.
6. Uploads full-field forecasts to `gs://$GCS_COMMON_BUCKET/raw_forecast/gencast/$DATE/`.

Required environment variables:

- `DATE`: forecast date in `YYYYMMDDTHH` format
- `GCS_BUCKET`: pipeline data bucket, used as a fallback when `GCS_COMMON_BUCKET` is unset

Optional environment variables:

- `FORECAST_REGION`: defaults to `india`
- `GCS_COMMON_BUCKET`: common data bucket for ICs and full-field forecasts
- `GRAPHCAST_BUCKET`: defaults to `dm_graphcast`
