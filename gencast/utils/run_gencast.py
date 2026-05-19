import argparse
import concurrent.futures
import datetime
import json
import logging
import os
import shutil
import tempfile
import time
import traceback
import zlib
from pathlib import Path

import haiku as hk
import jax
import jax.numpy as jnp
import numpy as np
import pandas as pd
import xarray as xr
import zarr
from jax.experimental import multihost_utils
from graphcast import (
    checkpoint,
    data_utils,
    gencast,
    nan_cleaning,
    normalization,
    rollout,
    xarray_jax,
)
from preprocess_ic import get_ic

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

BASE = Path(__file__).parent.parent

REPO_ROOT = BASE.parent

# JAX_CACHE_DIR = REPO_ROOT.parent / "jax_cache"
# JAX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
# jax.config.update("jax_compilation_cache_dir", str(JAX_CACHE_DIR))
# jax.config.update("jax_persistent_cache_min_entry_size_bytes", -1)
# jax.config.update("jax_persistent_cache_min_compile_time_secs", 0)
# jax.config.update("jax_persistent_cache_enable_xla_caches", "xla_gpu_per_fusion_autotune_cache_dir")

DEBUG_FLAG = os.getenv("GENCAST_DEBUG", "false")
if DEBUG_FLAG.strip().lower() == "1":
    logging.info(
        "GENCAST_DEBUG is set to a truthy value (%s). Enabling DEBUG mode.", DEBUG_FLAG
    )
    DEBUG = True
else:
    DEBUG = False

if DEBUG:
    N_MEMBERS = 4
    N_DAYS = 2
    DEBUG_SUFFIX = "__DEBUG"
else:
    N_MEMBERS = 24
    N_DAYS = 50

N_STEPS = N_DAYS * 2  # 2 steps per day (12h interval)

SAVE_LEVELS = [200, 500, 700, 850]
SAVE_DICT = {
    "geopotential": SAVE_LEVELS,
    "u_component_of_wind": SAVE_LEVELS,
    "v_component_of_wind": SAVE_LEVELS,
    "2m_temperature": [],
    "sea_surface_temperature": [],
    "total_precipitation_12hr": [],
    "mean_sea_level_pressure": [],
}

RENAME_DICT = {
    "geopotential": "z",
    "u_component_of_wind": "u",
    "v_component_of_wind": "v",
    "2m_temperature": "2t",
    "sea_surface_temperature": "sst",
    "total_precipitation_12hr": "tp",
    "mean_sea_level_pressure": "msl",
}

MODEL_PATH = BASE / "weights" / "GenCast 0p25deg Operational <2022.npz"
STATS_DIR = BASE / "data"
FCST_DIR = BASE / "raw" / "output"
MAX_EXCEPTION_MESSAGE_CHARS = int(
    os.getenv("GENCAST_EXCEPTION_MESSAGE_CHARS", "4000")
)


def _truncate_text(text, limit=MAX_EXCEPTION_MESSAGE_CHARS):
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}... <truncated {omitted} chars>"


def _log_exception_summary(exc):
    message = _truncate_text(str(exc).replace("\n", "\\n"))
    logging.error("GenCast failed: %s: %s", exc.__class__.__name__, message)
    logging.error("GenCast traceback summary:")
    frames = traceback.extract_tb(exc.__traceback__)
    for frame in frames[-12:]:
        logging.error(
            '  File "%s", line %s, in %s',
            frame.filename,
            frame.lineno,
            frame.name,
        )
        if frame.line:
            logging.error("    %s", frame.line)


def open_nc_file(path):
    try:
        return xr.open_dataset(path, engine="h5netcdf").compute()
    except Exception as e:
        logging.error(f"Failed to open {path} with h5netcdf: {e}")
        logging.info(f"Attempting to open {path} with netcdf4 engine instead.")
        return xr.open_dataset(path, engine="netcdf4").compute()


