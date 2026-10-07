import importlib.util
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_ECMWF_PATH = REPO_ROOT / "IC/utils/download_ecmwf.py"
WRAPPER_PATH = REPO_ROOT / "docker/downloader/src/main.py"
MODEL_CONFIG_PATH = REPO_ROOT / "config/models.json"

DATE = "20261007T00"
ENS_ONLY = json.dumps({"ethiopia": [], "india": ["AIFS_ENS_v2"]})
ENS_AND_GENCAST = json.dumps({"ethiopia": ["gencast"], "india": ["AIFS_ENS_v2"]})
ENS_GRIBS = [
    "20261007000000-0h-oper-fc.grib2",
    "20261006180000-0h-oper-fc.grib2",
    "20261007000000-0h-wave-fc.grib2",
    "20261006180000-0h-wave-fc.grib2",
]
GENCAST_12Z = "20261006120000-0h-oper-fc.grib2"
SST_BLOB = f"ic/gencast_sst/{DATE}/sst_{DATE}.nc"


def load_download_ecmwf():
    """Import IC/utils/download_ecmwf.py with its ECMWF/earthkit/tqdm imports stubbed."""
    opendata = types.ModuleType("ecmwf.opendata")
    opendata.Client = object
    ecmwf = types.ModuleType("ecmwf")
    ecmwf.opendata = opendata
    earthkit_data = types.ModuleType("earthkit.data")
    earthkit = types.ModuleType("earthkit")
    earthkit.data = earthkit_data
    tqdm = types.ModuleType("tqdm")
    tqdm.tqdm = object
    stubs = {
        "ecmwf": ecmwf,
        "ecmwf.opendata": opendata,
        "earthkit": earthkit,
        "earthkit.data": earthkit_data,
        "tqdm": tqdm,
    }
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "download_ecmwf_under_test", DOWNLOAD_ECMWF_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def load_downloader_wrapper():
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

    module_name = "downloader_wrapper_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, WRAPPER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class DownloadEcmwfTest(unittest.TestCase):
    """The science script: HPC behaviour by default, filtered when models are given."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_download_ecmwf()

    def run_get_data(self, sst_error=None, **kwargs):
        requested = []

        def download_file(urls):
            requested.append(urls[0].rsplit("/", 1)[-1])
            return Path(requested[-1])

        with (
            mock.patch.object(self.module, "download_file", side_effect=download_file),
            mock.patch.object(self.module, "get_sst", side_effect=sst_error) as get_sst,
        ):
            result = self.module.get_data(DATE, **kwargs)
        return result, requested, get_sst

    def test_ens_only_streams_have_no_12z_and_no_mars(self):
        streams, download_mars = self.module.get_streams_deltas({"AIFS_ENS_v2"})
        self.assertEqual(streams, {"oper": {0, 6}, "wave": {0, 6}})
        self.assertFalse(download_mars)

    def test_model_names_are_case_insensitive(self):
        self.assertEqual(
            self.module.get_streams_deltas({"aifs_ens_v2"}),
            self.module.get_streams_deltas({"AIFS_ENS_v2"}),
        )

    def test_no_enabled_models_matches_today(self):
        streams, download_mars = self.module.get_streams_deltas()
        self.assertEqual(streams, {"oper": {0, 6, 12}, "wave": {0, 6}})
        self.assertTrue(download_mars)

    def test_get_data_for_ens_only_requests_no_12z_file_and_no_sst(self):
        result, requested, get_sst = self.run_get_data(enabled_models={"AIFS_ENS_v2"})
        self.assertEqual(result, DATE)
        self.assertEqual(sorted(requested), sorted(ENS_GRIBS))
        self.assertNotIn(GENCAST_12Z, requested)
        get_sst.assert_not_called()

    def test_get_data_without_model_info_requests_12z_and_sst_as_today(self):
        _, requested, get_sst = self.run_get_data()
        self.assertIn(GENCAST_12Z, requested)
        get_sst.assert_called_once()

    def test_sst_failure_is_fatal_by_default(self):
        with self.assertRaisesRegex(RuntimeError, "Access token expired"):
            self.run_get_data(
                sst_error=RuntimeError("ecmwf.API error 1: Access token expired")
            )

    def test_sst_failure_is_logged_and_skipped_when_not_fatal(self):
        with self.assertLogs(level="ERROR") as logs:
            result, requested, _ = self.run_get_data(
                sst_error=RuntimeError("ecmwf.API error 1: Access token expired"),
                enabled_models={"AIFS_ENS_v2", "gencast"},
                sst_fatal=False,
            )
        self.assertEqual(result, DATE)
        self.assertIn(GENCAST_12Z, requested)
        self.assertIn("GenCast SST (MARS) download failed", logs.output[0])


class DownloaderWrapperTest(unittest.TestCase):
    """The cloud wrapper, driven through its click command."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_download(self, env, existing=(), sst_error=None):
        module = load_downloader_wrapper()
        module.MODEL_CONFIG_PATH = MODEL_CONFIG_PATH
        module.IC_UTILS = self.tmp
        module.IC_ECMWF_DIR = self.tmp / "ecmwf"
        events = []

        def get_data(date, enabled_models=None, sst_fatal=True):
            enabled = None if enabled_models is None else sorted(enabled_models)
            events.append(("get_data", enabled, sst_fatal))
            module.IC_ECMWF_DIR.mkdir(parents=True, exist_ok=True)
            for name in module._expected_ecmwf_grib_names(date, enabled_models):
                (module.IC_ECMWF_DIR / name).touch()
            return date

        def gencast_sst(bucket, date_str):
            events.append(("sst", date_str))
            if sst_error is not None:
                raise sst_error

        fake_download_ecmwf = types.ModuleType("download_ecmwf")
        fake_download_ecmwf.get_data = get_data
        full_env = {"REGION_MODELS": None, "REGIONS": None, **env}
        with (
            mock.patch.dict(sys.modules, {"download_ecmwf": fake_download_ecmwf}),
            mock.patch.object(module.os, "chdir"),
            mock.patch.object(
                module, "blob_exists", side_effect=lambda b, p: p in existing
            ),
            mock.patch.object(
                module,
                "upload_file",
                side_effect=lambda b, local, gcs: events.append(
                    ("upload", gcs.rsplit("/", 1)[-1])
                ),
            ),
            mock.patch.object(
                module,
                "write_gcs_text",
                side_effect=lambda b, path, content: events.append(
                    ("write", path, content)
                ),
            ),
            mock.patch.object(
                module, "_download_and_upload_gencast_sst", side_effect=gencast_sst
            ),
        ):
            result = CliRunner().invoke(
                module.main,
                ["--source", "ecmwf", "--date", DATE, "--bucket", "common"],
                env=full_env,
            )
        return result, events

    @staticmethod
    def uploads(events):
        return [event[1] for event in events if event[0] == "upload"]

    def test_ens_only_uploads_four_gribs_without_12z_or_sst(self):
        result, events = self.run_download({"REGION_MODELS": ENS_ONLY})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(events[0], ("get_data", ["AIFS_ENS_v2"], False))
        self.assertEqual(sorted(self.uploads(events)), sorted(ENS_GRIBS))
        self.assertNotIn(GENCAST_12Z, self.uploads(events))
        self.assertFalse([e for e in events if e[0] == "sst"])
        self.assertEqual(
            events[-1], ("write", "intermediate/latest_ecmwf_date.txt", DATE)
        )

    def test_skip_check_does_not_require_sst_when_not_needed(self):
        existing = {f"ic/ecmwf/{DATE}/grib/{name}" for name in ENS_GRIBS}
        result, events = self.run_download(
            {"REGION_MODELS": ENS_ONLY}, existing=existing
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            events, [("write", "intermediate/latest_ecmwf_date.txt", DATE)]
        )

    def test_sst_failure_uploads_gribs_and_latest_date_before_nonzero_exit(self):
        result, events = self.run_download(
            {"REGION_MODELS": ENS_AND_GENCAST},
            sst_error=RuntimeError("ecmwf.API error 1: Access token expired"),
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("GenCast SST download failed for 20261007T00", result.output)
        self.assertIn("Access token expired", result.output)
        self.assertEqual(events[0], ("get_data", ["AIFS_ENS_v2", "gencast"], False))
        kinds = [event[0] for event in events]
        self.assertEqual(kinds[-2:], ["sst", "write"])
        self.assertEqual(
            sorted(self.uploads(events)), sorted([*ENS_GRIBS, GENCAST_12Z])
        )
        # every GRIB upload and the latest-date marker happen before the job fails
        self.assertLess(
            max(i for i, k in enumerate(kinds) if k == "upload"), kinds.index("sst")
        )
        self.assertEqual(
            events[-1], ("write", "intermediate/latest_ecmwf_date.txt", DATE)
        )

    def test_gencast_enabled_with_sst_success_exits_zero(self):
        result, events = self.run_download({"REGION_MODELS": ENS_AND_GENCAST})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(("sst", DATE), events)

    def test_no_model_information_matches_today(self):
        result, events = self.run_download({})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(events[0], ("get_data", None, False))
        self.assertEqual(
            sorted(self.uploads(events)), sorted([*ENS_GRIBS, GENCAST_12Z])
        )
        self.assertIn(("sst", DATE), events)

    def test_no_model_information_skip_check_still_requires_sst(self):
        existing = {
            f"ic/ecmwf/{DATE}/grib/{name}" for name in [*ENS_GRIBS, GENCAST_12Z]
        }
        result, events = self.run_download({}, existing=existing)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(events[0][0], "get_data")
        self.assertIn(("sst", DATE), events)

    def test_unparsable_region_models_falls_back_to_today(self):
        module = load_downloader_wrapper()
        with (
            mock.patch.dict("os.environ", {"REGION_MODELS": "not json"}),
            self.assertLogs(module.logger, level="WARNING") as logs,
        ):
            self.assertIsNone(module._enabled_models())
        self.assertIn("Could not parse REGION_MODELS", logs.output[0])


class EnabledModelsParsingTest(unittest.TestCase):
    def setUp(self):
        self.module = load_downloader_wrapper()
        self.module.MODEL_CONFIG_PATH = MODEL_CONFIG_PATH

    def enabled(self, env):
        clean = {"REGION_MODELS": "", "REGIONS": "", **env}
        with mock.patch.dict("os.environ", clean):
            return self.module._enabled_models()

    def test_region_models_union(self):
        env = {
            "REGION_MODELS": json.dumps(
                {"a": ["AIFS_ENS_v2"], "b": ["gencast", "AIFS_ENS_v2"]}
            )
        }
        self.assertEqual(self.enabled(env), {"AIFS_ENS_v2", "gencast"})

    def test_regions_fallback(self):
        regions = {
            "india": {"models": ["AIFS_ENS_v2"], "stages": ["sync"]},
            "ethiopia": {"models": []},
        }
        self.assertEqual(
            self.enabled({"REGIONS": json.dumps(regions)}), {"AIFS_ENS_v2"}
        )

    def test_unset_means_no_information(self):
        self.assertIsNone(self.enabled({}))

    def test_all_regions_empty_means_no_ecmwf_inputs(self):
        enabled = self.enabled(
            {"REGION_MODELS": json.dumps({"india": [], "ethiopia": []})}
        )
        self.assertEqual(enabled, set())
        self.assertEqual(self.module._ecmwf_stream_deltas(enabled), {})
        self.assertFalse(self.module._needs_sst(enabled))

    def test_needs_sst_only_for_models_with_mars_params(self):
        self.assertFalse(self.module._needs_sst({"AIFS_ENS_v2", "neuralgcm"}))
        self.assertTrue(self.module._needs_sst({"GENCAST"}))
        self.assertTrue(self.module._needs_sst(None))


if __name__ == "__main__":
    unittest.main()
