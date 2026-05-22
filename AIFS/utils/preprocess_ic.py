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
REPO_ROOT = BASE.parent

TFM_LATLON_N320_NAME = (
    "9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz"
)
TFM_LATLON_N320_PATH = BASE / "EKR" / "mir_16_linear" / TFM_LATLON_N320_NAME
IC_DIR = REPO_ROOT / "IC" / "output" / "ecmwf"

LSM_GRIB_PATH = BASE / "data" / "lsm.grib"


def get_grib_path(date, stream="oper"):
    date_f = date.strftime("%Y%m%d%H%M%S")

    grib_path = IC_DIR / f"{date_f}-0h-{stream}-fc.grib2"
    if not grib_path.exists():
        logging.error(f"GRIB file {grib_path} does not exist.")
        raise FileNotFoundError(f"GRIB file {grib_path} not found.")

    return str(grib_path)

def preprocess_v2(fields):
    mwd = fields.pop("mwd")
    mwd_rad = np.deg2rad(mwd)

    fields["cos_mwd"] = np.cos(mwd_rad)
    fields["sin_mwd"] = np.sin(mwd_rad)

    mask = np.equal(ekd.from_source("file", LSM_GRIB_PATH)[0].to_numpy(flatten=True), 0)

    fields["sd"][:, mask] = np.nan
    fields["swvl1"][:, mask] = np.nan
    fields["swvl2"][:,mask] = np.nan

    return fields

def get_ic(date, model_config):
    TFM_LATLON_N320 = load_npz(TFM_LATLON_N320_PATH)

    PARAM_SFC = model_config["PARAM_SFC"]
    PARAM_SOIL = model_config["PARAM_SOIL"]
    PARAM_PL = model_config["PARAM_PL"]
    LEVELS = model_config["LEVELS"]
    SOIL_LEVELS = model_config["SOIL_LEVELS"]
    PARAM_WAVE = model_config["PARAM_WAVE"]

    def get_open_data(param, target_date, levelist=None, stream="oper"):
        fields = defaultdict(list)
        # AIFS usually needs T and T-6h
        for d in [target_date - datetime.timedelta(hours=6), target_date]:
            f_path = get_grib_path(d, stream=stream)

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

    if len(PARAM_WAVE) > 0:
        fields.update(get_open_data(PARAM_WAVE, date, stream="wave"))

    # Transform GH to Z
    for level in LEVELS:
        key = f"gh_{level}"
        if key in fields:
            gh = fields.pop(key)
            fields[f"z_{level}"] = gh * 9.80665

    if model_config["class"] == "v2":
        fields = preprocess_v2(fields)

    if len(model_config["remove_vars"]) > 0:
        for var in model_config["remove_vars"]:
            if var in fields:
                logging.info(f"Removing variable {var} as per model config")
                fields.pop(var)

    return dict(date=date, fields=fields)
