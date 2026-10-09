import importlib.util
import json
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
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
    api_core = sys.modules.setdefault(
        "google.api_core", types.ModuleType("google.api_core")
    )
    exceptions = sys.modules.setdefault(
        "google.api_core.exceptions", types.ModuleType("google.api_core.exceptions")
    )
    for name in ("NotFound", "PreconditionFailed"):
        if not hasattr(exceptions, name):
            setattr(exceptions, name, type(name, (Exception,), {}))
    api_core.exceptions = exceptions
    google_module.api_core = api_core
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

    def run_download(
        self, env, existing=(), sst_error=None, unpublished=(), claim=True
    ):
        module = load_downloader_wrapper()
        module.MODEL_CONFIG_PATH = MODEL_CONFIG_PATH
        module.IC_UTILS = self.tmp
        module.IC_ECMWF_DIR = self.tmp / "ecmwf"
        events = []
        self.claim_events = []

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
            mock.patch.object(
                module, "_unpublished_ecmwf_files", return_value=list(unpublished)
            ),
            mock.patch.object(
                module,
                "_acquire_download_claim",
                side_effect=lambda b, d: (
                    self.claim_events.append(("claim", d)) or claim
                ),
            ),
            mock.patch.object(
                module,
                "_release_download_claim",
                side_effect=lambda b, d: self.claim_events.append(("release", d)),
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


class DownloaderPublicationAndClaimTest(unittest.TestCase):
    """The downloader waits for full publication and claims the date."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.harness = DownloaderWrapperTest()
        self.harness.tmp = self.tmp

    def test_unpublished_files_end_quietly_without_download_or_marker(self):
        result, events = self.harness.run_download(
            {"REGION_MODELS": ENS_ONLY}, unpublished=["20261007000000-0h-wave-fc.grib2"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(events, [])  # no get_data, no upload, no latest marker
        self.assertEqual(self.harness.claim_events, [])  # checked before claiming

    def test_claim_held_elsewhere_exits_without_downloading(self):
        result, events = self.harness.run_download(
            {"REGION_MODELS": ENS_ONLY}, claim=False
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(events, [])
        self.assertEqual(self.harness.claim_events, [("claim", DATE)])

    def test_claim_is_released_after_success_and_after_failure(self):
        result, events = self.harness.run_download({"REGION_MODELS": ENS_ONLY})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self.harness.claim_events, [("claim", DATE), ("release", DATE)]
        )
        self.assertEqual(events[0][0], "get_data")

        result, events = self.harness.run_download(
            {"REGION_MODELS": ENS_AND_GENCAST},
            sst_error=RuntimeError("ecmwf.API error 1: Access token expired"),
        )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(
            self.harness.claim_events, [("claim", DATE), ("release", DATE)]
        )
        self.assertIn(("write", "intermediate/latest_ecmwf_date.txt", DATE), events)

    def test_published_check_accepts_mirror_or_origin_per_file(self):
        module = load_downloader_wrapper()
        module.MODEL_CONFIG_PATH = MODEL_CONFIG_PATH
        mirror_only = {ENS_GRIBS[0], ENS_GRIBS[1]}
        origin_only = {ENS_GRIBS[2]}

        def head(url, timeout, allow_redirects):
            name = url.rsplit("/", 1)[-1]
            ok = (
                url.startswith(module.ECMWF_MIRROR_BASE_URL) and name in mirror_only
            ) or (url.startswith(module.ECMWF_ORIGIN_BASE_URL) and name in origin_only)
            return types.SimpleNamespace(status_code=200 if ok else 404)

        with mock.patch.object(module.requests, "head", side_effect=head):
            missing = module._unpublished_ecmwf_files(DATE, {"AIFS_ENS_v2"})
        self.assertEqual(missing, [ENS_GRIBS[3]])

    def test_published_check_treats_network_errors_as_missing(self):
        module = load_downloader_wrapper()
        module.MODEL_CONFIG_PATH = MODEL_CONFIG_PATH
        with mock.patch.object(
            module.requests, "head", side_effect=module.requests.ConnectionError("boom")
        ):
            missing = module._unpublished_ecmwf_files(DATE, {"AIFS_ENS_v2"})
        self.assertEqual(sorted(missing), sorted(ENS_GRIBS))


class FakeClaimStore:
    """Minimal GCS with generations and if_generation_match for the claim helpers."""

    def __init__(self, module):
        self.module, self.objects, self.counter = module, {}, 0

    def bucket(self, name):
        store = self

        class Bucket:
            def blob(self, path):
                return Blob(path)

            def get_blob(self, path):
                return Blob(path) if path in store.objects else None

        class Blob:
            def __init__(self, path):
                self.path = path

            @property
            def generation(self):
                return (
                    store.objects[self.path][1] if self.path in store.objects else None
                )

            def upload_from_string(
                self, data, content_type=None, if_generation_match=None
            ):
                current = (
                    store.objects[self.path][1] if self.path in store.objects else 0
                )
                if if_generation_match is not None and current != if_generation_match:
                    raise store.module.PreconditionFailed(self.path)
                store.counter += 1
                store.objects[self.path] = (data, store.counter)

            def download_as_text(self):
                return store.objects[self.path][0]

            def delete(self):
                if self.path not in store.objects:
                    raise store.module.NotFound(self.path)
                del store.objects[self.path]

        return Bucket()


class DownloadClaimTest(unittest.TestCase):
    def setUp(self):
        self.module = load_downloader_wrapper()
        self.store = FakeClaimStore(self.module)
        self.module._client = lambda: self.store
        self.path = self.module._download_claim_path(DATE)

    def put_claim(self, execution, started):
        self.store.objects[self.path] = (
            json.dumps({"execution": execution, "started_utc": started}),
            99,
        )

    def acquire(self, execution="exec-1"):
        with mock.patch.dict("os.environ", {"CLOUD_RUN_EXECUTION": execution}):
            return self.module._acquire_download_claim("common", DATE)

    def test_claim_path_is_not_a_trigger_or_an_ic_file(self):
        self.assertEqual(self.path, f"ic/ecmwf/{DATE}/download-claim.json")
        self.assertFalse(self.path.endswith("_done"))

    def test_first_execution_claims_and_release_deletes(self):
        self.assertTrue(self.acquire())
        self.assertIn(self.path, self.store.objects)
        self.module._release_download_claim("common", DATE)
        self.assertNotIn(self.path, self.store.objects)
        self.module._release_download_claim("common", DATE)  # already gone: no error

    def test_fresh_claim_from_another_execution_blocks(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.put_claim("exec-0", now)
        self.assertFalse(self.acquire("exec-1"))

    def test_own_claim_is_reused_on_a_cloud_run_retry(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.put_claim("exec-1", now)
        self.assertTrue(self.acquire("exec-1"))

    def test_stale_claim_from_a_crashed_execution_is_taken_over(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=1300)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.put_claim("exec-0", old)
        self.assertTrue(self.acquire("exec-1"))
        self.assertIn('"exec-1"', self.store.objects[self.path][0])

    def test_stale_age_is_job_timeout_plus_margin(self):
        self.assertEqual(self.module.DOWNLOAD_CLAIM_MAX_AGE_SECONDS, 1200)
        compute = (
            Path(__file__).resolve().parents[1] / "terraform/modules/compute/main.tf"
        ).read_text()
        downloader = compute.split("    downloader = {", 1)[1].split("    }", 1)[0]
        self.assertIn('timeout = "900s"', downloader)


if __name__ == "__main__":
    unittest.main()