class _TimedOperation:
    def __init__(self, operation):
        self._operation = operation
        self._start = None

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, traceback):
        elapsed = time.perf_counter() - self._start
        if exc_type is None:
            logging.info("Completed %s in %.2fs.", self._operation, elapsed)
        else:
            logging.info("Failed %s after %.2fs.", self._operation, elapsed)


def _forecast_output_path(date_f):
    suffix = DEBUG_SUFFIX if DEBUG else ""
    final_path = FCST_DIR / f"init_{date_f}{suffix}.zarr"
    return final_path


def _partial_temp_prefix(outfile):
    return f".{outfile.name}.partial."


def _remove_stale_partial_temp_dirs(outfile):
    prefix = _partial_temp_prefix(outfile)
    for path in FCST_DIR.glob(f"{prefix}*"):
        logging.info(f"Removing stale partial output directory {path}.")
        _remove_store(path)


def _remove_store(path):
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _select_pmap_devices(num_samples):
    devices = (
        jax.devices() if _env_bool("GENCAST_JAX_DISTRIBUTED") else jax.local_devices()
    )
    for count in range(min(len(devices), num_samples), 0, -1):
        if num_samples % count == 0:
            selected = devices[:count]
            break
    else:
        selected = devices[:1]

    if len(selected) != len(devices):
        device_scope = "global" if _env_bool("GENCAST_JAX_DISTRIBUTED") else "local"
        logging.info(
            "Using %d of %d %s JAX devices so %d samples divide evenly.",
            len(selected),
            len(devices),
            device_scope,
            num_samples,
        )
    return selected


def _array_crc32(array):
    data = np.asarray(array)
    checksum = zlib.crc32(f"{data.shape}|{data.dtype}".encode("utf-8"))
    checksum = zlib.crc32(np.ascontiguousarray(data).view(np.uint8), checksum)
    return np.uint32(checksum)


def _numeric_coord_names(dataset):
    return [
        name
        for name, coord in dataset.coords.items()
        if np.issubdtype(np.asarray(coord.data).dtype, np.number)
    ]


def _dataset_host_arrays(dataset):
    for name, variable in dataset.data_vars.items():
        yield f"data_var:{name}", variable.data
    for name in _numeric_coord_names(dataset):
        yield f"coord:{name}", dataset.coords[name].data


def _log_multihost_dataset_differences(dataset, label):
    if jax.process_count() == 1:
        return []

    mismatches = []
    for name, array in _dataset_host_arrays(dataset):
        digest = np.asarray(_array_crc32(array), dtype=np.uint32)
        all_digests = np.asarray(multihost_utils.process_allgather(digest)).reshape(
            -1
        )
        if len(set(all_digests.tolist())) > 1:
            mismatches.append(f"{name}={all_digests.tolist()}")

    if mismatches:
        logging.warning(
            "%s differs across JAX processes: %s",
            label,
            "; ".join(mismatches),
        )
    else:
        logging.info(
            "%s data variables and numeric coordinates are identical across "
            "%d JAX processes.",
            label,
            jax.process_count(),
        )
    return mismatches


def _zarr_chunks_for_var(dims, sizes, sample_chunk, time_chunk):
    chunks = []
    for dim in dims:
        if dim == "sample":
            chunks.append(sample_chunk)
        elif dim == "time":
            chunks.append(time_chunk)
        elif dim == "batch":
            chunks.append(1)
        else:
            chunks.append(sizes[dim])
    return tuple(chunks)


