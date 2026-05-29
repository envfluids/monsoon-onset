import argparse
import concurrent.futures
import gc
import logging
import multiprocessing as mp
import os
import queue
import shutil
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import xarray as xr
from zarr.codecs import BloscCodec, BloscShuffle
from anemoi.inference.outputs.printer import print_state
from anemoi.inference.runners.simple import SimpleRunner
from preprocess_ic import get_ic
from scipy.sparse import load_npz
import json

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

BASE = Path(__file__).resolve().parent.parent
TFM_N320_LATLON_NAME = (
    "7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz"
)
TFM_N320_LATLON_PATH = (
    Path(__file__).resolve().parent.parent
    / "EKR"
    / "mir_16_linear"
    / TFM_N320_LATLON_NAME
)

TFM_N320_LATLON = load_npz(TFM_N320_LATLON_PATH)

latitudes = np.linspace(90, -90, 721)
longitudes = np.linspace(0, 359.75, 1440)

REPO_ROOT = BASE.parent
MODEL_CONFIG_PATH = REPO_ROOT / "config" / "models.json"
with open(MODEL_CONFIG_PATH, "r") as f:
    MODEL_CONFIG = json.load(f)

WEIGHTS_DIR = BASE / "weights"

class ZarrMirror:
    def __init__(self, source_root, target_root, max_workers):
        self.source_root = Path(source_root)
        self.target_root = Path(target_root)
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="zarr-mirror",
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
        logging.info(f"Mirrored {local_file} to {target_file}")


# def get_state(date_f):
#     logging.info(f"Reading input state for date: {date_f}")
#     with open(f"../raw/ifs_ic/input_state_{date_f}.pkl", "rb") as f:
#         data = pickle.load(f)
#         vars_to_remove = ["swvl1", "swvl2"]
#         for key in vars_to_remove:
#             if key in data["fields"]:
#                 logging.info(f"Removing variable {key} from input state")
#                 data["fields"].pop(key)
#         return data


_CHUNK_SHAPE_BY_DIM = {
    "time": 1,
    "number": 1,
    "prediction_timedelta": 24,
    "lat": 90,
    "lon": 180,
}
# Shard shape must be a positive multiple of chunk shape on every dim.
# One shard packs a full single-member field (7×8×8 = 448 chunks).
_SHARD_SHAPE_BY_DIM = {
    "time": 1,
    "number": 1,
    "prediction_timedelta": 168,
    "lat": 720,
    "lon": 1440,
}


def _v3_encoding(ds):
    compressors = (
        BloscCodec(cname="zstd", clevel=3, shuffle=BloscShuffle.bitshuffle),
    )
    encoding = {}
    for name, da in ds.data_vars.items():
        if not da.dims or not all(d in _CHUNK_SHAPE_BY_DIM for d in da.dims):
            continue
        encoding[name] = {
            "chunks": tuple(_CHUNK_SHAPE_BY_DIM[d] for d in da.dims),
            "shards": tuple(_SHARD_SHAPE_BY_DIM[d] for d in da.dims),
            "compressors": compressors,
        }
    return encoding


def process_step(output_state):
    output_state, runcount = output_state
    data_vars = {}
    logging.info(f"Processing step {runcount} for date: {output_state['date']}")
    for field in output_state["fields"]:
        values = (
            TFM_N320_LATLON * output_state["fields"][field].reshape(-1, 1)
        ).reshape(721, 1440)
        data_vars[field] = (["lat", "lon"], values.astype(np.float32))

    step_ds = xr.Dataset(
        data_vars,
        coords={"lat": latitudes, "lon": longitudes},
    )
    step_ds = step_ds.expand_dims("step")
    step_ds["step"] = [int(runcount)]
    return step_ds


