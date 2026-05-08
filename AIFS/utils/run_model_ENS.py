from scipy.sparse import load_npz
import numpy as np
import pandas as pd
import xarray as xr
import os
import pickle
import torch

from anemoi.inference.runners.simple import SimpleRunner
from anemoi.inference.outputs.printer import print_state

import gc

import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

TFM_N320_LATLON = load_npz(
    "../EKR/mir_16_linear/7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz"
)

latitudes = np.linspace(90, -90, 721)
longitudes = np.linspace(0, 359.75, 1440)


def get_state(date_f):
    logging.info(f"Reading input state for date: {date_f}")
    with open(f"../raw/ifs_ic/input_state_{date_f}.pkl", "rb") as f:
        data = pickle.load(f)
        vars_to_remove = ["swvl1", "swvl2"]
        for key in vars_to_remove:
            if key in data["fields"]:
                logging.info(f"Removing variable {key} from input state")
                data["fields"].pop(key)
        return data

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


def run_model_rng(output_dir, n_members, date_f, lead_time, save_vars, cpkt_path):
    date = pd.to_datetime(date_f, format="%Y%m%dT%H")
    filename = os.path.join(output_dir, f"init_{date_f}.zarr")

    input_state = get_state(date_f)

    for ens_number in range(n_members):
        torch.manual_seed(ens_number)
        runner = SimpleRunner(cpkt_path, device="cuda")
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
            print(f"Completed forecast for step {runcount}, results in memory")
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

        if ens_number == 0:
            if os.path.exists(filename):
                raise FileExistsError(
                    f"File {filename} already exists. Please remove it before running."
                )
            logging.info(
                f"Saving forecast for ensemble {ens_number} and date {date} to {filename} (mode=w)"
            )
            ds.to_zarr(filename, zarr_format=2, mode="w")
        else:
            logging.info(
                f"Saving forecast for ensemble {ens_number} and date {date} to {filename} (mode=a, append)"
            )
            ds.to_zarr(filename, zarr_format=2, mode="a", append_dim="number")

        del runner
        del ds
        gc.collect()


def main():
    parser = argparse.ArgumentParser(
        description="Process weather data for a given year"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Date for the inference in YYYYMMDDHH format",
    )
    args = parser.parse_args()
    date_f = args.date
    output_dir = "../raw/output/AIFS_ENS"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    cpkt_path = "../weights/aifs-ens-crps-1.0.ckpt"
    logging.info(f"Running for: {date_f}")
    logging.info(f"Output directory: {output_dir}")

    save_fields = [
        "2t",
        "u_850",
        "v_850",
        "u_200",
        "v_200",
        "tp",
        "tcw",
        "msl",
    ]
    lead_time = 24 * 46
    n_members = 25

    logging.info("Exiting inference script")

    run_model_rng(output_dir, n_members, date_f, lead_time, save_fields, cpkt_path)


if __name__ == "__main__":
    main()
