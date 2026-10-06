import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import click
import dask.array as da
import numpy as np
import pandas as pd
import xarray as xr
import zarr

REPO_ROOT = Path(__file__).resolve().parents[1]
AIFS_UTILS = REPO_ROOT / "AIFS" / "utils"
sys.path.insert(0, str(AIFS_UTILS))

import ens_settings
import india_store

DRYWETCAST_ROOT = Path.home() / "code" / "DryWetCast-India"
DRYWETCAST_COMMIT = "56f4805a532718afd61176c85eec75fa8c46b137"
REAL_SOURCE = (
    Path.home()
    / "monsoon-scratch"
    / "data"
    / "tp_6h_india_box_AIFS_ENS_v2_20260715T00.nc"
)
REAL_INIT = pd.Timestamp("2026-07-15T00")
INIT = pd.Timestamp("2026-10-15T00")
ATTRS = {
    "model_version": "AIFS_ENS_v2 (test.ckpt)",
    "ic_source": "ecmwf",
    "code_version": "test",
    "created_utc": "now",
}


def drywetcast_reader():
    """Return DryWetCast's aifs_features module at the pinned commit, or None."""
    if not (DRYWETCAST_ROOT / "pipeline" / "aifs_features.py").exists():
        return None
    try:
        head = subprocess.run(
            ["git", "-C", str(DRYWETCAST_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    if head != DRYWETCAST_COMMIT:
        return None
    if str(DRYWETCAST_ROOT) not in sys.path:
        sys.path.insert(0, str(DRYWETCAST_ROOT))
    from pipeline import aifs_features

    return aifs_features


AIFS_FEATURES = drywetcast_reader()
needs_drywetcast = unittest.skipIf(
    AIFS_FEATURES is None, f"DryWetCast-India not available at {DRYWETCAST_COMMIT[:7]}"
)


def synthetic_source(n_members=20, last_lead=168, lats=None, lons=None, seed=0):
    """6-hourly tp in metres on an India box (lat descending, like our raw output)."""
    lats = india_store.INDIA_LATS[::-1] if lats is None else lats
    lons = india_store.INDIA_LONS if lons is None else lons
    leads = np.arange(6, last_lead + 1, 6)
    rng = np.random.default_rng(seed)
    shape = (1, n_members, len(leads), len(lats), len(lons))
    tp = (rng.gamma(0.3, 0.002, shape) * (rng.random(shape) > 0.5)).astype(np.float32)
    return xr.Dataset(
        {"tp": (india_store.DIMS, tp)},
        coords={
            "time": [INIT.to_datetime64()],
            "number": np.arange(n_members),
            "prediction_timedelta": (leads * np.timedelta64(1, "h")).astype(
                "timedelta64[ns]"
            ),
            "lat": lats,
            "lon": lons,
        },
    )


class EnsSettingsTest(unittest.TestCase):
    def test_defaults_are_unchanged(self):
        self.assertEqual(ens_settings.resolve_run_settings(environ={}), (25, 1200))

    def test_environment_overrides_defaults(self):
        env = {"AIFS_ENS_N_MEMBERS": "51", "AIFS_ENS_LEAD_TIME_HOURS": "168"}
        self.assertEqual(ens_settings.resolve_run_settings(environ=env), (51, 168))

    def test_arguments_override_environment(self):
        env = {"AIFS_ENS_N_MEMBERS": "51", "AIFS_ENS_LEAD_TIME_HOURS": "168"}
        self.assertEqual(
            ens_settings.resolve_run_settings(30, 144, environ=env), (30, 144)
        )

    def test_blank_environment_uses_defaults(self):
        env = {"AIFS_ENS_N_MEMBERS": " ", "AIFS_ENS_LEAD_TIME_HOURS": ""}
        self.assertEqual(ens_settings.resolve_run_settings(environ=env), (25, 1200))

    def test_invalid_values_are_rejected(self):
        for kwargs in (
            {"n_members": 0},
            {"lead_time_hours": 0},
            {"lead_time_hours": 100},
            {"environ": {"AIFS_ENS_N_MEMBERS": "many"}},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ens_settings.resolve_run_settings(**{"environ": {}, **kwargs})

    def test_command_line_arguments(self):
        parser = argparse.ArgumentParser()
        ens_settings.add_run_arguments(parser)
        args = parser.parse_args(["--n-members", "51", "--lead-time-hours", "168"])
        self.assertEqual((args.n_members, args.lead_time_hours), (51, 168))
        args = parser.parse_args([])
        self.assertEqual((args.n_members, args.lead_time_hours), (None, None))

    def test_run_model_ens_uses_settings_instead_of_hard_coded_values(self):
        source = (AIFS_UTILS / "run_model_ENS.py").read_text()
        self.assertNotIn("lead_time = 24 * 50", source)
        self.assertNotIn("n_members = 25", source)
        self.assertIn(
            "resolve_run_settings(args.n_members, args.lead_time_hours)", source
        )


class BuildIndiaStoreTest(unittest.TestCase):
    def test_store_layout(self):
        store = india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        self.assertEqual(store["tp"].dims, india_store.DIMS)
        self.assertEqual(store["tp"].shape, (1, 20, 28, 129, 135))
        self.assertEqual(store["tp"].dtype, np.float32)
        np.testing.assert_array_equal(store["lat"].values, india_store.INDIA_LATS)
        np.testing.assert_array_equal(store["lon"].values, india_store.INDIA_LONS)
        self.assertEqual(store["tp"].attrs["units"], "m")
        self.assertEqual(list(store.data_vars), ["tp"])
        for key in ("model_version", "ic_source", "code_version", "created_utc"):
            self.assertIn(key, store.attrs)
        self.assertEqual(store.attrs["members"], 20)
        self.assertEqual(store.attrs["lead_hours"], "+6..+168")

    def test_keeps_all_leads_up_to_the_run_length(self):
        store = india_store.build_india_store(
            synthetic_source(last_lead=240), INIT, ATTRS
        )
        hours = store["prediction_timedelta"].values / np.timedelta64(1, "h")
        np.testing.assert_array_equal(hours, np.arange(6, 241, 6))

    def test_values_match_source_after_flipping_lat(self):
        source = synthetic_source()
        store = india_store.build_india_store(source, INIT, ATTRS)
        np.testing.assert_array_equal(
            store["tp"].values, source["tp"].values[:, :, :, ::-1, :]
        )

    def test_selects_box_from_global_grid(self):
        lats = np.linspace(90, -90, 721)
        lons = np.linspace(0, 359.75, 1440)
        leads = np.arange(6, 133, 6)
        shape = (1, 2, len(leads), len(lats), len(lons))
        lat_term = xr.DataArray(
            da.from_array(lats, chunks=60), dims="lat", coords={"lat": lats}
        )
        lon_term = xr.DataArray(
            da.from_array(lons, chunks=120), dims="lon", coords={"lon": lons}
        )
        tp = (
            xr.DataArray(
                da.zeros(shape, chunks=(1, 1, len(leads), 60, 120), dtype=np.float32),
                dims=india_store.DIMS,
            )
            + lat_term * 1000
            + lon_term
        )
        source = xr.Dataset(
            {"tp": tp},
            coords={
                "time": [INIT.to_datetime64()],
                "number": [0, 1],
                "prediction_timedelta": (leads * np.timedelta64(1, "h")).astype(
                    "timedelta64[ns]"
                ),
            },
        )
        store = india_store.build_india_store(source, INIT, ATTRS)
        expected = (
            india_store.INDIA_LATS[:, None] * 1000 + india_store.INDIA_LONS[None, :]
        )
        np.testing.assert_allclose(store["tp"].values[0, 1, 5], expected)

    def test_rejects_runs_shorter_than_126_hours(self):
        with self.assertRaisesRegex(ValueError, r"\+120 h"):
            india_store.build_india_store(synthetic_source(last_lead=120), INIT, ATTRS)

    def test_rejects_non_00_utc_init(self):
        with self.assertRaisesRegex(ValueError, "00 UTC"):
            india_store.build_india_store(
                synthetic_source(), pd.Timestamp("2026-10-15T12"), ATTRS
            )

    def test_rejects_wrong_init(self):
        with self.assertRaisesRegex(ValueError, "single init"):
            india_store.build_india_store(
                synthetic_source(), pd.Timestamp("2026-10-16T00"), ATTRS
            )

    def test_rejects_missing_grid_points(self):
        source = synthetic_source(lons=india_store.INDIA_LONS[:-1])
        with self.assertRaisesRegex(ValueError, "lon"):
            india_store.build_india_store(source, INIT, ATTRS)

    def test_rejects_gaps_in_leads(self):
        source = synthetic_source().drop_isel(prediction_timedelta=[3])
        with self.assertRaisesRegex(ValueError, "every 6 h"):
            india_store.build_india_store(source, INIT, ATTRS)


class CumulativeCheckTest(unittest.TestCase):
    def amounts(self, rainy_cells):
        """(members, leads, lat, lon) 6-hourly amounts with rain in only `rainy_cells` cells."""
        tp = np.zeros((2, 28, 129, 135), dtype=np.float32)
        rng = np.random.default_rng(1)
        flat = tp.reshape(2, 28, -1)
        flat[:, :, :rainy_cells] = rng.gamma(0.5, 0.002, (2, 28, rainy_cells)).astype(
            np.float32
        )
        return tp

    def test_six_hourly_amounts_pass(self):
        self.assertIsNone(india_store.cumulative_problem(self.amounts(500)))

    def test_cumulative_values_fail_when_there_are_enough_changes(self):
        tp = np.cumsum(self.amounts(500), axis=1)
        problem = india_store.cumulative_problem(tp)
        self.assertRegex(problem, r"only 0\.0% of lead-to-lead changes are decreases")

    def test_too_few_changes_warns_and_passes(self):
        tp = np.cumsum(self.amounts(5), axis=1)  # cumulative, but only ~270 changes
        with self.assertLogs(india_store.logger, level="WARNING") as logs:
            self.assertIsNone(india_store.cumulative_problem(tp))
        self.assertIn("Cumulative check skipped", logs.output[0])

    def test_all_zero_store_warns_and_passes(self):
        with self.assertLogs(india_store.logger, level="WARNING"):
            self.assertIsNone(
                india_store.cumulative_problem(np.zeros((2, 28, 129, 135), np.float32))
            )

    def test_dry_store_passes_validation_with_a_warning(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        store = india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        store["tp"].values[:] = 0.0
        path = tmp / india_store.store_name(INIT)
        india_store.write_store(store, path)
        with self.assertLogs(india_store.logger, level="WARNING"):
            india_store.validate_store(path, INIT)


class StoreOnDiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, store, name=None):
        path = self.tmp / (name or india_store.store_name(INIT))
        india_store.write_store(store, path)
        return path

    def test_store_name(self):
        self.assertEqual(india_store.store_name(INIT), "init_20261015T00.zarr")

    def test_written_store_is_zarr_v2_with_contract_encoding(self):
        path = self.write(
            india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        )
        group = zarr.open_group(str(path), mode="r")
        self.assertEqual(group.metadata.zarr_format, 2)
        self.assertEqual(group["tp"].attrs["_ARRAY_DIMENSIONS"], list(india_store.DIMS))
        self.assertEqual(group["tp"].shape, (1, 20, 28, 129, 135))
        self.assertEqual(group["tp"].chunks, (1, 1, 28, 129, 135))
        self.assertEqual(group["tp"].metadata.compressor.get_config()["cname"], "zstd")
        leads = group["prediction_timedelta"]
        self.assertTrue(np.issubdtype(leads.dtype, np.integer))
        np.testing.assert_array_equal(leads[:], np.arange(6, 169, 6))
        self.assertEqual(leads.attrs["units"], "hours")
        self.assertTrue(
            group["time"].attrs["units"].startswith("days since 2026-10-15")
        )
        self.assertEqual(int(group["time"][0]), 0)
        self.assertEqual(group.attrs["code_version"], "test")
        india_store.validate_store(path, INIT)

    def test_every_chunk_is_written_including_time_zero(self):
        # zarr 2 readers return garbage for a missing chunk with no fill value;
        # time = 0 equals zarr 3's implied fill and was once skipped.
        path = self.write(
            india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        )
        self.assertTrue((path / "time" / "0").is_file())
        group = zarr.open_group(str(path), mode="r")
        for name, array in group.arrays():
            with self.subTest(name):
                self.assertEqual(array.nchunks_initialized, array.nchunks)

    def test_validation_rejects_missing_chunks(self):
        path = self.write(
            india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        )
        (path / "time" / "0").unlink()
        with self.assertRaisesRegex(
            india_store.StoreValidationError, "time has 0 of 1 chunks"
        ):
            india_store.validate_store(path, INIT)

    def test_validation_rejects_cumulative_tp(self):
        store = india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        store["tp"].values[:] = np.cumsum(store["tp"].values, axis=2)
        path = self.write(store)
        with self.assertRaisesRegex(india_store.StoreValidationError, "cumulative"):
            india_store.validate_store(path, INIT)

    def test_validation_rejects_contract_violations(self):
        cases = {
            "members": (lambda s: s.isel(number=slice(0, 19)), "at least 20"),
            "negative": (
                lambda s: s.assign(tp=s["tp"].where(s["lat"] < 30, -0.001)),
                "negative",
            ),
            "nan": (lambda s: s.assign(tp=s["tp"].where(s["lat"] < 30)), "unmasked"),
            "units": (lambda s: s.assign(tp=s["tp"].assign_attrs(units="mm")), "units"),
            "lon order": (lambda s: s.isel(lon=slice(None, None, -1)), "lon"),
            "dim order": (
                lambda s: s.transpose(
                    "time", "prediction_timedelta", "number", "lat", "lon"
                ),
                "dims",
            ),
        }
        base = india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        for name, (change, message) in cases.items():
            with self.subTest(name):
                path = self.write(
                    change(base.copy(deep=True)), f"{name.replace(' ', '_')}.zarr"
                )
                with self.assertRaisesRegex(india_store.StoreValidationError, message):
                    india_store.validate_store(path, INIT)

    def test_validation_rejects_wrong_date_and_hours_units(self):
        path = self.write(
            india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        )
        with self.assertRaisesRegex(india_store.StoreValidationError, "init"):
            india_store.validate_store(path, pd.Timestamp("2026-10-16T00"))

        store = india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        path = self.tmp / "hours.zarr"
        store.to_zarr(
            path,
            zarr_format=2,
            encoding={"time": {"units": "hours since 2026-10-15 00:00:00"}},
        )
        with self.assertRaisesRegex(india_store.StoreValidationError, "days since"):
            india_store.validate_store(path, INIT)

    def test_validation_rejects_zarr_v3(self):
        store = india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        path = self.tmp / "v3.zarr"
        store.to_zarr(
            path,
            zarr_format=3,
            encoding={
                "time": {"units": "days since 2026-10-15 00:00:00", "dtype": "int64"},
                "prediction_timedelta": {"units": "hours", "dtype": "int64"},
            },
        )
        with self.assertRaisesRegex(
            india_store.StoreValidationError, "zarr format is 3"
        ):
            india_store.validate_store(path, INIT)

    def test_produce_finalises_only_a_valid_store(self):
        source_path = self.tmp / "source.nc"
        synthetic_source().to_netcdf(source_path)
        config = {
            "weights": "test.ckpt",
            "ic_source": "ecmwf",
            "ic_streams": ["oper", "wave"],
        }
        out = self.tmp / "out"
        with mock.patch.dict("os.environ", {"AIFS_CODE_VERSION": "abc1234"}):
            final = india_store.produce_india_store(
                source_path, out, INIT, "AIFS_ENS_v2", config
            )
        self.assertEqual(final, out / "init_20261015T00.zarr")
        self.assertFalse((out / "init_20261015T00_partial.zarr").exists())
        attrs = zarr.open_group(str(final), mode="r").attrs
        self.assertEqual(attrs["model_version"], "AIFS_ENS_v2 (test.ckpt)")
        self.assertEqual(attrs["ic_source"], "ecmwf (oper, wave)")
        self.assertEqual(attrs["code_version"], "abc1234")
        self.assertTrue(attrs["created_utc"].endswith("Z"))

        shutil.rmtree(final)
        too_few = self.tmp / "too_few.nc"
        synthetic_source(n_members=19).to_netcdf(too_few)
        with self.assertRaisesRegex(india_store.StoreValidationError, "at least 20"):
            india_store.produce_india_store(too_few, out, INIT, "AIFS_ENS_v2", config)
        self.assertFalse(final.exists())


@needs_drywetcast
class DryWetCastReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        AIFS_FEATURES._daily_from_zarr.cache_clear()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def daily(self, path, init):
        return AIFS_FEATURES._daily_from_zarr(
            str(path), tuple(AIFS_FEATURES.LEAD_BASES), f"{init:%Y%m%d}"
        )

    def test_reader_accepts_store_and_matches_direct_daily_totals(self):
        source = synthetic_source()
        path = self.tmp / india_store.store_name(INIT)
        india_store.write_store(
            india_store.build_india_store(source, INIT, ATTRS), path
        )
        daily = self.daily(path, INIT)
        self.assertEqual(daily.shape, (20, 5, 129, 135))

        r = source["tp"].values[0, :, :, ::-1, :]  # lat ascending
        index = {h: i for i, h in enumerate(range(6, 169, 6))}
        for d, base in enumerate(AIFS_FEATURES.LEAD_BASES):
            i = [index[base + off] for off in (6, 12, 18, 24, 30)]
            expected = (
                0.5 * r[:, i[0]]
                + r[:, i[1]]
                + r[:, i[2]]
                + r[:, i[3]]
                + 0.5 * r[:, i[4]]
            ) * 1000.0
            np.testing.assert_allclose(daily[:, d], expected, rtol=1e-6)

    def test_reader_alone_does_not_catch_cumulative_tp(self):
        store = india_store.build_india_store(synthetic_source(), INIT, ATTRS)
        store["tp"].values[:] = np.cumsum(store["tp"].values, axis=2)
        path = self.tmp / "cumulative.zarr"
        india_store.write_store(store, path)
        self.daily(path, INIT)  # DryWetCast's reader accepts it ...
        with self.assertRaises(india_store.StoreValidationError):  # ... ours does not
            india_store.validate_store(path, INIT)


@unittest.skipUnless(REAL_SOURCE.exists(), f"{REAL_SOURCE} not available")
class RealDataTest(unittest.TestCase):
    """20260715T00, 25 members, cut to a 7-day (168 h) run."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        with xr.open_dataset(REAL_SOURCE) as ds:
            cls.source = ds.sel(
                prediction_timedelta=slice(None, np.timedelta64(168, "h"))
            ).load()
        cls.path = cls.tmp / india_store.store_name(REAL_INIT)
        india_store.write_store(
            india_store.build_india_store(cls.source, REAL_INIT, ATTRS), cls.path
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_real_store_passes_our_validation(self):
        india_store.validate_store(self.path, REAL_INIT)
        group = zarr.open_group(str(self.path), mode="r")
        self.assertEqual(group["tp"].shape, (1, 25, 28, 129, 135))

    @needs_drywetcast
    def test_real_store_passes_drywetcast_reader(self):
        AIFS_FEATURES._daily_from_zarr.cache_clear()
        daily = AIFS_FEATURES._daily_from_zarr(
            str(self.path), tuple(AIFS_FEATURES.LEAD_BASES), "20260715"
        )
        self.assertEqual(daily.shape, (25, 5, 129, 135))
        r = self.source["tp"].values[0, :, :, ::-1, :]
        d1 = (0.5 * r[:, 0] + r[:, 1] + r[:, 2] + r[:, 3] + 0.5 * r[:, 4]) * 1000.0
        np.testing.assert_allclose(daily[:, 0], np.clip(d1, 0, None), rtol=1e-6)


def load_ens_wrapper():
    google_module = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud_module = sys.modules.setdefault(
        "google.cloud", types.ModuleType("google.cloud")
    )
    storage_module = sys.modules.setdefault(
        "google.cloud.storage", types.ModuleType("google.cloud.storage")
    )
    storage_module.Client = object
    cloud_module.storage = storage_module
    google_module.cloud = cloud_module

    module_name = "aifs_ens_wrapper_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / "docker/aifs-ens-v2/src/main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class EnsWrapperRegionTest(unittest.TestCase):
    BUCKETS = '{"india": "b-india", "ethiopia": "b-eth"}'

    def run_regions(self, regions, calls, fail=(), buckets=BUCKETS):
        """Run the wrapper with cloud steps stubbed; `fail` holds (step, region) pairs that raise."""
        module = load_ens_wrapper()

        def step(name):
            def record(*args):
                region = "india" if name == "india_store" else args[2]
                calls.append((name, region))
                if (name, region) in fail:
                    raise subprocess.CalledProcessError(1, name)

            return record

        patches = {
            "_setup_directories": lambda *a: None,
            "_download_inputs": lambda *a: None,
            "_zarr_mirror_target": lambda *a: None,
            "_run_inference": lambda *a: None,
            "_upload_full_field": lambda *a: None,
            "_run_post_process": step("post_process"),
            "_run_india_store": step("india_store"),
            "_upload_region_outputs": step("upload"),
            "_write_completion_marker": step("marker"),
        }
        with mock.patch.multiple(module, **patches):
            return module.main.main(
                args=[
                    "--date",
                    "20261015T00",
                    "--regions",
                    json.dumps(regions),
                    "--common-bucket",
                    "common",
                    "--region-buckets",
                    buckets,
                    "--upload-full-field",
                    "false",
                ],
                standalone_mode=False,
            )

    def test_india_builds_store_instead_of_post_process(self):
        calls = []
        self.run_regions(["ethiopia", "india"], calls)
        self.assertEqual(
            calls,
            [
                ("post_process", "ethiopia"),
                ("upload", "ethiopia"),
                ("marker", "ethiopia"),
                ("india_store", "india"),
                ("upload", "india"),
                ("marker", "india"),
            ],
        )

    def test_failed_store_means_no_upload_or_marker_for_india(self):
        calls = []
        with self.assertRaisesRegex(click.ClickException, "india"):
            self.run_regions(["india"], calls, fail={("india_store", "india")})
        self.assertEqual(calls, [("india_store", "india")])

    def test_failed_region_does_not_stop_later_regions(self):
        calls = []
        with self.assertRaises(click.ClickException) as raised:
            self.run_regions(
                ["india", "ethiopia"], calls, fail={("india_store", "india")}
            )
        self.assertEqual(
            raised.exception.message, "Region processing failed for: india"
        )
        self.assertEqual(
            calls,
            [
                ("india_store", "india"),
                ("post_process", "ethiopia"),
                ("upload", "ethiopia"),
                ("marker", "ethiopia"),
            ],
        )

    def test_failed_upload_gets_no_marker_and_other_region_finishes(self):
        calls = []
        with self.assertRaisesRegex(click.ClickException, "failed for: ethiopia$"):
            self.run_regions(
                ["ethiopia", "india"], calls, fail={("upload", "ethiopia")}
            )
        self.assertNotIn(("marker", "ethiopia"), calls)
        self.assertEqual(
            calls[-3:],
            [("india_store", "india"), ("upload", "india"), ("marker", "india")],
        )

    def test_all_failed_regions_are_reported(self):
        calls = []
        with self.assertRaisesRegex(
            click.ClickException, "failed for: india, ethiopia$"
        ):
            self.run_regions(
                ["india", "ethiopia"],
                calls,
                fail={("india_store", "india"), ("post_process", "ethiopia")},
            )
        self.assertEqual(
            calls, [("india_store", "india"), ("post_process", "ethiopia")]
        )

    def test_region_without_bucket_fails_alone(self):
        calls = []
        with self.assertRaisesRegex(click.ClickException, "failed for: india$"):
            self.run_regions(
                ["india", "ethiopia"], calls, buckets='{"ethiopia": "b-eth"}'
            )
        self.assertEqual(
            calls,
            [
                ("post_process", "ethiopia"),
                ("upload", "ethiopia"),
                ("marker", "ethiopia"),
            ],
        )

    def test_india_store_command(self):
        module = load_ens_wrapper()
        with mock.patch.object(module.subprocess, "run") as run:
            module._run_india_store("20261015T00", "AIFS_ENS_v2")
        command = run.call_args.args[0]
        self.assertEqual(
            command[1:],
            ["india_store.py", "--date", "20261015T00", "--model", "AIFS_ENS_v2"],
        )
        self.assertEqual(run.call_args.kwargs["cwd"], module.AIFS_UTILS)
        self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
