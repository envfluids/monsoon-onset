import argparse
import concurrent.futures
import datetime
import json
import logging
import os
import shutil
import subprocess
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
FCST_DIR = Path(os.getenv("GENCAST_OUTPUT_DIR", BASE / "raw" / "output"))
METADATA_DIR = Path(os.getenv("GENCAST_METADATA_DIR", BASE / "raw" / "output"))
SST_VAR = "sea_surface_temperature"
LAND_SEA_MASK_VAR = "land_sea_mask"
MAX_EXCEPTION_MESSAGE_CHARS = int(os.getenv("GENCAST_EXCEPTION_MESSAGE_CHARS", "4000"))


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


def _runtime_writes_outputs(runtime):
    return int(runtime.get("process_index", 0)) == 0


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


class FilesystemZarrMirror:
    def __init__(self, target_root, max_workers):
        self.target_root = Path(target_root)
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="filesystem-zarr-mirror",
        )
        self.queued_signatures = {}
        self.futures = []

    def enqueue_changed(self, source_root):
        source_root = Path(source_root)
        for local_file in source_root.rglob("*"):
            if not local_file.is_file():
                continue
            relative = local_file.relative_to(source_root)
            try:
                stat = local_file.stat()
            except FileNotFoundError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            if self.queued_signatures.get(relative) == signature:
                continue
            self.queued_signatures[relative] = signature
            self.futures.append(
                self.executor.submit(self._copy_file, local_file, relative)
            )

    def wait(self):
        errors = []
        try:
            for future in concurrent.futures.as_completed(self.futures):
                try:
                    future.result()
                except BaseException as exc:
                    errors.append(exc)
            self.futures.clear()
        finally:
            if errors:
                raise RuntimeError(
                    "Filesystem Zarr mirror failed for one or more component files: "
                    + "; ".join(str(error) for error in errors[:5])
                )

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _copy_file(self, local_file, relative):
        target_file = self.target_root / relative
        target_file.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, 4):
            try:
                shutil.copyfile(local_file, target_file)
                break
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(attempt)
        logging.info("Mirrored %s to %s", local_file, target_file)


class GcsZarrMirror:
    def __init__(self, source_root, bucket_name, gcs_prefix, max_workers):
        from google.cloud import storage

        self.source_root = Path(source_root)
        self.bucket_name = bucket_name
        self.bucket = storage.Client().bucket(bucket_name)
        self.gcs_prefix = gcs_prefix.strip("/")
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="gcs-zarr-mirror",
        )
        self.queued_signatures = {}
        self.futures = []

    def enqueue_changed(self, source_root=None):
        source_root = Path(source_root or self.source_root)
        for local_file in source_root.rglob("*"):
            if not local_file.is_file():
                continue
            relative = local_file.relative_to(source_root)
            try:
                stat = local_file.stat()
            except FileNotFoundError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            if self.queued_signatures.get(relative) == signature:
                continue
            self.queued_signatures[relative] = signature
            self.futures.append(
                self.executor.submit(self._upload_file, local_file, relative)
            )

    def wait(self):
        errors = []
        try:
            for future in concurrent.futures.as_completed(self.futures):
                try:
                    future.result()
                except BaseException as exc:
                    errors.append(exc)
            self.futures.clear()
        finally:
            if errors:
                raise RuntimeError(
                    "GCS Zarr mirror failed for one or more component files: "
                    + "; ".join(str(error) for error in errors[:5])
                )

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _upload_file(self, local_file, relative):
        gcs_path = f"{self.gcs_prefix}/{relative.as_posix()}"
        self.bucket.blob(gcs_path).upload_from_filename(str(local_file))
        logging.info("Mirrored %s to gs://%s/%s", local_file, self.bucket_name, gcs_path)


