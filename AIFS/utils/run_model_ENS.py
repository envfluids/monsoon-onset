import argparse
import gc
import logging
import multiprocessing as mp
import queue
import shutil
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import xarray as xr
from anemoi.inference.outputs.printer import print_state
from anemoi.inference.runners.simple import SimpleRunner
from preprocess_ic import get_ic, postprocess_ens
from scipy.sparse import load_npz

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

        ds = ds.chunk(
            {
                "time": 1,
                "number": 1,
                "prediction_timedelta": -1,
                "lat": -1,
                "lon": -1,
            }
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
    members,
    input_state,
    date,
    lead_time,
    save_vars,
    cpkt_path,
    result_queue,
    stop_event,
):
    logging.info(f"Worker {worker_id} using GPU {gpu_id} for members {members}")
    try:
        for ens_number in members:
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
            result_queue.put(("member", ens_number, ds))
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
    n_members,
    n_workers,
    date,
):
    pending = {}
    next_member = 0
    done_workers = set()

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
                        ds.to_zarr(filename, zarr_format=2, mode="w")
                    else:
                        logging.info(
                            f"Saving forecast for ensemble {next_member} and date "
                            f"{date} to {filename} (mode=a, append)"
                        )
                        ds.to_zarr(
                            filename,
                            zarr_format=2,
                            mode="a",
                            append_dim="number",
                        )

                    del ds
                    gc.collect()
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

        filename.rename(final_filename)
        status_queue.put(("done", str(final_filename)))
    except BaseException:
        stop_event.set()
        status_queue.put(("error", "writer", traceback.format_exc()))


def run_model(output_dir, n_members, date_f, lead_time, save_vars, cpkt_path, ngpus):
    date = pd.to_datetime(date_f, format="%Y%m%dT%H")
    filename = output_dir / f"init_{date_f}_partial.zarr"
    final_filename = output_dir / f"init_{date_f}_DEBUG.zarr"
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

    input_state = postprocess_ens(get_ic(date))
    n_workers = min(ngpus, n_members)
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
            filename,
            final_filename,
            n_members,
            n_workers,
            date,
        ),
        name="aifs-ens-writer",
    )
    writer.start()

    workers = []
    for worker_id in range(n_workers):
        members = list(range(worker_id, n_members, n_workers))
        gpu_id = worker_id
        logging.info(f"Assigning members {members} to worker {worker_id} GPU {gpu_id}")
        worker = ctx.Process(
            target=worker_loop,
            args=(
                worker_id,
                gpu_id,
                members,
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
    args = parser.parse_args()
    date_f = args.date
    output_dir = BASE / "raw" / "output" / "AIFS_ENS"
    output_dir.mkdir(parents=True, exist_ok=True)

    cpkt_path = BASE / "weights" / "aifs-ens-crps-1.0.ckpt"
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

    final_filename = run_model(
        output_dir, n_members, date_f, lead_time, save_fields, cpkt_path, ngpus
    )
    logging.info(f"Model run complete. Final output saved to {final_filename}")
    logging.info("Exiting inference script")


if __name__ == "__main__":
    main()
