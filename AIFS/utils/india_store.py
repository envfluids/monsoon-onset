"""Build and validate the daily India rainfall store that DryWetCast-India reads.

The store follows DryWetCast's `--aifs-zarr` contract (pipeline/aifs_features.py
`_daily_from_zarr` at commit 56f4805):

  init_<YYYYMMDD>T00.zarr, zarr format 2
  tp(time, number, prediction_timedelta, lat, lon), float32
    metres, 6-hourly amounts (not cumulative), unmasked
    lat 6.5..38.5 (129, ascending), lon 66.5..100 (135, ascending), 0.25 deg
  prediction_timedelta: integer lead hours, every 6 h from +6 to the run's length (>= +126)
  time: "days since YYYY-MM-DD 00:00:00"

The store is written under a partial name, validated with `validate_store`, and
only then renamed to its final name.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numcodecs
import numpy as np
import pandas as pd
import xarray as xr
import zarr

logger = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE.parent
MODEL_CONFIG_PATH = REPO_ROOT / "config" / "models.json"

# The exact DryWetCast target grid (pipeline/grid.py TARGET_LATS / TARGET_LONS).
INDIA_LATS = np.arange(6.5, 38.51, 0.25)  # 129 points
INDIA_LONS = np.arange(66.5, 100.01, 0.25)  # 135 points

DIMS = ("time", "number", "prediction_timedelta", "lat", "lon")
STEP_HOURS = 6
MIN_LEAD_HOURS = 126  # D5 of the 03Z-03Z days needs +126 h
MIN_MEMBERS = 20

# Cumulative check: among lead-to-lead changes larger than this (metres), a
# cumulative field has almost no decreases; 6-hourly amounts have many (~50%).
SIGNIFICANT_CHANGE_M = 1e-6
MIN_DECREASING_FRACTION = 0.05
MIN_SIGNIFICANT_CHANGES = 1000

COMPRESSOR = numcodecs.Blosc(cname="zstd", clevel=3, shuffle=numcodecs.Blosc.BITSHUFFLE)


class StoreValidationError(ValueError):
    pass


def store_name(init):
    return f"init_{pd.Timestamp(init):%Y%m%d}T00.zarr"


def _lead_hours(coord):
    values = coord.values
    if np.issubdtype(values.dtype, np.timedelta64):
        hours = values / np.timedelta64(1, "h")
    else:
        hours = values.astype(float)
    if not np.allclose(hours, np.round(hours)):
        raise ValueError(f"prediction_timedelta is not in whole hours: {hours[:5]}...")
    return np.round(hours).astype(np.int64)


def _indices(source, target, name):
    lookup = {round(float(v), 4): i for i, v in enumerate(source)}
    missing = [float(v) for v in target if round(float(v), 4) not in lookup]
    if missing:
        raise ValueError(
            f"Source {name} lacks {len(missing)} India grid points (first: {missing[:3]})"
        )
    return [lookup[round(float(v), 4)] for v in target]


def build_india_store(ds, init, attrs=None):
    """Return the India store Dataset for one 00 UTC run.

    `ds` holds `tp(time, number, prediction_timedelta, lat, lon)` in metres per
    6 h step, on our global 0.25 deg grid or an already-cut India box. All leads
    from +6 h to the run's length are kept.
    """
    init = pd.Timestamp(init)
    if init.hour != 0 or init.minute != 0:
        raise ValueError(f"The India store is for 00 UTC runs only, got {init}")

    tp = ds["tp"]
    if set(tp.dims) != set(DIMS):
        raise ValueError(f"tp dims {tp.dims} are not {DIMS}")

    if "time" in tp.coords:
        store_init = pd.Timestamp(tp["time"].values[0])
        if tp.sizes["time"] != 1 or store_init != init:
            raise ValueError(
                f"Source time {tp['time'].values} is not the single init {init}"
            )

    hours = _lead_hours(tp["prediction_timedelta"])
    keep = hours >= STEP_HOURS
    tp = tp.isel(prediction_timedelta=np.flatnonzero(keep))
    hours = hours[keep]
    if len(hours) == 0 or hours.max() < MIN_LEAD_HOURS:
        last = int(hours.max()) if len(hours) else 0
        raise ValueError(
            f"The run reaches +{last} h; the India store needs at least +{MIN_LEAD_HOURS} h"
        )
    expected = np.arange(STEP_HOURS, hours.max() + 1, STEP_HOURS)
    if not np.array_equal(hours, expected):
        raise ValueError(
            f"Lead hours are not every {STEP_HOURS} h from +{STEP_HOURS} h: {hours}"
        )

    tp = tp.isel(
        lat=_indices(tp["lat"].values, INDIA_LATS, "lat"),
        lon=_indices(tp["lon"].values, INDIA_LONS, "lon"),
    )
    values = np.asarray(tp.transpose(*DIMS).values, dtype=np.float32)

    store = xr.Dataset(
        {"tp": (DIMS, values)},
        coords={
            "time": [init.to_datetime64()],
            "number": tp["number"].values.astype(np.int64),
            "prediction_timedelta": (hours * np.timedelta64(1, "h")).astype(
                "timedelta64[ns]"
            ),
            "lat": INDIA_LATS,
            "lon": INDIA_LONS,
        },
        attrs={
            **(attrs or {}),
            "members": int(values.shape[1]),
            "lead_hours": f"+{STEP_HOURS}..+{int(hours.max())}",
        },
    )
    store["tp"].attrs = {
        "units": "m",
        "long_name": "Total precipitation over the preceding 6 hours",
        "accumulation": "6-hourly amounts, not cumulative",
    }
    store["lat"].attrs = {"units": "degrees_north"}
    store["lon"].attrs = {"units": "degrees_east"}
    return store


def write_store(store, path):
    init = pd.Timestamp(store["time"].values[0])
    n_leads = store.sizes["prediction_timedelta"]
    encoding = {
        "tp": {
            "chunks": (1, 1, n_leads, len(INDIA_LATS), len(INDIA_LONS)),
            "compressors": (COMPRESSOR,),
        },
        "time": {"units": f"days since {init:%Y-%m-%d} 00:00:00", "dtype": "int64"},
        "prediction_timedelta": {"units": "hours", "dtype": "int64"},
    }
    # write_empty_chunks: zarr 3 otherwise skips a chunk equal to the implied fill
    # value (e.g. time = 0), and zarr 2 readers return uninitialised memory for it.
    store.to_zarr(
        path,
        mode="w",
        zarr_format=2,
        encoding=encoding,
        consolidated=True,
        write_empty_chunks=True,
    )


def _dimension_names(array):
    if array.metadata.zarr_format == 2:
        return tuple(array.attrs.get("_ARRAY_DIMENSIONS", ()))
    return tuple(array.metadata.dimension_names or ())


def cumulative_problem(tp):
    """Return a reason if `tp` (members, leads, lat, lon) looks cumulative, else None."""
    changes = np.diff(tp, axis=1)
    significant = changes[np.abs(changes) > SIGNIFICANT_CHANGE_M]
    if significant.size < MIN_SIGNIFICANT_CHANGES:
        # Too little rain to judge (e.g. a very dry spell); don't block the store.
        logger.warning(
            "Cumulative check skipped: only %d lead-to-lead changes above %s m (need %d)",
            significant.size,
            SIGNIFICANT_CHANGE_M,
            MIN_SIGNIFICANT_CHANGES,
        )
        return None
    decreasing = float((significant < 0).mean())
    if decreasing < MIN_DECREASING_FRACTION:
        return (
            f"only {decreasing:.1%} of lead-to-lead changes are decreases "
            f"(need >= {MIN_DECREASING_FRACTION:.0%}); tp looks cumulative"
        )
    return None


def validate_store(path, init, min_members=MIN_MEMBERS):
    """Check a written store against DryWetCast's --aifs-zarr contract.

    Raises StoreValidationError listing every failed check.
    """
    init = pd.Timestamp(init)
    path = Path(path)
    errors = []

    if not path.name.endswith(".zarr") or not path.is_dir():
        raise StoreValidationError(f"{path} is not a .zarr directory")
    group = zarr.open_group(str(path), mode="r")
    if group.metadata.zarr_format != 2:
        errors.append(f"zarr format is {group.metadata.zarr_format}, expected 2")

    required = ("tp", "time", "number", "prediction_timedelta", "lat", "lon")
    missing = [key for key in required if key not in group]
    if missing:
        raise StoreValidationError(f"{path}: missing {missing}")

    # zarr 2 readers don't fill missing chunks reliably, so every chunk must exist.
    for name, array in group.arrays():
        if array.nchunks_initialized != array.nchunks:
            errors.append(
                f"{name} has {array.nchunks_initialized} of {array.nchunks} chunks on disk"
            )

    tp = group["tp"]
    if _dimension_names(tp) != DIMS:
        errors.append(f"tp dims are {_dimension_names(tp)}, expected {DIMS}")
    if (
        len(tp.shape) != 5
        or tp.shape[0] != 1
        or tp.shape[3:] != (len(INDIA_LATS), len(INDIA_LONS))
    ):
        errors.append(f"tp shape is {tp.shape}, expected (1, members, leads, 129, 135)")
    if tp.attrs.get("units") != "m":
        errors.append(f"tp units are {tp.attrs.get('units')!r}, expected 'm'")

    lat, lon = group["lat"][:], group["lon"][:]
    lat_ok = len(lat) == len(INDIA_LATS) and (
        np.allclose(lat, INDIA_LATS) or np.allclose(lat[::-1], INDIA_LATS)
    )
    if not lat_ok:
        errors.append(f"lat is not the 129-point India grid ({lat[:1]}..{lat[-1:]})")
    if not (len(lon) == len(INDIA_LONS) and np.allclose(lon, INDIA_LONS)):
        errors.append(
            f"lon is not the 135-point ascending India grid ({lon[:1]}..{lon[-1:]})"
        )

    leads = group["prediction_timedelta"]
    lead_values = leads[:]
    if not np.issubdtype(lead_values.dtype, np.integer):
        errors.append(
            f"prediction_timedelta is stored as {lead_values.dtype}, expected integer hours"
        )
    if leads.attrs.get("units") != "hours":
        errors.append(
            f"prediction_timedelta units are {leads.attrs.get('units')!r}, expected 'hours'"
        )
    expected_leads = np.arange(
        STEP_HOURS, max(int(lead_values.max()), 0) + 1, STEP_HOURS
    )
    if not np.array_equal(lead_values, expected_leads):
        errors.append(f"lead hours are not every {STEP_HOURS} h from +{STEP_HOURS} h")
    if int(lead_values.max()) < MIN_LEAD_HOURS:
        errors.append(
            f"last lead is +{int(lead_values.max())} h, need at least +{MIN_LEAD_HOURS} h"
        )

    time = group["time"]
    units = time.attrs.get("units", "")
    if not units.startswith("days since "):
        errors.append(f"time units are {units!r}, expected 'days since ...'")
    else:
        base = pd.Timestamp(units[len("days since ") :][:10])
        store_init = base + pd.Timedelta(days=float(time[0]))
        if store_init != init:
            errors.append(f"store init is {store_init}, expected {init}")

    n_members = len(group["number"][:])
    if n_members < min_members:
        errors.append(f"{n_members} members, need at least {min_members}")
    if len(tp.shape) == 5 and tp.shape[1] != n_members:
        errors.append(f"tp has {tp.shape[1]} members but number has {n_members}")

    if not errors:
        values = np.asarray(tp[0], dtype=np.float32)
        if not np.isfinite(values).all():
            errors.append("tp has NaN or infinite values; the store must be unmasked")
        elif values.min() < 0:
            errors.append(f"tp has negative values (min {values.min():.3g} m)")
        else:
            problem = cumulative_problem(values)
            if problem:
                errors.append(problem)

    if errors:
        raise StoreValidationError(f"{path} failed validation: " + "; ".join(errors))
    logger.info(
        "Validated %s: %d members, leads +6..+%d h",
        path,
        n_members,
        int(lead_values.max()),
    )


def store_attrs(model, model_config):
    streams = ", ".join(model_config.get("ic_streams", []))
    return {
        "title": "AIFS ensemble 6-hourly rainfall, India box, for DryWetCast-India",
        "model_version": f"{model} ({model_config['weights']})",
        "ic_source": f"{model_config['ic_source']} ({streams})"
        if streams
        else model_config["ic_source"],
        "code_version": code_version(),
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def code_version():
    version = os.environ.get("AIFS_CODE_VERSION", "").strip()
    if version:
        return version
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _open_source(path):
    path = Path(path)
    if path.suffix == ".zarr":
        return xr.open_zarr(path, chunks={})
    return xr.open_dataset(path)


def produce_india_store(source, output_dir, init, model, model_config):
    """Build, write, validate and finalise the store. Returns the final path."""
    init = pd.Timestamp(init)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = output_dir / store_name(init)
    partial_path = output_dir / store_name(init).replace(".zarr", "_partial.zarr")
    if partial_path.exists():
        shutil.rmtree(partial_path)

    with _open_source(source) as ds:
        store = build_india_store(ds, init, store_attrs(model, model_config))
    logger.info("Writing %s", partial_path)
    write_store(store, partial_path)
    validate_store(partial_path, init)

    if final_path.exists():
        shutil.rmtree(final_path)
    partial_path.rename(final_path)
    logger.info("India store ready: %s", final_path)
    return final_path


def main():
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
        ),
    )
    parser = argparse.ArgumentParser(
        description="Build the DryWetCast India store from an AIFS ensemble run"
    )
    parser.add_argument(
        "--date", required=True, help="Init in YYYYMMDDTHH format (00 UTC only)"
    )
    parser.add_argument(
        "--model", required=True, help="Model name in config/models.json"
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Default: AIFS/output/raw/<model>/init_<date>.zarr",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Default: AIFS/output/india/<model>/drywetcast",
    )
    args = parser.parse_args()

    with open(MODEL_CONFIG_PATH, encoding="utf-8") as f:
        model_config = json.load(f)[args.model]
    init = pd.to_datetime(args.date, format="%Y%m%dT%H")
    source = (
        args.source or BASE / "output" / "raw" / args.model / f"init_{args.date}.zarr"
    )
    output_dir = (
        args.output_dir or BASE / "output" / "india" / args.model / "drywetcast"
    )
    produce_india_store(source, output_dir, init, args.model, model_config)


if __name__ == "__main__":
    main()
