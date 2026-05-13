import argparse
import copy
import datetime
import gc
import logging
from pathlib import Path

import numpy as np
import xarray as xr
from anemoi.inference.outputs.printer import print_state
from anemoi.inference.runners.simple import SimpleRunner
from preprocess_ic import get_ic
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


def run(date_f, forecast_hours, output_dir, save_fields=None):
    local_checkpoint_path = "../weights/aifs-single-mse-1.1.ckpt"
    date = datetime.datetime.strptime(date_f, "%Y%m%dT%H")

    filename = output_dir / f"init_{date_f}.nc"
    if filename.exists():
        logging.warning(f"Output file {filename} already exists. Skipping model run.")
        return

    year = date.year
    month = date.month
    day = date.day
    hour = date.hour
    month_str = str(month).zfill(2)
    day_str = str(day).zfill(2)
    hour_str = str(hour).zfill(2)

    # Get the input state
    logging.info(f"Getting input state for date: {date_f}")
    input_state = get_ic(date)

    # Initialize runner on the GPU
    runner = SimpleRunner(local_checkpoint_path, device="cuda")

    # Run forecasts and save each step
    runcount = 6
    datasets = []
    logging.info(f"Running forecast for {forecast_hours} hours")
    for state in runner.run(input_state=input_state, lead_time=forecast_hours):
        print_state(state)

        saved_state = copy.deepcopy(state)
        if save_fields:
            selected_data = {
                "date": saved_state["date"],
                "fields": {
                    key: saved_state["fields"][key]
                    for key in save_fields
                    if key in saved_state["fields"]
                },
                "latitudes": saved_state["latitudes"],
                "longitudes": saved_state["longitudes"],
            }
            datasets.append((selected_data, runcount))
        else:
            datasets.append((saved_state, runcount))
        logging.info(f"Completed forecast for step {runcount}, results in memory")
        runcount += 6

    ds = xr.concat(
        [process_step(output_state) for output_state in datasets], dim="step"
    )
    del datasets
    ds = ds.expand_dims("time")
    ds["time"] = [np.datetime64(f"{year}-{month_str}-{day_str}T{hour_str}:00:00")]
    ds.to_netcdf(filename)
    del runner
    gc.collect()
    logging.info(f"Completed forecast for {output_dir}")


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

    output_dir = BASE / "raw" / "output" / "AIFS"
    output_dir.mkdir(parents=True, exist_ok=True)

    lead_time = 46 * 24
    logging.info(f"Date: {date_f}")
    logging.info(f"Lead time: {lead_time} hours")

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

    logging.info(f"Running for: {date_f}")
    logging.info(f"Output directory: {output_dir}")

    run(date_f, lead_time, output_dir, save_fields)

    logging.info("Exiting inference script")


if __name__ == "__main__":
    main()