def _validate_save_config(targets_template):
    missing_vars = sorted(set(SAVE_DICT) - set(targets_template.data_vars))
    if missing_vars:
        raise ValueError(f"SAVE_DICT includes unknown target variables: {missing_vars}")

    missing_renames = sorted(set(SAVE_DICT) - set(RENAME_DICT))
    if missing_renames:
        raise ValueError(
            f"RENAME_DICT is missing saved target variables: {missing_renames}"
        )

    renamed_vars = [RENAME_DICT[name] for name in SAVE_DICT]
    duplicate_renames = sorted(
        {name for name in renamed_vars if renamed_vars.count(name) > 1}
    )
    if duplicate_renames:
        raise ValueError(
            f"RENAME_DICT maps multiple saved variables to: {duplicate_renames}"
        )

    allowed_levels = set(SAVE_LEVELS)
    requested_level_lists = []
    available_levels = set(np.asarray(targets_template.level.values).tolist())
    for name, levels in SAVE_DICT.items():
        levels = tuple(levels)
        var = targets_template[name]
        if "level" in var.dims:
            if not levels:
                raise ValueError(f"SAVE_DICT['{name}'] must include pressure levels.")

            extra_levels = sorted(set(levels) - allowed_levels)
            if extra_levels:
                raise ValueError(
                    f"SAVE_DICT['{name}'] includes levels outside SAVE_LEVELS: "
                    f"{extra_levels}"
                )

            missing_levels = sorted(set(levels) - available_levels)
            if missing_levels:
                raise ValueError(
                    f"SAVE_DICT['{name}'] includes levels not in model output: "
                    f"{missing_levels}"
                )

            requested_level_lists.append(levels)
        elif levels:
            raise ValueError(
                f"SAVE_DICT['{name}'] specifies pressure levels, but the variable "
                "has no level dimension."
            )

    if len(set(requested_level_lists)) > 1:
        raise ValueError(
            "All saved pressure-level variables must use the same level list so "
            "they can share one output 'level' coordinate."
        )


def _targets_template_for_save(targets_template):
    _validate_save_config(targets_template)

    data_vars = {}
    for name, levels in SAVE_DICT.items():
        var = targets_template[name]
        if "level" in var.dims:
            var = var.sel(level=list(levels))
        data_vars[RENAME_DICT[name]] = var

    return xr.Dataset(data_vars=data_vars, attrs=targets_template.attrs)


def _initialize_partial_zarr(
    partial_path,
    targets_template,
    num_samples,
    sample_chunk,
    time_chunk,
):
    root = zarr.open_group(str(partial_path), mode="w", zarr_format=2)
    root.attrs.update(targets_template.attrs)
    sizes = dict(targets_template.sizes)
    sizes["sample"] = num_samples

    coords = dict(targets_template.coords)
    coords["sample"] = xr.DataArray(np.arange(num_samples), dims=("sample",))
    for name, coord in coords.items():
        values = np.asarray(coord.values)
        chunks = values.shape if values.shape else None
        array = root.create_array(
            name,
            shape=values.shape,
            chunks=chunks,
            dtype=values.dtype,
            fill_value=None,
        )
        array[...] = values
        array.attrs.update(coord.attrs)
        array.attrs["_ARRAY_DIMENSIONS"] = list(coord.dims)

    for name, var in targets_template.data_vars.items():
        dims = ("sample",) + var.dims
        shape = tuple(sizes[dim] for dim in dims)
        chunks = _zarr_chunks_for_var(dims, sizes, sample_chunk, time_chunk)
        fill_value = np.nan if np.issubdtype(var.dtype, np.floating) else None
        array = root.create_array(
            name,
            shape=shape,
            chunks=chunks,
            dtype=var.dtype,
            fill_value=fill_value,
        )
        array.attrs.update(var.attrs)
        array.attrs["_ARRAY_DIMENSIONS"] = list(dims)


def _slice_from_coordinate_values(values, full_values, dim):
    values = np.asarray(values)
    indexer = pd.Index(full_values).get_indexer(values)
    if (indexer < 0).any():
        raise ValueError(
            f"Chunk contains {dim} values that are not in the output template."
        )
    expected = np.arange(indexer[0], indexer[0] + len(indexer))
    if not np.array_equal(indexer, expected):
        raise ValueError(
            f"Chunk {dim} values are not contiguous in the output template."
        )
    return slice(int(indexer[0]), int(indexer[-1]) + 1)