def build_member_dataset(
    input_state, ens_number, date, lead_time, save_vars, cpkt_path, gpu_id
):
    torch.cuda.set_device(gpu_id)
    torch.manual_seed(ens_number)

    runner = None
    try:
        runner = SimpleRunner(cpkt_path, device=f"cuda:{gpu_id}")
        datasets = []
        runcount = 6
        for state in runner.run(input_state=input_state, lead_time=lead_time):
            print_state(state)
            datasets.append(
                process_step(
                    (
                        {
                            "date": state["date"],
                            "fields": {var: state["fields"][var] for var in save_vars},
                            "latitudes": state["latitudes"],
                            "longitudes": state["longitudes"],
                        },
                        runcount,
                    )
                )
            )
            print(
                f"Completed ensemble {ens_number} forecast for step {runcount}, "
                "results in memory"
            )
            runcount += 6

        ds = xr.concat([output_state for output_state in datasets], dim="step")
        ds = ds.rename({"step": "prediction_timedelta"})
        ds["prediction_timedelta"] = (
            ds["prediction_timedelta"]
            .astype("timedelta64[h]")
            .astype("timedelta64[ns]")
        )
        ds = ds.expand_dims("number")
        ds["number"] = [int(ens_number)]
        ds = ds.expand_dims("time")
        ds["time"] = [date]

        # Dask chunks must align with zarr v3 shard shape (the on-disk write
        # unit). Inner zarr chunks for fast partial reads are set separately in
        # `_v3_encoding`.
        ds = ds.chunk(
            {dim: _SHARD_SHAPE_BY_DIM[dim] for dim in _SHARD_SHAPE_BY_DIM}
        )
        return ds
    finally:
        if runner is not None:
            del runner
        gc.collect()
        torch.cuda.empty_cache()


def worker_loop(
    worker_id,
    gpu_id,
    task_queue,
    input_state,
    date,
    lead_time,
    save_vars,
    cpkt_path,
    result_queue,
    stop_event,
):
    logging.info(f"Worker {worker_id} using GPU {gpu_id}")
    try:
        while not stop_event.is_set():
            try:
                ens_number = task_queue.get(timeout=5)
            except queue.Empty:
                continue

            if ens_number is None:
                break

            if stop_event.is_set():
                break

            logging.info(
                f"Worker {worker_id} starting ensemble {ens_number} on GPU {gpu_id}"
            )
            ds = build_member_dataset(
                input_state,
                ens_number,
                date,
                lead_time,
                save_vars,
                cpkt_path,
                gpu_id,
            )
            while not stop_event.is_set():
                try:
                    result_queue.put(("member", ens_number, ds), timeout=5)
                    logging.info(
                        f"Worker {worker_id} completed ensemble {ens_number}"
                    )
                    break
                except queue.Full:
                    continue

            del ds
            gc.collect()
            torch.cuda.empty_cache()

        result_queue.put(("done", worker_id))
    except BaseException:
        stop_event.set()
        result_queue.put(("error", worker_id, traceback.format_exc()))


def writer_loop(
    result_queue,
    status_queue,
    stop_event,
    filename,
    final_filename,
    mirror_target,
    mirror_workers,
    n_members,
    n_workers,
    date,
):
    pending = {}
    next_member = 0
    done_workers = set()
    mirror = None
    if mirror_target:
        mirror = ZarrMirror(filename, mirror_target, mirror_workers)
        logging.info(f"Streaming AIFS-ENS Zarr components to {mirror_target}")

    try:
        while next_member < n_members:
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
                _, ens_number, ds = message
                pending[ens_number] = ds

                while next_member in pending:
                    ds = pending.pop(next_member)
                    if next_member == 0:
                        logging.info(
                            f"Saving forecast for ensemble {next_member} and date "
                            f"{date} to {filename} (mode=w)"
                        )
                        ds.to_zarr(
                            filename,
                            zarr_format=3,
                            mode="w",
                            encoding=_v3_encoding(ds),
                        )
                    else:
                        logging.info(
                            f"Saving forecast for ensemble {next_member} and date "
                            f"{date} to {filename} (mode=a, append)"
                        )
                        ds.to_zarr(
                            filename,
                            zarr_format=3,
                            mode="a",
                            append_dim="number",
                        )

                    del ds
                    gc.collect()
                    if mirror is not None:
                        mirror.enqueue_changed(filename)
                    next_member += 1

            elif message_type == "done":
                _, worker_id = message
                done_workers.add(worker_id)
                if len(done_workers) == n_workers and next_member < n_members:
                    missing = [
                        member
                        for member in range(next_member, n_members)
                        if member not in pending
                    ]
                    raise RuntimeError(
                        f"All workers finished before all members were written. "
                        f"Missing members: {missing}"
                    )

            elif message_type == "error":
                _, worker_id, error = message
                stop_event.set()
                raise RuntimeError(f"Worker {worker_id} failed:\n{error}")

            else:
                raise RuntimeError(f"Unknown writer queue message: {message_type}")

        if mirror is not None:
            logging.info("Waiting for in-flight AIFS-ENS Zarr mirror copies")
            mirror.wait()
        filename.rename(final_filename)
        if mirror is not None:
            logging.info("Reconciling final AIFS-ENS Zarr store to mirror target")
            mirror.enqueue_changed(final_filename)
            mirror.wait()
        status_queue.put(("done", str(final_filename)))
    except BaseException:
        stop_event.set()
        status_queue.put(("error", "writer", traceback.format_exc()))
    finally:
        if mirror is not None:
            mirror.close()


