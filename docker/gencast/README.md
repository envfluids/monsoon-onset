# GenCast Container

Cloud Batch container for GenCast inference.

The entrypoint is `python -m src.main`. It:

1. Downloads GenCast weights from `gs://dm_graphcast/gencast/params/`.
2. Downloads normalization stats from `gs://dm_graphcast/gencast/stats/`.
3. Downloads the required ECMWF GRIB files into `AIFS/raw/ifs_ic/grib`.
4. Downloads the GenCast SST IC into `gencast/raw/sst_ic`.
5. Runs `gencast/utils/run_gencast.py`.
6. Uploads outputs to `gs://$GCS_BUCKET/$FORECAST_REGION/output/gencast/$DATE/`.

Required environment variables:

- `DATE`: forecast date in `YYYYMMDDTHH` format
- `GCS_BUCKET`: pipeline data bucket

Optional environment variables:

- `FORECAST_REGION`: defaults to `india`
- `GRAPHCAST_BUCKET`: defaults to `dm_graphcast`