def _write_prediction_chunk(
    partial_path,
    chunk,
    target_times,
    sample_values,
    target_levels,
):
    region = {
        "sample": _slice_from_coordinate_values(
            chunk.sample.values, sample_values, "sample"
        ),
        "time": _slice_from_coordinate_values(chunk.time.values, target_times, "time"),
    }
    if target_levels is not None:
        region["level"] = slice(None)

    root = zarr.open_group(str(partial_path), mode="r+", zarr_format=2)
    for name, levels in SAVE_DICT.items():
        output_name = RENAME_DICT[name]
        var = chunk[name]
        if "level" in var.dims:
            var = var.sel(level=list(levels))
            region["level"] = _slice_from_coordinate_values(
                var.level.values,
                target_levels,
                "level",
            )

        key = tuple(region[dim] if dim in region else slice(None) for dim in var.dims)
        root[output_name][key] = np.asarray(var.data)
    return region


def _timed_write_prediction_chunk(*args):
    started = time.perf_counter()
    region = _write_prediction_chunk(*args)
    return region, time.perf_counter() - started


class _AsyncPredictionWriter:
    def __init__(
        self,
        partial_path,
        target_times,
        sample_values,
        target_levels,
        max_pending=2,
    ):
        self._partial_path = partial_path
        self._target_times = target_times
        self._sample_values = sample_values
        self._target_levels = target_levels
        self._max_pending = max_pending
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._pending = []

    def submit(self, chunk):
        self._drain_completed()
        if len(self._pending) >= self._max_pending:
            started = time.perf_counter()
            self._finish_next()
            logging.info(
                "Async writer backpressure waited %.2fs for %s.",
                time.perf_counter() - started,
                self._partial_path,
            )

        future = self._executor.submit(
            _timed_write_prediction_chunk,
            self._partial_path,
            chunk,
            self._target_times,
            self._sample_values,
            self._target_levels,
        )
        self._pending.append(future)

    def wait(self, operation="async writer drain"):
        started = time.perf_counter()
        pending_count = len(self._pending)
        while self._pending:
            self._finish_next()
        logging.info(
            "Completed %s for %s: %d pending writes drained in %.2fs.",
            operation,
            self._partial_path,
            pending_count,
            time.perf_counter() - started,
        )

    def close(self):
        self._executor.shutdown(wait=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self.wait("final async writer drain")
        finally:
            self.close()

    def _drain_completed(self):
        remaining = []
        for future in self._pending:
            if future.done():
                region, write_seconds = future.result()
                logging.info(
                    "Finished writing chunk to %s in %.2fs: sample=%s, time=%s.",
                    self._partial_path,
                    write_seconds,
                    region["sample"],
                    region["time"],
                )
            else:
                remaining.append(future)
        self._pending = remaining

    def _finish_next(self):
        future = self._pending.pop(0)
        region, write_seconds = future.result()
        logging.info(
            "Finished writing chunk to %s in %.2fs: sample=%s, time=%s.",
            self._partial_path,
            write_seconds,
            region["sample"],
            region["time"],
        )


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def initialize_jax_runtime() -> dict[str, object]:
    batch_task_index = os.getenv("BATCH_TASK_INDEX")
    batch_task_count = os.getenv("BATCH_TASK_COUNT")
    batch_job_id = os.getenv("BATCH_JOB_ID")

    if batch_task_index is not None and batch_job_id is not None:
        logging.info(
            "Detected Cloud Batch environment: task_index=%s, task_count=%s, job_id=%s",
            batch_task_index,
            batch_task_count,
            batch_job_id,
        )
        # Cloud Batch internal DNS: <job-id>-0-<task-index>
        coordinator_address = f"{batch_job_id}-0-0:1234"
        jax.distributed.initialize(
            coordinator_address=coordinator_address,
            num_processes=int(batch_task_count or 1),
            process_id=int(batch_task_index),
        )
    elif _env_bool("GENCAST_JAX_DISTRIBUTED"):
        logging.info("Initializing JAX distributed runtime (auto-detect).")
        jax.distributed.initialize()

    local_devices = jax.local_devices()
    global_devices = jax.devices()
    runtime = {
        "jax_version": jax.__version__,
        "backend": jax.default_backend(),
        "process_index": jax.process_index(),
        "process_count": jax.process_count(),
        "local_device_count": jax.local_device_count(),
        "global_device_count": jax.device_count(),
        "local_devices": [str(device) for device in local_devices],
        "global_devices": [str(device) for device in global_devices],
    }
    logging.info("GenCast JAX runtime: %s", json.dumps(runtime, sort_keys=True))

    expected_global = os.getenv("GENCAST_EXPECTED_GLOBAL_DEVICES")
    if expected_global is not None and jax.device_count() != int(expected_global):
        raise RuntimeError(
            f"Expected {expected_global} global JAX devices, found {jax.device_count()}"
        )

    expected_local = os.getenv("GENCAST_EXPECTED_LOCAL_DEVICES")
    if expected_local is not None and jax.local_device_count() != int(expected_local):
        raise RuntimeError(
            f"Expected {expected_local} local JAX devices, found {jax.local_device_count()}"
        )

    expected_processes = os.getenv("GENCAST_EXPECTED_PROCESS_COUNT")
    if expected_processes is not None and jax.process_count() != int(
        expected_processes
    ):
        raise RuntimeError(
            f"Expected {expected_processes} JAX processes, found {jax.process_count()}"
        )

    return runtime


def write_run_metadata(date_f: str, runtime: dict[str, object]) -> None:
    FCST_DIR.mkdir(parents=True, exist_ok=True)
    metadata_path = FCST_DIR / f"run_metadata_{date_f}.json"
    with metadata_path.open("w") as f:
        json.dump(runtime, f, indent=2, sort_keys=True)


def get_model():
    with open(MODEL_PATH, "rb") as f:
        ckpt = checkpoint.load(f, gencast.CheckPoint)
        if not _env_bool("GENCAST_JAX_DISTRIBUTED"):
            logging.info(
                "Running in non-distributed mode, modifying model config for GPU execution."
            )
            denoiser_architecture_config = ckpt.denoiser_architecture_config
            denoiser_architecture_config.sparse_transformer_config.attention_type = (
                "triblockdiag_mha"
            )
            denoiser_architecture_config.sparse_transformer_config.mask_type = "full"
    params = ckpt.params
    state = {}

    task_config = ckpt.task_config
    sampler_config = ckpt.sampler_config
    noise_config = ckpt.noise_config
    noise_encoder_config = ckpt.noise_encoder_config
    denoiser_architecture_config = ckpt.denoiser_architecture_config
    print("Model description:\n", ckpt.description, "\n")
    print("Model license:\n", ckpt.license, "\n")

    diffs_stddev_by_level = open_nc_file(STATS_DIR / "diffs_stddev_by_level.nc")
    mean_by_level = open_nc_file(STATS_DIR / "mean_by_level.nc")
    stddev_by_level = open_nc_file(STATS_DIR / "stddev_by_level.nc")
    min_by_level = open_nc_file(STATS_DIR / "min_by_level.nc")

    def construct_wrapped_gencast():
        """Constructs and wraps the GenCast Predictor."""
        predictor = gencast.GenCast(
            sampler_config=sampler_config,
            task_config=task_config,
            denoiser_architecture_config=denoiser_architecture_config,
            noise_config=noise_config,
            noise_encoder_config=noise_encoder_config,
        )

        predictor = normalization.InputsAndResiduals(
            predictor,
            diffs_stddev_by_level=diffs_stddev_by_level,
            mean_by_level=mean_by_level,
            stddev_by_level=stddev_by_level,
        )

        predictor = nan_cleaning.NaNCleaner(
            predictor=predictor,
            reintroduce_nans=True,
            fill_value=min_by_level,
            var_to_clean="sea_surface_temperature",
        )

        return predictor

    @hk.transform_with_state
    def run_forward(inputs, targets_template, forcings):
        predictor = construct_wrapped_gencast()
        return predictor(inputs, targets_template=targets_template, forcings=forcings)

    run_forward_jitted = jax.jit(
        lambda rng, i, t, f: run_forward.apply(params, state, rng, i, t, f)[0]
    )
    # We also produce a pmapped version for running in parallel.
    run_forward_pmap = xarray_jax.pmap(run_forward_jitted, dim="sample")

    return run_forward_pmap, task_config


def get_forcings_targets(task_config, date, lt, lat, lon, level):
    dt = 12
    times = [pd.Timedelta(dt * i + dt, "h") for i in range(lt)]
    datetimes = [date + t for t in times]

    forcings = xr.Dataset(
        coords={
            "datetime": datetimes,
            "time": times,
            "lat": lat,
            "lon": lon,
        }
    )

    data_utils.add_derived_vars(forcings)
    forcings = forcings.drop_vars(["datetime", "year_progress", "day_progress"])
    forcings = forcings.drop_vars("lat")
    forcings = forcings.expand_dims("batch", axis=0)

    forcings = forcings.transpose("batch", "time", "lon")

    target_vars = list(task_config.target_variables)
    target_vars_sfc = [
        "2m_temperature",
        "mean_sea_level_pressure",
        "10m_v_component_of_wind",
        "10m_u_component_of_wind",
        "total_precipitation_12hr",
        "sea_surface_temperature",
    ]

    data_vars = {}
    for var in target_vars:
        if var in target_vars_sfc:
            # Surface variables (no level dimension)
            data_vars[var] = (
                ("time", "lat", "lon"),
                np.zeros((len(times), lat.size, lon.size), dtype=np.float32),
            )
        else:
            # Variables with level dimension
            data_vars[var] = (
                ("time", "level", "lat", "lon"),
                np.zeros(
                    (len(times), level.size, lat.size, lon.size), dtype=np.float32
                ),
            )

    targets_template = xr.Dataset(
        data_vars=data_vars,
        coords={"time": times, "lat": lat, "lon": lon, "level": level},
    )

    targets_template = targets_template.expand_dims("batch")
    return forcings, targets_template


def import_ic(task_config, date):
    ic = get_ic(date)
    if set(task_config.forcing_variables) & data_utils._DERIVED_VARS:
        data_utils.add_derived_vars(ic)
    if set(task_config.forcing_variables) & {data_utils.TISR}:
        data_utils.add_tisr_var(ic)

    ic["time"] = [-pd.Timedelta("12h"), pd.Timedelta(0)]

    ic = ic.drop_vars(["datetime", "year_progress", "day_progress"])

    return ic


def run_model(date_f, runtime):
    run_started = time.perf_counter()
    run_status = "failed"
    try:
        date = datetime.datetime.strptime(date_f, "%Y%m%dT%H")
        if DEBUG:
            logging.info(
                "Running in DEBUG mode with reduced ensemble members and days."
            )
        outfile = _forecast_output_path(date_f)
        if outfile.exists():
            logging.info(f"Output file {outfile} already exists. Skipping run.")
            run_status = "skipped"
            write_run_metadata(date_f, runtime)
            return

        FCST_DIR.mkdir(parents=True, exist_ok=True)
        _remove_stale_partial_temp_dirs(outfile)
        with _TimedOperation("get_model"):
            run_forward_pmap, task_config = get_model()
        with _TimedOperation("initial condition import"):
            ic = import_ic(task_config, date)
        _log_multihost_dataset_differences(ic, "initial conditions")
        lat = ic.lat.values
        lon = ic.lon.values
        level = ic.level.values
        with _TimedOperation("forcings and target template construction"):
            forcings_ds, targets_template_ds = get_forcings_targets(
                task_config, date, N_STEPS, lat, lon, level
            )
        _log_multihost_dataset_differences(forcings_ds, "forcings")
        _log_multihost_dataset_differences(
            targets_template_ds,
            "finite rollout target template",
        )
        with _TimedOperation("saved output template construction"):
            saved_targets_template_ds = _targets_template_for_save(targets_template_ds)
        num_ensemble_members = int(
            os.getenv("GENCAST_ENSEMBLE_MEMBERS", str(N_MEMBERS))
        )
        rng = jax.random.PRNGKey(0)

        rngs = np.stack(
            [jax.random.fold_in(rng, i) for i in range(num_ensemble_members)], axis=0
        )

        num_steps_per_chunk = 1
        total_steps = N_STEPS
        sample_values = np.arange(num_ensemble_members)
        pmap_devices = _select_pmap_devices(num_ensemble_members)
        total_chunks = total_steps * (num_ensemble_members // len(pmap_devices))

        with tempfile.TemporaryDirectory(
            prefix=_partial_temp_prefix(outfile),
            dir=FCST_DIR,
        ) as partial_temp_dir:
            partial_outfile = Path(partial_temp_dir) / outfile.name
            with _TimedOperation("partial Zarr initialization"):
                _initialize_partial_zarr(
                    partial_path=partial_outfile,
                    targets_template=saved_targets_template_ds,
                    num_samples=num_ensemble_members,
                    sample_chunk=len(pmap_devices),
                    time_chunk=num_steps_per_chunk,
                )

            target_levels = (
                saved_targets_template_ds.level.values
                if "level" in saved_targets_template_ds.coords
                else None
            )
            chunks_queued = 0
            with _AsyncPredictionWriter(
                partial_path=partial_outfile,
                target_times=targets_template_ds.time.values,
                sample_values=sample_values,
                target_levels=target_levels,
            ) as writer:
                chunk_wait_started = time.perf_counter()
                for chunk in rollout.chunked_prediction_generator_multiple_runs(
                    # Use pmapped version to parallelise across devices.
                    predictor_fn=run_forward_pmap,
                    rngs=rngs,
                    inputs=ic,
                    targets_template=targets_template_ds,
                    forcings=forcings_ds,
                    num_steps_per_chunk=num_steps_per_chunk,
                    num_samples=num_ensemble_members,
                    pmap_devices=pmap_devices,
                ):
                    chunk_generation_seconds = time.perf_counter() - chunk_wait_started
                    writer.submit(chunk)
                    chunks_queued += 1
                    logging.info(
                        "Queued chunk %d/%d for async write to %s after %.2fs "
                        "waiting for rollout chunk.",
                        chunks_queued,
                        total_chunks,
                        partial_outfile,
                        chunk_generation_seconds,
                    )
                    chunk_wait_started = time.perf_counter()

            with _TimedOperation("Zarr metadata consolidation"):
                zarr.consolidate_metadata(str(partial_outfile))
            with _TimedOperation("final output rename"):
                partial_outfile.rename(outfile)
        logging.info(f"Forecast written to {outfile}.")
        run_status = "completed"
    finally:
        logging.info(
            "run_model(%s) %s in %.2fs.",
            date_f,
            run_status,
            time.perf_counter() - run_started,
        )
        write_run_metadata(date_f, runtime)


def main():
    parser = argparse.ArgumentParser(description="Run GenCast for a given date.")
    parser.add_argument(
        "--date",
        type=str,
        help="Date to run the model for (YYYYMMDDTHH format)",
        required=True,
    )
    args = parser.parse_args()
    date_f = args.date

    try:
        runtime = initialize_jax_runtime()
        run_model(date_f, runtime)
    except Exception as exc:
        _log_exception_summary(exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
