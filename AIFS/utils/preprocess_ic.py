import datetime
import logging
from collections import defaultdict
from pathlib import Path

import earthkit.data as ekd
import numpy as np
from scipy.sparse import load_npz

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

BASE = Path(__file__).resolve().parent.parent

TFM_LATLON_N320_NAME = (
    "9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz"
)
TFM_LATLON_N320_PATH = BASE / "EKR" / "mir_16_linear" / TFM_LATLON_N320_NAME
GRIB_OUTPUT_DIR = BASE / "raw" / "ifs_ic" / "grib"


def postprocess_ens(input_state):
    logging.info("Postprocessing for AIFS ENS")
    vars_to_remove = ["swvl1", "swvl2"]
    for key in vars_to_remove:
        if key in input_state["fields"]:
            logging.info(f"Removing variable {key} from input state")
            input_state["fields"].pop(key)
    return input_state


def get_grib_path(date):
    date_f = date.strftime("%Y%m%d%H%M%S")

    grib_path = GRIB_OUTPUT_DIR / f"{date_f}-0h-oper-fc.grib2"
    if not grib_path.exists():
        logging.error(f"GRIB file {grib_path} does not exist.")
        raise FileNotFoundError(f"GRIB file {grib_path} not found.")

    return str(grib_path)


def get_ic(date):
    TFM_LATLON_N320 = load_npz(TFM_LATLON_N320_PATH)

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

    def get_open_data(param, target_date, levelist=None):
        fields = defaultdict(list)
        # AIFS usually needs T and T-6h
        for d in [target_date - datetime.timedelta(hours=6), target_date]:
            f_path = get_grib_path(d)

            source = ekd.from_source("file", f_path)
            if levelist:
                data = source.sel(param=param, level=levelist)
            else:
                if param == PARAM_SFC:
                    data = source.sel(param=param, levtype="sfc")
                else:
                    data = source.sel(param=param)

            for f in data:
                # Shift -180/180 to 0/360
                assert f.to_numpy().shape == (721, 1440)
                values = np.roll(f.to_numpy(), -f.shape[1] // 2, axis=1)
                # Interpolate to N320
                values = TFM_LATLON_N320 * values.flatten()

                name = (
                    f"{f.metadata('param')}_{f.metadata('levelist')}"
                    if levelist
                    else f.metadata("param")
                )
                fields[name].append(values)

        for p, v_list in fields.items():
            fields[p] = np.stack(v_list)
        return fields

    fields = {}

    # 1. Surface Data
    fields.update(get_open_data(PARAM_SFC, date))

    # 3. Soil Data
    soil = get_open_data(PARAM_SOIL, date, levelist=SOIL_LEVELS)
    mapping = {"sot_1": "stl1", "sot_2": "stl2", "vsw_1": "swvl1", "vsw_2": "swvl2"}
    for k, v in soil.items():
        if k in mapping:
            fields[mapping[k]] = v

    # 4. Pressure Levels
    fields.update(get_open_data(PARAM_PL, date, levelist=LEVELS))

    # Transform GH to Z
    for level in LEVELS:
        key = f"gh_{level}"
        if key in fields:
            gh = fields.pop(key)
            fields[f"z_{level}"] = gh * 9.80665

    return dict(date=date, fields=fields)