def _path_is_relative_to(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _ensure_gcsfuse_mount(bucket_name, mount_path):
    mount_path = Path(mount_path)
    mount_path.mkdir(parents=True, exist_ok=True)
    if os.path.ismount(mount_path):
        logging.info(
            "Cloud Storage FUSE mount already active in GenCast container: gs://%s -> %s",
            bucket_name,
            mount_path,
        )
        return

    gcsfuse = shutil.which("gcsfuse")
    if not gcsfuse:
        raise RuntimeError(
            "GENCAST_ZARR_MIRROR_TARGET points under the Cloud Storage FUSE mount, "
            "but gcsfuse is not installed in the GenCast container."
        )

    profile = os.getenv("GENCAST_GCSFUSE_PROFILE", "aiml-checkpointing").strip()
    command = [gcsfuse, "--implicit-dirs"]
    if profile:
        command.append(f"--profile={profile}")
    command.extend([bucket_name, str(mount_path)])

    logging.info(
        "Mounting Cloud Storage FUSE in GenCast container for async full-field mirror: "
        "gs://%s -> %s profile=%s",
        bucket_name,
        mount_path,
        profile or "default",
    )
    subprocess.run(command, check=True)
    if not os.path.ismount(mount_path):
        raise RuntimeError(
            f"Cloud Storage FUSE command completed but {mount_path} is not mounted."
        )
    logging.info(
        "Cloud Storage FUSE active in GenCast container: gs://%s -> %s",
        bucket_name,
        mount_path,
    )


def _configure_jax_compilation_cache():
    cache_dir = os.getenv("JAX_COMPILATION_CACHE_DIR", "").strip()
    if not cache_dir:
        logging.info("No JAX persistent compilation cache configured.")
        return

    try:
        mount_path = Path(os.getenv("GENCAST_GCSFUSE_MOUNT", "/mnt/disks/common"))
        bucket_name = os.getenv("GENCAST_GCSFUSE_BUCKET", "").strip()
        if _path_is_relative_to(cache_dir, mount_path):
            if not bucket_name:
                raise RuntimeError(
                    "JAX_COMPILATION_CACHE_DIR is under GENCAST_GCSFUSE_MOUNT, "
                    "but GENCAST_GCSFUSE_BUCKET is not set."
                )
            _ensure_gcsfuse_mount(bucket_name, mount_path)

        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        jax.config.update("jax_compilation_cache_dir", cache_dir)

        min_compile_time = os.getenv("JAX_PERSISTENT_CACHE_MIN_COMPILE_TIME_SECS")
        if min_compile_time:
            jax.config.update(
                "jax_persistent_cache_min_compile_time_secs",
                float(min_compile_time),
            )

        min_entry_size = os.getenv("JAX_PERSISTENT_CACHE_MIN_ENTRY_SIZE_BYTES")
        if min_entry_size:
            jax.config.update(
                "jax_persistent_cache_min_entry_size_bytes",
                int(min_entry_size),
            )

        explain_misses = os.getenv("JAX_EXPLAIN_CACHE_MISSES")
        if explain_misses:
            jax.config.update(
                "jax_explain_cache_misses",
                explain_misses.strip().lower() in {"1", "true", "yes", "y"},
            )

        logging.info(
            "Configured JAX persistent compilation cache: %s",
            cache_dir,
        )
    except Exception as e:
        logging.error(
            "Failed to configure JAX persistent compilation cache: %s. "
            "Proceeding without compilation cache.",
            e,
            exc_info=True,
        )




def _ensure_gcsfuse_for_path(path):
    mount_path = Path(os.getenv("GENCAST_GCSFUSE_MOUNT", "/mnt/disks/common"))
    bucket_name = os.getenv("GENCAST_GCSFUSE_BUCKET", "").strip()
    if not _path_is_relative_to(path, mount_path):
        return
    if not bucket_name:
        raise RuntimeError(
            f"{path} is under GENCAST_GCSFUSE_MOUNT, but GENCAST_GCSFUSE_BUCKET is not set."
        )
    _ensure_gcsfuse_mount(bucket_name, mount_path)


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
        all_digests = np.asarray(multihost_utils.process_allgather(digest)).reshape(-1)
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


def _apply_graphcast_nan_cleaner_upstream(dataset, fill_value, label):
    if jax.process_count() == 1:
        return dataset

    if dataset is None or SST_VAR not in dataset:
        return dataset

    nan_count = int(dataset[SST_VAR].isnull().sum().item())
    if nan_count == 0:
        return dataset

    logging.info(
        "Applying GraphCast NaNCleaner upstream to fill %d %s NaNs in %s "
        "before multihost device placement.",
        nan_count,
        SST_VAR,
        label,
    )
    cleaner = nan_cleaning.NaNCleaner(
        predictor=None,
        reintroduce_nans=False,
        fill_value=fill_value,
        var_to_clean=SST_VAR,
    )
    return cleaner._clean(dataset)


def _sst_land_mask(inputs):
    if LAND_SEA_MASK_VAR in inputs:
        mask = inputs[LAND_SEA_MASK_VAR] < 0.5
    elif SST_VAR in inputs:
        mask = inputs[SST_VAR].isnull().any(dim="time")
    else:
        return None

    indexers = {dim: 0 for dim in mask.dims if dim not in {"lat", "lon"}}
    if indexers:
        mask = mask.isel(indexers, drop=True)
    return mask


def _mask_sst_output(chunk, land_mask):
    if land_mask is None or SST_VAR not in chunk:
        return chunk
    return chunk.assign({SST_VAR: chunk[SST_VAR].where(~land_mask, np.nan)})


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
        mirror=None,
        max_pending=None,
    ):
        self._partial_path = partial_path
        self._target_times = target_times
        self._sample_values = sample_values
        self._target_levels = target_levels
        self._mirror = mirror
        self._max_pending = max_pending or int(
            os.getenv("GENCAST_ASYNC_WRITER_MAX_PENDING", "2")
        )
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._pending = []
        logging.info(
            "Initialized async GenCast prediction writer for %s with max_pending=%d.",
            self._partial_path,
            self._max_pending,
        )

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
                if self._mirror is not None:
                    self._mirror.enqueue_changed(self._partial_path)
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
        if self._mirror is not None:
            self._mirror.enqueue_changed(self._partial_path)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def initialize_jax_runtime() -> dict[str, object]:
    logging.info("Initializing JAX runtime.")
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
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    metadata_path = METADATA_DIR / f"run_metadata_{date_f}.json"
    with metadata_path.open("w") as f:
        json.dump(runtime, f, indent=2, sort_keys=True)


