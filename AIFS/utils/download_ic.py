import earthkit.data as ekd
import datetime
from collections import defaultdict
from scipy.sparse import load_npz
import numpy as np
from ecmwf.opendata import Client as OpendataClient
import pickle
import os
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def check_new_data():
    now_utc = datetime.datetime.utcnow()
    logging.info(f"Current UTC time: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")

    DATE = OpendataClient().latest()
    # DATE = datetime.datetime(2026, 3, 21, 12)
    DATE_FORMATTED = DATE.strftime("%Y%m%dT%H")

    logging.info(f"Latest available date: {DATE.strftime('%Y-%m-%d %H:%M:%S')}")

    OUTPUT_DIR = "../raw/ifs_ic"
    # Create the output directory if it doesn't exist

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    OUTPUT_FILE = os.path.join(OUTPUT_DIR, f"input_state_{DATE_FORMATTED}.pkl")
    # Check if the file already exists
    if os.path.exists(OUTPUT_FILE):
        logging.info(f"File {OUTPUT_FILE} already exists. Exiting.")
        return None, None
    else:
        return DATE, OUTPUT_FILE


def save_data(DATE, OUTPUT_FILE):
    TFM_LATLON_N320 = load_npz(
        "../EKR/mir_16_linear/9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz"
    )

    PARAM_SFC = [
        "10u",
        "10v",
        "2d",
        "2t",
        "msl",
        "skt",
        "sp",
        "tcw",
        "lsm",
        "z",
        "slor",
        "sdor",
    ]
    PARAM_SOIL = ["vsw", "sot"]
    PARAM_PL = ["gh", "t", "u", "v", "w", "q"]
    LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50]
    SOIL_LEVELS = [1, 2]

    logging.info(f"Downloading data for date: {DATE.strftime('%Y-%m-%d %H:%M:%S')}")

    def get_open_data(param, levelist=[]):
        fields = defaultdict(list)
        # Get the data for the current date and the previous date
        for date in [DATE - datetime.timedelta(hours=6), DATE]:
            data = ekd.from_source(
                "ecmwf-open-data", date=date, param=param, levelist=levelist
            )
            for f in data:
                # Open data is between -180 and 180, we need to shift it to 0-360
                assert f.to_numpy().shape == (721, 1440)
                values = np.roll(f.to_numpy(), -f.shape[1] // 2, axis=1)
                # Interpolate the data to from 0.25 to N320
                values = TFM_LATLON_N320 * values.flatten()
                # Add the values to the list
                name = (
                    f"{f.metadata('param')}_{f.metadata('levelist')}"
                    if levelist
                    else f.metadata("param")
                )
                fields[name].append(values)

        # Create a single matrix for each parameter
        for param, values in fields.items():
            fields[param] = np.stack(values)

        return fields

    fields = {}

    sfc = get_open_data(param=PARAM_SFC)

    fields.update(sfc)
    soil = get_open_data(param=PARAM_SOIL, levelist=SOIL_LEVELS)

    mapping = {"sot_1": "stl1", "sot_2": "stl2", "vsw_1": "swvl1", "vsw_2": "swvl2"}
    for k, v in soil.items():
        fields[mapping[k]] = v

    pl = get_open_data(param=PARAM_PL, levelist=LEVELS)

    fields.update(pl)

    # Transform GH to Z
    for level in LEVELS:
        gh = fields.pop(f"gh_{level}")
        fields[f"z_{level}"] = gh * 9.80665

    input_state = dict(date=DATE, fields=fields)

    with open(OUTPUT_FILE, "wb") as f:
        pickle.dump(input_state, f)


def get_data():
    DATE, OUTPUT_FILE = check_new_data()
    if DATE:
        save_data(DATE, OUTPUT_FILE)
        logging.info(f"Data saved to {OUTPUT_FILE}")
        DATE_FORMATTED = DATE.strftime("%Y%m%dT%H")
        return DATE_FORMATTED
    else:
        logging.info("No new data to download.")
        return None


if __name__ == "__main__":
    get_data()
