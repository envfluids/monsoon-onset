import argparse
import gc
import logging
import shutil
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


def run_model(output_dir, n_members, date_f, lead_time, save_vars, cpkt_path):
    date = pd.to_datetime(date_f, format="%Y%m%dT%H")
    filename = output_dir / f"init_{date_f}_partial.zarr"
    final_filename = output_dir / f"init_{date_f}.zarr"
    if final_filename.exists():
        logging.warning(
            f"Final output file {final_filename} already exists. Skipping model run."
        )
        return final_filename

    input_state = postprocess_ens(get_ic(date))

    for ens_number in range(n_members):
        if ens_number == 0:
            if filename.exists():
                logging.warning(f"Partial output file {filename} already exists.")
                shutil.rmtree(filename)
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
    filename.rename(final_filename)
    return final_filename


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

    final_filename = run_model(
        output_dir, n_members, date_f, lead_time, save_fields, cpkt_path
    )
    logging.info(f"Model run complete. Final output saved to {final_filename}")
    logging.info("Exiting inference script")


if __name__ == "__main__":
    main()