def _zarr_mirror_for_runtime(
    runtime: dict[str, object],
) -> FilesystemZarrMirror | GcsZarrMirror | None:
    if int(runtime.get("process_index", 0)) != 0:
        return None

    target = os.getenv("GENCAST_ZARR_MIRROR_TARGET", "").strip()
    if target:
        workers = int(os.getenv("GENCAST_ZARR_MIRROR_WORKERS", "16"))
        mount_path = Path(os.getenv("GENCAST_GCSFUSE_MOUNT", "/mnt/disks/common"))
        bucket_name = os.getenv("GENCAST_GCSFUSE_BUCKET", "").strip()
        if _path_is_relative_to(target, mount_path):
            if not bucket_name:
                raise RuntimeError(
                    "GENCAST_ZARR_MIRROR_TARGET is under GENCAST_GCSFUSE_MOUNT, "
                    "but GENCAST_GCSFUSE_BUCKET is not set."
                )
            _ensure_gcsfuse_mount(bucket_name, mount_path)
        logging.info(
            "Using filesystem/Cloud Storage FUSE GenCast Zarr mirror target %s with %d workers. "
            "Set only by cloud wrappers or operators; absent in the HPC workflow.",
            target,
            workers,
        )
        return FilesystemZarrMirror(target_root=target, max_workers=workers)

    bucket_name = os.getenv("GENCAST_ZARR_MIRROR_BUCKET", "").strip()
    gcs_prefix = os.getenv("GENCAST_ZARR_MIRROR_PREFIX", "").strip("/")
    if not bucket_name or not gcs_prefix:
        logging.info(
            "No GenCast Zarr mirror configured; writing full-field forecast only "
            "to the local output directory."
        )
        return None

    workers = int(os.getenv("GENCAST_ZARR_MIRROR_WORKERS", "16"))
    logging.info(
        "Using direct GCS API GenCast Zarr mirror to gs://%s/%s with %d workers. "
        "GCS-FUSE is not in use for this run.",
        bucket_name,
        gcs_prefix,
        workers,
    )
    return GcsZarrMirror(
        source_root=FCST_DIR,
        bucket_name=bucket_name,
        gcs_prefix=gcs_prefix,
        max_workers=workers,
    )


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
            from graphcast import rollout
        else:
            import rollout_patched as rollout
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
            reintroduce_nans=jax.process_count() == 1,
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

    return run_forward_pmap, task_config, min_by_level, rollout


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
    # forcings = forcings.drop_vars("lat")
    forcings = forcings.expand_dims("batch", axis=0)

    # forcings = forcings.transpose("batch", "time", "lon")
    forcings = forcings.transpose("batch", "time", "lat", "lon")

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
    writes_outputs = _runtime_writes_outputs(runtime)
    mirror = _zarr_mirror_for_runtime(runtime)
    outfile = None
    try:
        date = datetime.datetime.strptime(date_f, "%Y%m%dT%H")
        if DEBUG:
            logging.info(
                "Running in DEBUG mode with reduced ensemble members and days."
            )
        outfile = _forecast_output_path(date_f)
        if writes_outputs:
            _ensure_gcsfuse_for_path(FCST_DIR)
            logging.info("GenCast process 0 forecast output directory: %s", FCST_DIR)
        else:
            logging.info(
                "GenCast JAX process %s/%s participates in distributed inference "
                "without writing duplicate full-field Zarr outputs.",
                runtime.get("process_index", 0),
                runtime.get("process_count", 1),
            )
        if writes_outputs and outfile.exists():
            try:
                # Verify that the existing Zarr store is complete and readable
                xr.open_zarr(outfile)
                logging.info(f"Output file {outfile} already exists and is valid. Skipping run.")
                run_status = "skipped"
                if mirror is not None:
                    logging.info("Reconciling existing GenCast Zarr store to configured mirror.")
                    mirror.enqueue_changed(outfile)
                    mirror.wait()
                write_run_metadata(date_f, runtime)
                return
            except Exception as e:
                logging.warning(
                    f"Output directory {outfile} exists but is not a valid Zarr store ({e}). "
                    "Removing corrupt/partial store to restart forecast generation."
                )
                _remove_store(outfile)

        if writes_outputs:
            FCST_DIR.mkdir(parents=True, exist_ok=True)
            _remove_stale_partial_temp_dirs(outfile)
        with _TimedOperation("get_model"):
            run_forward_pmap, task_config, min_by_level, rollout = get_model()
        with _TimedOperation("initial condition import"):
            ic = import_ic(task_config, date)
        output_sst_land_mask = _sst_land_mask(ic)
        ic = _apply_graphcast_nan_cleaner_upstream(
            ic,
            min_by_level,
            "initial conditions",
        )
        _log_multihost_dataset_differences(ic, "initial conditions")
        lat = ic.lat.values
        lon = ic.lon.values
        level = ic.level.values
        with _TimedOperation("forcings and target template construction"):
            forcings_ds, targets_template_ds = get_forcings_targets(
                task_config, date, N_STEPS, lat, lon, level
            )
        forcings_ds = _apply_graphcast_nan_cleaner_upstream(
            forcings_ds,
            min_by_level,
            "forcings",
        )
        targets_template_ds = _apply_graphcast_nan_cleaner_upstream(
            targets_template_ds,
            min_by_level,
            "target template",
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

        logging.info(
            "Starting forecast generation: %d total steps, %d ensemble members, "
            "%d steps per chunk, %d total chunks, %d pmap devices.",
            total_steps,
            num_ensemble_members,
            num_steps_per_chunk,
            total_chunks,
            len(pmap_devices),
        )

        target_levels = (
            saved_targets_template_ds.level.values
            if "level" in saved_targets_template_ds.coords
            else None
        )

        if writes_outputs:
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
                if mirror is not None:
                    mirror.enqueue_changed(partial_outfile)

                chunks_queued = 0
                with _AsyncPredictionWriter(
                    partial_path=partial_outfile,
                    target_times=targets_template_ds.time.values,
                    sample_values=sample_values,
                    target_levels=target_levels,
                    mirror=mirror,
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
                        chunk = _mask_sst_output(chunk, output_sst_land_mask)
                        chunk_generation_seconds = (
                            time.perf_counter() - chunk_wait_started
                        )
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
                if mirror is not None:
                    logging.info(
                        "Reconciling consolidated GenCast partial Zarr to configured mirror."
                    )
                    mirror.enqueue_changed(partial_outfile)
                    mirror.wait()
                with _TimedOperation("final output rename"):
                    partial_outfile.rename(outfile)
                if mirror is not None:
                    logging.info("Reconciling final GenCast Zarr store to configured mirror.")
                    mirror.enqueue_changed(outfile)
                    mirror.wait()
            logging.info(f"Forecast written to {outfile}.")
        else:
            chunks_queued = 0
            chunk_wait_started = time.perf_counter()
            for chunk in rollout.chunked_prediction_generator_multiple_runs(
                predictor_fn=run_forward_pmap,
                rngs=rngs,
                inputs=ic,
                targets_template=targets_template_ds,
                forcings=forcings_ds,
                num_steps_per_chunk=num_steps_per_chunk,
                num_samples=num_ensemble_members,
                pmap_devices=pmap_devices,
            ):
                _ = _mask_sst_output(chunk, output_sst_land_mask)
                chunks_queued += 1
                logging.info(
                    "Generated chunk %d/%d on non-writing process after %.2fs.",
                    chunks_queued,
                    total_chunks,
                    time.perf_counter() - chunk_wait_started,
                )
                chunk_wait_started = time.perf_counter()
        run_status = "completed"
    except Exception as exc:
        if writes_outputs and outfile is not None and outfile.exists():
            logging.warning(
                f"GenCast run failed. Cleaning up partial output directory {outfile}."
            )
            _remove_store(outfile)
        raise exc
    finally:
        if mirror is not None:
            mirror.close()
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
        _configure_jax_compilation_cache()
        runtime = initialize_jax_runtime()
        logging.info(
            "JAX persistent compilation cache active for process %s/%s",
            runtime.get("process_index", 0),
            runtime.get("process_count", 1),
        )
        run_model(date_f, runtime)
    except Exception as exc:
        _log_exception_summary(exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
