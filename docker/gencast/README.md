# GenCast Container

Cloud TPU VM container for GenCast inference. The workflow launches GenCast as
a TPU queued resource on a v5p-64 slice, then runs this container on every TPU
VM host with `--privileged --net=host` so JAX can see the TPU devices.

The entrypoint is `python -m src.main`. It:

1. Downloads GenCast weights from `gs://dm_graphcast/gencast/params/`.
2. Downloads normalization stats from `gs://dm_graphcast/gencast/stats/`.
3. Downloads the required ECMWF GRIB files into `AIFS/raw/ifs_ic/grib`.
4. Downloads the GenCast SST IC into `gencast/raw/sst_ic`.
5. Runs `gencast/utils/run_gencast.py`.
6. Streams full-field forecasts through Cloud Storage FUSE to `gs://$GCS_COMMON_BUCKET/full_field/gencast/$DATE/` while inference is running.
7. Uploads per-region outputs and writes completion markers.

Required environment variables:

- `DATE`: forecast date in `YYYYMMDDTHH` format
- `FORECAST_REGIONS`: JSON list of regions to publish
- `GCS_COMMON_BUCKET`: common data bucket for ICs, weights, full-field forecasts, and markers
- `GCS_REGION_BUCKETS`: JSON map of region names to region buckets

Optional environment variables:

- `GRAPHCAST_BUCKET`: defaults to `dm_graphcast`
- `GENCAST_JAX_DISTRIBUTED`: set to `true` on TPU slices
- `GENCAST_EXPECTED_GLOBAL_DEVICES`: defaults to Terraform's v5p-64 expectation of `32`
- `GENCAST_EXPECTED_LOCAL_DEVICES`: defaults to Terraform's v5p host expectation of `4`
- `GENCAST_EXPECTED_PROCESS_COUNT`: defaults to Terraform's v5p-64 host count of `8`
- `GENCAST_ENSEMBLE_MEMBERS`: set to `32` by the workflow so the pmap axis matches the TPU devices
- `GENCAST_ZARR_MIRROR_TARGET`: optional filesystem or Cloud Storage FUSE target for streaming full-field Zarr components during inference; the TPU dispatcher sets this to `/mnt/disks/common/full_field/gencast/$DATE/init_$DATE.zarr`
- `GENCAST_GCSFUSE_BUCKET`: bucket to mount inside the GenCast container when the mirror target is under `GENCAST_GCSFUSE_MOUNT`; defaults to `GCS_COMMON_BUCKET` in the wrapper
- `GENCAST_GCSFUSE_MOUNT`: container-side Cloud Storage FUSE mount point; defaults to `/mnt/disks/common`
- `GENCAST_GCSFUSE_PROFILE`: Cloud Storage FUSE profile; defaults to `aiml-checkpointing`
- `GENCAST_ZARR_MIRROR_WORKERS`: number of worker threads process 0 uses to stream changed full-field Zarr components through the mirror during inference; defaults to `16`
