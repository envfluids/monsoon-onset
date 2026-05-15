# GenCast Container

Cloud TPU VM container for GenCast inference. The workflow launches GenCast as
a TPU queued resource on a v5p-32 slice, then runs this container on every TPU
VM host with `--privileged --net=host` so JAX can see the TPU devices.

The entrypoint is `python -m src.main`. It:

1. Downloads GenCast weights from `gs://dm_graphcast/gencast/params/`.
2. Downloads normalization stats from `gs://dm_graphcast/gencast/stats/`.
3. Downloads the required ECMWF GRIB files into `AIFS/raw/ifs_ic/grib`.
4. Downloads the GenCast SST IC into `gencast/raw/sst_ic`.
5. Runs `gencast/utils/run_gencast.py`.
6. Uploads full-field forecasts to `gs://$GCS_COMMON_BUCKET/full_field/gencast/$DATE/`.
7. Uploads per-region outputs and writes completion markers.

Required environment variables:

- `DATE`: forecast date in `YYYYMMDDTHH` format
- `FORECAST_REGIONS`: JSON list of regions to publish
- `GCS_COMMON_BUCKET`: common data bucket for ICs, weights, full-field forecasts, and markers
- `GCS_REGION_BUCKETS`: JSON map of region names to region buckets

Optional environment variables:

- `GRAPHCAST_BUCKET`: defaults to `dm_graphcast`
- `GENCAST_JAX_DISTRIBUTED`: set to `true` on TPU slices
- `GENCAST_EXPECTED_GLOBAL_DEVICES`: defaults to Terraform's v5p-32 expectation of `32`
- `GENCAST_EXPECTED_LOCAL_DEVICES`: defaults to Terraform's v5p host expectation of `4`
- `GENCAST_EXPECTED_PROCESS_COUNT`: defaults to Terraform's v5p-32 host count of `8`
- `GENCAST_ENSEMBLE_MEMBERS`: set to `32` by the workflow so the pmap axis matches the TPU devices
