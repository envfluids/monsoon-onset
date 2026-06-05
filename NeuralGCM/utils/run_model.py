import argparse
import concurrent.futures
import logging
import multiprocessing as mp
import os
import pickle
import queue
import shutil
import threading
import time
import traceback
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xarray
from zarr.codecs import BloscCodec, BloscShuffle

warnings.filterwarnings(
    "ignore", message="Consolidated metadata is currently not part.*"
)

BASE = Path(__file__).resolve().parent.parent
OUTPUT_PATH = Path(os.environ.get("NEURALGCM_RAW_OUTPUT_DIR", BASE / "output" / "raw"))
MODEL_NAME = "models_v1_precip_stochastic_precip_2_8_deg.pkl"
N_MEMBERS = 30
MIRROR_WORKERS = max(1, int(os.environ.get("NEURALGCM_ZARR_MIRROR_WORKERS", "8")))

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)

_MODEL_RUNTIME = None


class ZarrMirror:
    def __init__(self, source_root, target_root, max_workers):
        self.source_root = Path(source_root)
        self.target_root = Path(target_root)
        self.partial_marker = self.target_root.with_name(f"{self.target_root.name}.partial")
        self.complete_marker = self.target_root.with_name(f"{self.target_root.name}.complete")
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="zarr-mirror",
        )
        self.queued_signatures = {}
        self.futures = []
        self.lock = threading.Lock()

    def mark_partial(self):
        self.target_root.parent.mkdir(parents=True, exist_ok=True)
        if self.partial_marker.exists() and self.target_root.exists():
            logging.warning(
                "Removing previous partial NeuralGCM Zarr mirror target: %s",
                self.target_root,
            )
            shutil.rmtree(self.target_root)
        self.complete_marker.unlink(missing_ok=True)
        self.partial_marker.write_text(
            f"partial NeuralGCM Zarr mirror for {self.target_root.name}\n",
            encoding="utf-8",
        )

    def mark_complete(self):
        self.partial_marker.unlink(missing_ok=True)
        self.complete_marker.write_text(
            f"complete NeuralGCM Zarr mirror for {self.target_root.name}\n",
            encoding="utf-8",
        )

    def enqueue_changed(self, changed_root=None, *, force=False):
        changed_root = Path(changed_root or self.source_root)
        for local_file in changed_root.rglob("*"):
            if not local_file.is_file():
                continue
            relative = local_file.relative_to(changed_root)
            try:
                stat = local_file.stat()
            except FileNotFoundError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            with self.lock:
                if not force and self.queued_signatures.get(relative) == signature:
                    continue
                self.queued_signatures[relative] = signature
                self.futures.append(
                    self.executor.submit(self._copy_file, local_file, relative)
                )

    def wait(self):
        errors = []
        while True:
            with self.lock:
                futures = self.futures
                self.futures = []
            if not futures:
                break
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            raise RuntimeError(
                "Zarr mirror failed for one or more component files: "
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


_CHUNK_SHAPE_BY_DIM = {
    "ensemble": 1,
    "time": 24,
    "level": 1,
    "surface": 1,
    "longitude": 64,
    "latitude": 32,
}
# Shards remain modest for the pressure-level fields while packing multiple
# inner chunks into each object written through GCS FUSE.
_SHARD_SHAPE_BY_DIM = {
    "ensemble": 1,
    "time": 168,
    "level": 1,
    "surface": 1,
    "longitude": 128,
    "latitude": 64,
}


def _storage_shape(dim, size):
    chunk = min(size, _CHUNK_SHAPE_BY_DIM[dim])
    shard_limit = min(size, _SHARD_SHAPE_BY_DIM[dim])
    shard = max(chunk, shard_limit - (shard_limit % chunk))
    return chunk, shard


def _v3_encoding(ds):
    compressors = (
        BloscCodec(cname="zstd", clevel=3, shuffle=BloscShuffle.bitshuffle),
    )
    encoding = {}
    for name, da in ds.data_vars.items():
        if not da.dims or not all(dim in _CHUNK_SHAPE_BY_DIM for dim in da.dims):
            continue
        storage_shapes = [_storage_shape(dim, da.sizes[dim]) for dim in da.dims]
        encoding[name] = {
            "chunks": tuple(chunk for chunk, _ in storage_shapes),
            "shards": tuple(shard for _, shard in storage_shapes),
            "compressors": compressors,
        }
    return encoding


def _dask_shard_chunks(ds):
    return {
        dim: _storage_shape(dim, size)[1]
        for dim, size in ds.sizes.items()
        if dim in _SHARD_SHAPE_BY_DIM
    }


def _get_model_runtime():
    global _MODEL_RUNTIME
    if _MODEL_RUNTIME is not None:
        return _MODEL_RUNTIME

    import neuralgcm
    from dinosaur import horizontal_interpolation, spherical_harmonic

    with (BASE / "weights" / MODEL_NAME).open("rb") as f:
        ckpt = pickle.load(f)

    ckpt["model_config_str"] = "\n".join(
        [
            ckpt["model_config_str"],
            (
                "dycore/SequentialStepFilter.filter_modules ="
                " (@dycore/ExponentialFilter,@stability/ExponentialFilter,"
                "@surface_pressure/FixGlobalMeanFilter)"
            ),
        ]
    )
    model = neuralgcm.PressureLevelModel.from_checkpoint(ckpt)
    era5_grid = spherical_harmonic.Grid(
        latitude_nodes=721,
        longitude_nodes=1440,
        latitude_spacing="equiangular_with_poles",
        longitude_offset=np.float32(0.0),
    )
    regridder = horizontal_interpolation.ConservativeRegridder(
        era5_grid, model.data_coords.horizontal, skipna=True
    )
    _MODEL_RUNTIME = model, regridder
    return _MODEL_RUNTIME


def get_forcings_clim(year):
    forcings_clim = xarray.open_dataset(
        BASE / "data" / "forcings" / "SST-SeaIce_clim_1979_2017_no_leap.nc"
    )
    new_time = pd.date_range(
        f"{year}-01-01T00:00:00.000000000",
        f"{year}-12-31T00:00:00.000000000",
        freq="D",
    )
    if new_time.shape[0] > 365:
        new_time = new_time[new_time.dayofyear != 60]
    return forcings_clim.assign_coords(time=("time", new_time))


def _prepare_worker(date, date_f, forcings_clim):
    from dinosaur import xarray_utils

    model, regridder = _get_model_runtime()
    gfs_ds = xarray.open_dataset(
        BASE / "raw" / "ncep_ic" / "processed" / f"gdas_{date_f}.nc"
    )
    gfs_date = gfs_ds.sel(time=date)
    eval_era5 = xarray_utils.regrid(gfs_date, regridder)
    eval_era5 = xarray_utils.fill_nan_with_nearest(eval_era5)
    forcings_clim_sub = forcings_clim.sel(
        time=slice(eval_era5.time, eval_era5.time + np.timedelta64(90, "D"))
    )
    return {
        "model": model,
        "gfs_ds": gfs_ds,
        "eval_era5": eval_era5,
        "forcing_initial": model.forcings_from_xarray(forcings_clim_sub.isel(time=0)),
        "all_forcings": model.forcings_from_xarray(forcings_clim_sub),
        "dt": np.timedelta64(6, "h"),
        "steps": 45 * 24 // 6,
    }


def build_member_dataset(runtime, member):
    import jax

    member = int(member)
    model = runtime["model"]
    eval_era5 = runtime["eval_era5"]
    dt = runtime["dt"]
    steps = runtime["steps"]
    times = eval_era5.time.values + (np.arange(1, steps + 1) * dt)

    inputs = model.inputs_from_xarray(eval_era5)
    rng_key = jax.random.key(member)
    initial_state = model.encode(inputs, runtime["forcing_initial"], rng_key)
    _, predictions = model.unroll(
        initial_state,
        runtime["all_forcings"],
        steps=steps,
        timedelta=dt,
        start_with_input=False,
    )
    predictions_ds = model.data_to_xarray(predictions, times=times)

    specific_humidity = predictions_ds[["specific_humidity"]].sel(
        level=[
            100,
            200,
            300,
            400,
            500,
            550,
            600,
            650,
            700,
            750,
            775,
            800,
            825,
            850,
            875,
            900,
            925,
            950,
            975,
            1000,
        ]
    )
    geopotential = predictions_ds[["geopotential"]].sel(
        level=[850, 900, 925, 950, 975, 1000]
    )
    wind = predictions_ds[["u_component_of_wind", "v_component_of_wind"]].sel(
        level=[200, 850]
    )
    precipitation = predictions_ds[["precipitation_cumulative_mean"]]
    data = xarray.merge(
        [specific_humidity, geopotential, wind, precipitation],
        join="outer",
    )
    data = data.expand_dims("ensemble", axis=0)
    data["ensemble"] = [member]
    return data.chunk(_dask_shard_chunks(data))


def worker_loop(
    worker_id,
    gpu_id,
    members,
    result_queue,
    stop_event,
    date_f,
):
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    forcings_clim = None
    runtime = None
    try:
        import jax

        logging.info(
            "Worker %s using GPU %s with devices: %s",
            worker_id,
            gpu_id,
            jax.local_devices(),
        )
        date = datetime.strptime(date_f, "%Y%m%dT%H")
        forcings_clim = get_forcings_clim(date.year)
        runtime = _prepare_worker(date, date_f, forcings_clim)

        for member in members:
            if stop_event.is_set():
                break

            logging.info("Worker %s starting member %s", worker_id, member)
            ds = build_member_dataset(runtime, member)
            while not stop_event.is_set():
                try:
                    result_queue.put(("member", int(member), ds), timeout=5)
                    logging.info("Worker %s completed member %s", worker_id, member)
                    break
                except queue.Full:
                    continue

        result_queue.put(("done", worker_id))
    except BaseException:
        stop_event.set()
        result_queue.put(("error", worker_id, traceback.format_exc()))
    finally:
        if runtime is not None:
            runtime["gfs_ds"].close()
        if forcings_clim is not None:
            forcings_clim.close()


def writer_loop(
    result_queue,
    status_queue,
    stop_event,
    partial_path,
    final_path,
    mirror_target,
    mirror_workers,
    members,
    n_workers,
    date_f,
):
    pending = {}
    next_member_index = 0
    done_workers = set()
    mirror = None
    if mirror_target:
        mirror = ZarrMirror(partial_path, mirror_target, mirror_workers)
        mirror.mark_partial()
        logging.info("Streaming NeuralGCM Zarr components to %s", mirror_target)

    try:
        while next_member_index < len(members):
            try:
                message = result_queue.get(timeout=5)
            except queue.Empty:
                if stop_event.is_set():
                    raise RuntimeError(
                        "Writer stopped before all ensemble members were received."
                    )
                continue

            message_type = message[0]
            if message_type == "member":
                _, member, ds = message
                pending[member] = ds

                while (
                    next_member_index < len(members)
                    and members[next_member_index] in pending
                ):
                    member = members[next_member_index]
                    ds = pending.pop(member)
                    if next_member_index == 0:
                        logging.info(
                            "Saving NeuralGCM member %s for %s to %s (mode=w)",
                            member,
                            date_f,
                            partial_path,
                        )
                        ds.to_zarr(
                            partial_path,
                            zarr_format=3,
                            mode="w",
                            encoding=_v3_encoding(ds),
                        )
                    else:
                        logging.info(
                            "Saving NeuralGCM member %s for %s to %s "
                            "(mode=a, append)",
                            member,
                            date_f,
                            partial_path,
                        )
                        ds.to_zarr(
                            partial_path,
                            zarr_format=3,
                            mode="a",
                            append_dim="ensemble",
                        )
                    if mirror is not None:
                        mirror.enqueue_changed(partial_path)
                    next_member_index += 1

            elif message_type == "done":
                _, worker_id = message
                done_workers.add(worker_id)
                if len(done_workers) == n_workers and next_member_index < len(members):
                    missing = [
                        member
                        for member in members[next_member_index:]
                        if member not in pending
                    ]
                    raise RuntimeError(
                        "All workers finished before all members were written. "
                        f"Missing members: {missing}"
                    )
            elif message_type == "error":
                _, worker_id, error = message
                stop_event.set()
                raise RuntimeError(f"Worker {worker_id} failed:\n{error}")
            else:
                raise RuntimeError(f"Unknown writer queue message: {message_type}")

        if mirror is not None:
            logging.info("Waiting for in-flight NeuralGCM Zarr mirror copies")
            mirror.wait()
        partial_path.rename(final_path)
        if mirror is not None:
            logging.info("Reconciling final NeuralGCM Zarr store to mirror target")
            mirror.enqueue_changed(final_path, force=True)
            mirror.wait()
            mirror.mark_complete()
        status_queue.put(("done", str(final_path)))
    except BaseException:
        stop_event.set()
        status_queue.put(("error", "writer", traceback.format_exc()))
    finally:
        if mirror is not None:
            mirror.close()


def _all_members(seed):
    members = np.arange(1, N_MEMBERS + 1)
    if seed is None:
        logging.info("No seed provided, using default members")
        return members
    logging.info("Using seed: %s", seed)
    return members + (seed * N_MEMBERS)


def _visible_gpu_ids():
    configured = os.environ.get("CUDA_VISIBLE_DEVICES")
    if configured is not None:
        if not configured or configured == "-1":
            return []
        return [gpu.strip() for gpu in configured.split(",") if gpu.strip()]

    try:
        import jax

        return [str(index) for index in range(jax.local_device_count(backend="gpu"))]
    except RuntimeError:
        return []


def _member_batches(members, n_workers):
    return [members[worker_id::n_workers] for worker_id in range(n_workers)]


def run_parallel_model(date_f, seed, gpu_ids, mirror_target=None):
    members = [int(member) for member in _all_members(seed)]
    if not gpu_ids:
        raise RuntimeError("No CUDA GPUs detected for NeuralGCM ensemble inference.")

    final_path = OUTPUT_PATH / f"{date_f}.zarr"
    partial_path = OUTPUT_PATH / f"{date_f}_partial.zarr"
    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        logging.warning("Final output store %s already exists; skipping run", final_path)
        return final_path
    if partial_path.exists():
        logging.warning("Removing stale partial output store: %s", partial_path)
        shutil.rmtree(partial_path)

    n_workers = min(len(gpu_ids), len(members))
    member_batches = _member_batches(members, n_workers)
    ctx = mp.get_context("spawn")
    result_queue = ctx.Queue(maxsize=max(2, n_workers))
    status_queue = ctx.Queue()
    stop_event = ctx.Event()

    writer = ctx.Process(
        target=writer_loop,
        args=(
            result_queue,
            status_queue,
            stop_event,
            partial_path,
            final_path,
            mirror_target,
            MIRROR_WORKERS,
            members,
            n_workers,
            date_f,
        ),
        name="neuralgcm-writer",
    )
    writer.start()

    workers = []
    for worker_id, (gpu_id, member_batch) in enumerate(
        zip(gpu_ids[:n_workers], member_batches)
    ):
        logging.info(
            "Starting worker %s on GPU %s with members %s",
            worker_id,
            gpu_id,
            member_batch,
        )
        worker = ctx.Process(
            target=worker_loop,
            args=(
                worker_id,
                gpu_id,
                member_batch,
                result_queue,
                stop_event,
                date_f,
            ),
            name=f"neuralgcm-worker-{worker_id}",
        )
        worker.start()
        workers.append(worker)

    try:
        while True:
            try:
                status = status_queue.get(timeout=5)
            except queue.Empty:
                failed_workers = [
                    worker
                    for worker in workers
                    if worker.exitcode is not None and worker.exitcode != 0
                ]
                if failed_workers:
                    failed = ", ".join(
                        f"{worker.name} exited {worker.exitcode}"
                        for worker in failed_workers
                    )
                    raise RuntimeError(f"Ensemble worker process failed: {failed}")
                if writer.exitcode is not None:
                    if writer.exitcode == 0:
                        break
                    raise RuntimeError(
                        f"Writer process exited with code {writer.exitcode}"
                    )
                continue

            if status[0] == "done":
                break
            if status[0] == "error":
                _, source, error = status
                raise RuntimeError(f"{source} failed:\n{error}")
            raise RuntimeError(f"Unknown status message: {status[0]}")
    except BaseException:
        stop_event.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
        if writer.is_alive():
            writer.terminate()
        raise
    finally:
        for worker in workers:
            worker.join()
        writer.join()

    failed_workers = [
        worker
        for worker in workers
        if worker.exitcode is not None and worker.exitcode != 0
    ]
    if failed_workers:
        failed = ", ".join(
            f"{worker.name} exited {worker.exitcode}" for worker in failed_workers
        )
        raise RuntimeError(f"Ensemble worker process failed: {failed}")
    if writer.exitcode not in (0, None):
        raise RuntimeError(f"Writer process exited with code {writer.exitcode}")
    return final_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, required=True, help="Forecast date YYYYMMDDTHH")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    args = parser.parse_args()

    gpu_ids = _visible_gpu_ids()
    logging.info("Detected %s CUDA GPUs: %s", len(gpu_ids), gpu_ids)
    mirror_target = os.environ.get("NEURALGCM_ZARR_MIRROR_TARGET")
    final_path = run_parallel_model(
        args.date,
        args.seed,
        gpu_ids,
        mirror_target=mirror_target,
    )
    logging.info("Model run complete. Final output saved to %s", final_path)


if __name__ == "__main__":
    main()