def run_model(
    model_config,
    output_dir,
    n_members,
    date_f,
    lead_time,
    save_vars,
    cpkt_path,
    ngpus,
    mirror_target=None,
    mirror_workers=8,
):
    date = pd.to_datetime(date_f, format="%Y%m%dT%H")
    filename = output_dir / f"init_{date_f}_partial.zarr"
    final_filename = output_dir / f"init_{date_f}.zarr"
    if final_filename.exists():
        logging.warning(
            f"Final output file {final_filename} already exists. Skipping model run."
        )
        return final_filename

    if ngpus < 1:
        raise RuntimeError("No CUDA GPUs detected for AIFS ensemble inference.")

    if filename.exists():
        logging.warning(f"Partial output file {filename} already exists. Removing it.")
        shutil.rmtree(filename)

    input_state = get_ic(date, model_config)
    n_workers = min(ngpus, n_members)
    ctx = mp.get_context("spawn")
    task_queue = ctx.Queue()
    result_queue = ctx.Queue(maxsize=max(2, n_workers))
    status_queue = ctx.Queue()
    stop_event = ctx.Event()

    for ens_number in range(n_members):
        task_queue.put(ens_number)
    for _ in range(n_workers):
        task_queue.put(None)

    writer = ctx.Process(
        target=writer_loop,
        args=(
            result_queue,
            status_queue,
            stop_event,
            filename,
            final_filename,
            mirror_target,
            mirror_workers,
            n_members,
            n_workers,
            date,
        ),
        name="aifs-ens-writer",
    )
    writer.start()

    workers = []
    for worker_id in range(n_workers):
        gpu_id = worker_id
        logging.info(f"Starting worker {worker_id} on GPU {gpu_id}")
        worker = ctx.Process(
            target=worker_loop,
            args=(
                worker_id,
                gpu_id,
                task_queue,
                input_state,
                date,
                lead_time,
                save_vars,
                cpkt_path,
                result_queue,
                stop_event,
            ),
            name=f"aifs-ens-worker-{worker_id}",
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

    return final_filename


def main():
    parser = argparse.ArgumentParser(
        description="Process weather data for a given year"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Date for the inference in YYYYMMDDTHH format",
        required=True,
    )
    parser.add_argument(
        "--model",
        type=str,
        required=True,
        help="Model name to run (must be defined in model_config.json)",
    )
    args = parser.parse_args()
    date_f = args.date
    model_name = args.model
    model_config = MODEL_CONFIG.get(model_name)

    if model_config is None:
        logging.error(f"Model {model_name} not found in model_config.json")
        raise ValueError(f"Model {model_name} not defined in model_config.json")
    
    output_dir = BASE / "output" / "raw" / model_name
    output_dir.mkdir(parents=True, exist_ok=True)

    cpkt_path = WEIGHTS_DIR / model_config["weights"]
    logging.info(f"Using model weights: {cpkt_path}")
    logging.info(f"Running for: {date_f}")
    logging.info(f"Output directory: {output_dir}")

    save_fields = [
        "2t",
        "u_850",
        "v_850",
        "u_200",
        "v_200",
        "u_700",
        "v_700",
        "z_200",
        "z_500",
        "z_700",
        "z_850",
        "tp",
        "tcw",
        "msl",
    ]

    lead_time = 24 * 46
    n_members = 25

    ngpus = torch.cuda.device_count()
    logging.info(f"Detected {ngpus} CUDA GPUs")

    mirror_target = os.environ.get("AIFS_ENS_ZARR_MIRROR_TARGET")
    mirror_workers = int(os.environ.get("AIFS_ENS_ZARR_MIRROR_WORKERS", "8"))
    if mirror_target:
        logging.info(f"AIFS-ENS Zarr mirror target: {mirror_target}")

    final_filename = run_model(
        model_config,
        output_dir,
        n_members,
        date_f,
        lead_time,
        save_fields,
        cpkt_path,
        ngpus,
        mirror_target=mirror_target,
        mirror_workers=mirror_workers,
    )
    logging.info(f"Model run complete. Final output saved to {final_filename}")
    logging.info("Exiting inference script")


if __name__ == "__main__":
    main()
