import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = REPO_ROOT / "docker/drywetcast/src/main.py"
PIPELINE_STATE_PATH = REPO_ROOT / "docker/pipeline-state/src/main.py"

DATE = "20261007T00"
DAY = "20261007"
COMMON = "common"
INDIA = "india-bucket"
CONFIGS = ["gefs_reduced", "gefs_full", "ncmrwf_reduced", "ncmrwf_full"]
START = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
BUNDLE_PREFIX = "weights/drywetcast/subdistrict/v1/"
BUNDLE_URI = f"gs://{COMMON}/{BUNDLE_PREFIX}"


def _exceptions_module():
    google = sys.modules.setdefault("google", types.ModuleType("google"))
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
    google.api_core = api_core
    return exceptions


def load_wrapper():
    _exceptions_module()
    google = sys.modules["google"]
    cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    storage = sys.modules.setdefault(
        "google.cloud.storage", types.ModuleType("google.cloud.storage")
    )
    if not hasattr(storage, "Client"):
        storage.Client = object
    cloud.storage = storage
    google.cloud = cloud
    name = "drywetcast_wrapper_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, WRAPPER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeGCS:
    """In-memory GCS with generations and if_generation_match preconditions."""

    def __init__(self, module):
        self.module = module
        self.objects = {}  # (bucket, name) -> [bytes, generation]
        self.counter = 0
        self.writes = []  # (bucket, name) in write order

    def bucket(self, name):
        return FakeBucket(self, name)

    def list_blobs(self, bucket, prefix=""):
        return [
            FakeBlob(self, b, n)
            for (b, n) in sorted(self.objects)
            if b == bucket and n.startswith(prefix)
        ]

    def put(self, bucket, name, data=b"x"):
        self.counter += 1
        self.objects[(bucket, name)] = [
            data if isinstance(data, bytes) else data.encode(),
            self.counter,
        ]
        self.writes.append((bucket, name))

    def text(self, bucket, name):
        return self.objects[(bucket, name)][0].decode()

    def json(self, bucket, name):
        return json.loads(self.text(bucket, name))

    def has(self, bucket, name):
        return (bucket, name) in self.objects


class FakeBucket:
    def __init__(self, gcs, name):
        self.gcs, self.name = gcs, name

    def blob(self, path):
        return FakeBlob(self.gcs, self.name, path)

    def get_blob(self, path):
        return (
            FakeBlob(self.gcs, self.name, path)
            if self.gcs.has(self.name, path)
            else None
        )


class FakeBlob:
    def __init__(self, gcs, bucket, name):
        self.gcs, self.bucket, self.name = gcs, bucket, name

    @property
    def generation(self):
        entry = self.gcs.objects.get((self.bucket, self.name))
        return entry[1] if entry else None

    def exists(self):
        return self.gcs.has(self.bucket, self.name)

    def _check(self, if_generation_match):
        if if_generation_match is None:
            return
        current = self.generation or 0
        if current != if_generation_match:
            raise self.gcs.module.PreconditionFailed(self.name)

    def download_as_text(self, if_generation_match=None):
        if not self.exists():
            raise self.gcs.module.NotFound(self.name)
        self._check(if_generation_match)
        return self.gcs.text(self.bucket, self.name)

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        self._check(if_generation_match)
        self.gcs.put(self.bucket, self.name, data)

    def upload_from_filename(self, filename):
        self.gcs.put(self.bucket, self.name, Path(filename).read_bytes())

    def download_to_filename(self, filename):
        Path(filename).write_bytes(self.gcs.objects[(self.bucket, self.name)][0])

    def delete(self):
        if not self.exists():
            raise self.gcs.module.NotFound(self.name)
        del self.gcs.objects[(self.bucket, self.name)]


class DryWetCastJobTest(unittest.TestCase):
    def setUp(self):
        self.module = load_wrapper()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True)
        )
        self.gcs = FakeGCS(self.module)
        self.module._CLIENT = self.gcs
        self.module.WORK_DIR = self.tmp / "work"
        self.clock = [START]
        self.calls = []
        self.fail_configs = set()
        self.fail_subdistrict = set()
        self.subdistrict_dirs = []  # (config, dir passed to DryWetCast)
        self.fail_gefs_downloads = 0
        self.ncmrwf_answers = []
        self.sleeps = []
        self.put_store()

    # -- fixtures ----------------------------------------------------------
    def put_store(self):
        prefix = self.module.store_path(DATE)
        self.gcs.put(INDIA, f"{prefix}/.zmetadata", "{}")
        self.gcs.put(
            INDIA,
            f"{prefix}/.zattrs",
            json.dumps({"code_version": "abc1234", "members": 51}),
        )
        self.gcs.put(INDIA, f"{prefix}/tp/0.0.0.0.0", b"chunk")

    def put_bundle(self, manifest=None, corrupt=None):
        """The required files plus a README, and a manifest without a header."""
        names = [*self.module.SUBDISTRICT_REQUIRED, "README.txt"]
        rows = []
        for name in names:
            data = f"content of {name}".encode()
            rows.append(f"{len(data)}\t{hashlib.sha256(data).hexdigest()}\t{name}")
            if name == corrupt:
                data += b" tampered"
            self.gcs.put(COMMON, BUNDLE_PREFIX + name, data)
        self.gcs.put(
            COMMON,
            BUNDLE_PREFIX + "subdistrict_manifest.tsv",
            manifest if manifest is not None else "\n".join(rows) + "\n",
        )
        self.module.SUBDISTRICT_GCS = BUNDLE_URI

    def bundle_downloads(self):
        return [
            n for b, n in self.gcs.writes if b == COMMON and n.startswith(BUNDLE_PREFIX)
        ]

    def fake_run(self, args, subdistrict_dir=None):
        def value(flag):
            return args[args.index(flag) + 1]

        if "--download-only" in args:
            config = value("--config")
            self.calls.append(("download", config))
            if config.startswith("gefs_") and self.fail_gefs_downloads:
                self.fail_gefs_downloads -= 1
                raise subprocess.CalledProcessError(1, "run_forecast.py")
            scratch = Path(value("--out-dir"))
            (scratch / f"gefs_{DAY}_00z" / "mean_spread").mkdir(
                parents=True, exist_ok=True
            )
            if config.startswith("ncmrwf_"):
                (scratch / f"ncmrwf_{DAY}").mkdir(parents=True, exist_ok=True)
                (scratch / f"ncmrwf_{DAY}" / f"{DAY}.nc").write_bytes(b"n" * 1000)
            return
        config = value("--config")
        self.calls.append(("run", config))
        self.subdistrict_dirs.append((config, subdistrict_dir))
        if config in self.fail_configs:
            raise subprocess.CalledProcessError(1, f"run_forecast.py --config {config}")
        fig = Path(value("--fig-out"))
        fig.write_bytes(b"png")
        fig.with_suffix(".npz").write_bytes(b"npz")
        fig.with_name(f"{fig.stem}_outlook.png").write_bytes(b"outlook")
        if subdistrict_dir is not None:
            # DryWetCast checks the bundle is complete before using it.
            for name in self.module.SUBDISTRICT_REQUIRED:
                assert (Path(subdistrict_dir) / name).is_file(), name
            fig.with_name(f"{fig.stem}_subdistrict.pkl").write_bytes(b"pkl")
            if config in self.fail_subdistrict:
                raise subprocess.CalledProcessError(1, "run_forecast.py subdistrict")
            fig.with_name(f"{fig.stem}_subdistrict.png").write_bytes(b"map")
        archive = Path(value("--archive-dir")) / config
        archive.mkdir(parents=True, exist_ok=True)
        (archive / f"{DAY}_00.npz").write_bytes(b"npz")

    def ncmrwf_available(self, day):
        self.calls.append(("ncmrwf_check", day))
        answer = self.ncmrwf_answers.pop(0) if self.ncmrwf_answers else True
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock[0] += timedelta(seconds=seconds)

    def invoke(self, configs=CONFIGS, finalize=False, execution="exec-1"):
        env = {
            "DATE": DATE,
            "FORECAST_REGION": "india",
            "DRYWETCAST_CONFIGS": json.dumps(configs),
            "DRYWETCAST_FINALIZE": "true" if finalize else "false",
            "GCS_COMMON_BUCKET": COMMON,
            "GCS_REGION_BUCKETS": json.dumps({"india": INDIA}),
            "CLOUD_RUN_EXECUTION": execution,
            "ACTION": "run",
        }
        records = []
        handler = logging.Handler(level=logging.INFO)
        handler.emit = lambda record: records.append(
            f"{record.levelname}:{record.getMessage()}"
        )
        self.module.logger.addHandler(handler)
        try:
            with (
                mock.patch.object(
                    self.module, "run_drywetcast", side_effect=self.fake_run
                ),
                mock.patch.object(
                    self.module, "ncmrwf_available", side_effect=self.ncmrwf_available
                ),
                mock.patch.object(
                    self.module, "utc_now", side_effect=lambda: self.clock[0]
                ),
                mock.patch.object(self.module.time, "sleep", side_effect=self.sleep),
            ):
                result = CliRunner().invoke(self.module.main, [], env=env)
        finally:
            self.module.logger.removeHandler(handler)
        self.logs = records
        return result

    def marker(self, config):
        return self.module.config_marker_path("india", config, DATE)

    def status(self):
        return self.gcs.json(INDIA, self.module.status_path(DATE))["configs"]

    # -- tests ---------------------------------------------------------------
    def test_all_configs_run_upload_and_mark_done(self):
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        for config in CONFIGS:
            prefix = self.module.product_prefix(DATE, config)
            self.assertTrue(self.gcs.has(INDIA, f"{prefix}/{DAY}_00.png"))
            self.assertTrue(self.gcs.has(INDIA, f"{prefix}/{DAY}_00.npz"))
            self.assertTrue(
                self.gcs.has(INDIA, f"output/drywetcast/archive/{config}/{DAY}_00.npz")
            )
            self.assertTrue(self.gcs.has(COMMON, self.marker(config)))
            self.assertEqual(self.status()[config]["state"], "done")
        self.assertTrue(
            self.gcs.has(COMMON, f"intermediate/drywetcast_india_{DATE}_done")
        )
        self.assertFalse(self.gcs.has(INDIA, self.module.claim_path(DATE)))
        manifest = self.gcs.json(INDIA, self.module.manifest_path(DATE))
        self.assertEqual(manifest["aifs_store_code_version"], "abc1234")
        self.assertEqual(manifest["ncmrwf_file_bytes"], 1000)
        self.assertEqual(
            [c for c in self.calls if c[0] != "ncmrwf_check"],
            [
                ("download", "gefs_reduced"),
                ("run", "gefs_reduced"),
                ("run", "gefs_full"),
                ("download", "ncmrwf_reduced"),
                ("run", "ncmrwf_reduced"),
                ("run", "ncmrwf_full"),
            ],
        )

    def test_gefs_products_are_marked_before_any_ncmrwf_check(self):
        self.invoke()
        first_check = self.calls.index(("ncmrwf_check", DAY))
        self.assertLess(self.calls.index(("run", "gefs_full")), first_check)
        writes = [name for bucket, name in self.gcs.writes if bucket == COMMON]
        self.assertLess(
            writes.index(self.marker("gefs_full")),
            writes.index(self.marker("ncmrwf_reduced")),
        )

    def test_ncmrwf_late_leaves_configs_pending_after_30_minutes(self):
        self.ncmrwf_answers = [False] * 20
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(self.gcs.has(COMMON, self.marker("gefs_reduced")))
        self.assertTrue(self.gcs.has(COMMON, self.marker("gefs_full")))
        self.assertFalse(self.gcs.has(COMMON, self.marker("ncmrwf_reduced")))
        self.assertEqual(self.status()["ncmrwf_full"]["state"], "ncmrwf_pending")
        self.assertEqual(sum(self.sleeps), 30 * 60)
        self.assertEqual(
            self.calls.count(("ncmrwf_check", DAY)), 7
        )  # every 5 min for 30 min
        self.assertFalse(
            self.gcs.has(COMMON, f"intermediate/drywetcast_india_{DATE}_done")
        )
        self.assertNotIn(("download", "ncmrwf_reduced"), self.calls)

    def test_finalize_marks_ncmrwf_unavailable_with_error_and_nonzero_exit(self):
        self.ncmrwf_answers = [False]
        result = self.invoke(configs=["ncmrwf_reduced", "ncmrwf_full"], finalize=True)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not on the portal by the daily cutoff", result.output)
        self.assertEqual(self.sleeps, [])  # one last check, no polling
        self.assertEqual(self.status()["ncmrwf_reduced"]["state"], "ncmrwf_unavailable")
        self.assertTrue(
            any("ERROR" in line and "UNAVAILABLE" in line for line in self.logs)
        )

    def test_finalize_with_ncmrwf_present_runs_normally(self):
        result = self.invoke(configs=["ncmrwf_reduced", "ncmrwf_full"], finalize=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(self.gcs.has(COMMON, self.marker("ncmrwf_full")))

    def test_fresh_claim_from_another_execution_exits_without_running(self):
        claim = {
            "execution": "exec-0",
            "started_utc": "2026-10-07T08:50:00Z",
            "configs": CONFIGS,
        }
        self.gcs.put(INDIA, self.module.claim_path(DATE), json.dumps(claim))
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.calls, [])
        self.assertEqual(
            self.gcs.json(INDIA, self.module.claim_path(DATE))["execution"], "exec-0"
        )

    def test_stale_claim_is_taken_over(self):
        claim = {
            "execution": "exec-0",
            "started_utc": "2026-10-07T07:00:00Z",
            "configs": CONFIGS,
        }
        self.gcs.put(INDIA, self.module.claim_path(DATE), json.dumps(claim))
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(("run", "gefs_reduced"), self.calls)

    def test_own_claim_is_reused_on_a_cloud_run_retry(self):
        claim = {
            "execution": "exec-1",
            "started_utc": "2026-10-07T08:59:00Z",
            "configs": CONFIGS,
        }
        self.gcs.put(INDIA, self.module.claim_path(DATE), json.dumps(claim))
        result = self.invoke(execution="exec-1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(("run", "gefs_reduced"), self.calls)

    def test_one_config_failure_does_not_stop_the_others(self):
        self.fail_configs = {"gefs_full"}
        result = self.invoke()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("gefs_full failed", result.output)
        self.assertTrue(self.gcs.has(COMMON, self.marker("ncmrwf_full")))
        self.assertFalse(self.gcs.has(COMMON, self.marker("gefs_full")))
        self.assertEqual(
            self.status()["gefs_full"],
            {**self.status()["gefs_full"], "state": "failed", "attempts": 1},
        )
        self.invoke(configs=["gefs_full"])
        self.assertEqual(self.status()["gefs_full"]["attempts"], 2)
        self.assertFalse(self.gcs.has(INDIA, self.module.claim_path(DATE)))

    def test_gefs_download_is_retried_after_clearing_atmosphere_files(self):
        self.fail_gefs_downloads = 1
        stale = (
            self.module.WORK_DIR / DATE / "scratch" / f"gefs_{DAY}_00z" / "mean_spread"
        )

        original = self.fake_run

        def run_with_partial_file(args, **kwargs):
            if "--download-only" in args and self.fail_gefs_downloads:
                stale.mkdir(parents=True, exist_ok=True)
                (stale / "truncated.grb2").write_bytes(b"x")
            original(args, **kwargs)

        self.fake_run = run_with_partial_file
        result = self.invoke(configs=["gefs_reduced"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.calls.count(("download", "gefs_reduced")), 2)
        self.assertFalse((stale / "truncated.grb2").exists())
        self.assertEqual(self.sleeps, [60])

    def test_missing_store_fails_every_config_without_running(self):
        for key in [
            k for k in self.gcs.objects if k[1].startswith(self.module.store_path(DATE))
        ]:
            del self.gcs.objects[key]
        result = self.invoke()
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn(("run", "gefs_reduced"), self.calls)
        self.assertEqual({v["state"] for v in self.status().values()}, {"failed"})

    def test_portal_error_fails_only_ncmrwf_configs(self):
        self.ncmrwf_answers = [
            RuntimeError("NCMRWF portal: listing refused (wrong or expired API key?)")
        ]
        result = self.invoke()
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(self.gcs.has(COMMON, self.marker("gefs_full")))
        self.assertEqual(self.status()["ncmrwf_reduced"]["state"], "failed")
        self.assertIn("portal", self.status()["ncmrwf_reduced"]["message"])

    def test_configs_already_done_are_skipped(self):
        self.gcs.put(COMMON, self.marker("gefs_reduced"), "done")
        self.invoke(configs=["gefs_reduced", "gefs_full"])
        self.assertNotIn(("run", "gefs_reduced"), self.calls)
        self.assertIn(("run", "gefs_full"), self.calls)

    def test_rejects_unknown_configs_and_non_00z_dates(self):
        self.assertEqual(self.invoke(configs=["gefs_reduced", "bogus"]).exit_code, 1)
        result = CliRunner().invoke(
            self.module.main, ["--date", "20261007T12"], env={"ACTION": "run"}
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("00z", result.output)

    # -- subdistrict bundle and products (22cdbe5) ---------------------------
    def product(self, config, suffix):
        return f"{self.module.product_prefix(DATE, config)}/{DAY}_00{suffix}"

    def test_verified_bundle_adds_subdistrict_products_before_the_marker(self):
        self.put_bundle()
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        staged = self.module.WORK_DIR / DATE / "subdistrict"
        self.assertEqual(
            self.subdistrict_dirs, [(config, staged) for config in CONFIGS]
        )
        writes = [name for _, name in self.gcs.writes]
        for config in CONFIGS:
            for suffix in (
                ".png",
                ".npz",
                "_outlook.png",
                "_subdistrict.pkl",
                "_subdistrict.png",
            ):
                with self.subTest(config=config, suffix=suffix):
                    self.assertLess(
                        writes.index(self.product(config, suffix)),
                        writes.index(self.marker(config)),
                    )
        manifest = self.gcs.json(INDIA, self.module.manifest_path(DATE))
        bundle = manifest["subdistrict_bundle"]
        self.assertEqual(bundle["state"], "staged")
        self.assertEqual(bundle["source"], BUNDLE_URI)
        self.assertEqual(bundle["files"], 12)
        manifest_text = self.gcs.text(
            COMMON, BUNDLE_PREFIX + "subdistrict_manifest.tsv"
        ).encode()
        self.assertEqual(
            bundle["manifest_sha256"], hashlib.sha256(manifest_text).hexdigest()
        )
        run = manifest["this_run"]["gefs_full"]
        self.assertEqual(run["subdistrict"], "done")
        self.assertEqual(
            run["files"],
            [
                f"{DAY}_00.png",
                f"{DAY}_00.npz",
                f"{DAY}_00_outlook.png",
                f"{DAY}_00_subdistrict.pkl",
                f"{DAY}_00_subdistrict.png",
            ],
        )

    def assert_core_products_without_subdistrict(self, result):
        self.assertEqual(result.exit_code, 0, result.output)
        for config in CONFIGS:
            self.assertTrue(self.gcs.has(COMMON, self.marker(config)))
            self.assertTrue(self.gcs.has(INDIA, self.product(config, "_outlook.png")))
            self.assertFalse(
                self.gcs.has(INDIA, self.product(config, "_subdistrict.pkl"))
            )
            self.assertFalse(
                self.gcs.has(INDIA, self.product(config, "_subdistrict.png"))
            )

    def test_bad_checksum_skips_subdistrict_products_but_not_the_configs(self):
        self.put_bundle(corrupt="target_to_index.pkl")
        result = self.invoke()
        self.assert_core_products_without_subdistrict(result)
        self.assertEqual({d for _, d in self.subdistrict_dirs}, {None})
        self.assertFalse((self.module.WORK_DIR / DATE / "subdistrict").exists())
        bundle = self.gcs.json(INDIA, self.module.manifest_path(DATE))[
            "subdistrict_bundle"
        ]
        self.assertEqual(bundle["state"], "unavailable")
        self.assertIn("target_to_index.pkl", bundle["message"])
        self.assertTrue(
            any(
                line.startswith("ERROR:DRYWETCAST_SUBDISTRICT_UNAVAILABLE")
                for line in self.logs
            )
        )

    def test_bad_manifests_are_rejected(self):
        required = self.module.SUBDISTRICT_REQUIRED
        sha = "0" * 64
        cases = {
            "header line": "size\tsha256\tname\n",
            "missing required file": "".join(
                f"1\t{sha}\t{name}\n" for name in required[1:]
            ),
            "path in name": f"1\t{sha}\t../weight_matrix.npy\n",
            "empty": "",
        }
        for label, manifest in cases.items():
            with self.subTest(label):
                self.setUp()
                self.put_bundle(manifest=manifest)
                result = self.invoke(configs=["gefs_reduced"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(self.subdistrict_dirs, [("gefs_reduced", None)])
                bundle = self.gcs.json(INDIA, self.module.manifest_path(DATE))[
                    "subdistrict_bundle"
                ]
                self.assertEqual(bundle["state"], "unavailable")

    def test_without_a_bundle_setting_only_the_outlook_is_added(self):
        result = self.invoke()
        self.assert_core_products_without_subdistrict(result)
        manifest = self.gcs.json(INDIA, self.module.manifest_path(DATE))
        self.assertEqual(manifest["subdistrict_bundle"], {"state": "not_configured"})
        self.assertEqual(manifest["this_run"]["ncmrwf_full"]["subdistrict"], "skipped")

    def test_subdistrict_step_failure_reruns_the_config_without_it(self):
        self.put_bundle()
        self.fail_subdistrict = {"gefs_full"}
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        staged = self.module.WORK_DIR / DATE / "subdistrict"
        self.assertEqual(
            [d for c, d in self.subdistrict_dirs if c == "gefs_full"], [staged, None]
        )
        self.assertTrue(self.gcs.has(COMMON, self.marker("gefs_full")))
        self.assertTrue(self.gcs.has(INDIA, self.product("gefs_full", "_outlook.png")))
        # The pickle left by the failed attempt is not uploaded.
        self.assertFalse(
            self.gcs.has(INDIA, self.product("gefs_full", "_subdistrict.pkl"))
        )
        self.assertTrue(
            self.gcs.has(INDIA, self.product("gefs_reduced", "_subdistrict.png"))
        )
        run = self.gcs.json(INDIA, self.module.manifest_path(DATE))["this_run"]
        self.assertEqual(run["gefs_full"]["subdistrict"], "failed")
        self.assertEqual(run["gefs_reduced"]["subdistrict"], "done")
        self.assertTrue(
            any(
                line.startswith("ERROR:DRYWETCAST_SUBDISTRICT_FAILED gefs_full")
                for line in self.logs
            )
        )

    def test_a_core_failure_still_fails_the_config_after_the_rerun(self):
        self.put_bundle()
        self.fail_configs = {"gefs_full"}
        result = self.invoke()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.calls.count(("run", "gefs_full")), 2)
        self.assertEqual(self.status()["gefs_full"]["state"], "failed")

    def test_bundle_is_not_copied_when_no_config_runs(self):
        self.put_bundle()
        before = len(self.bundle_downloads())
        self.ncmrwf_answers = [False] * 20
        result = self.invoke(configs=["ncmrwf_reduced", "ncmrwf_full"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse((self.module.WORK_DIR / DATE / "subdistrict").exists())
        self.assertEqual(len(self.bundle_downloads()), before)
        bundle = self.gcs.json(INDIA, self.module.manifest_path(DATE))[
            "subdistrict_bundle"
        ]
        self.assertEqual(bundle["state"], "not_staged")

    def test_run_drywetcast_passes_only_the_verified_bundle_path(self):
        with (
            mock.patch.dict(
                self.module.os.environ, {"DRYWETCAST_SUBDISTRICT_DATA": "/unverified"}
            ),
            mock.patch.object(self.module.subprocess, "run") as run,
        ):
            self.module.run_drywetcast(["--config", "gefs_reduced"])
            self.module.run_drywetcast(
                ["--config", "gefs_reduced"], subdistrict_dir=Path("/tmp/ok")
            )
        first, second = (call.kwargs["env"] for call in run.call_args_list)
        self.assertNotIn("DRYWETCAST_SUBDISTRICT_DATA", first)
        self.assertEqual(second["DRYWETCAST_SUBDISTRICT_DATA"], "/tmp/ok")

    def test_gcs_uri_and_manifest_parsing(self):
        self.assertEqual(self.module.parse_gcs_uri(BUNDLE_URI), (COMMON, BUNDLE_PREFIX))
        self.assertEqual(
            self.module.parse_gcs_uri(f"gs://{COMMON}/{BUNDLE_PREFIX.rstrip('/')}"),
            (COMMON, BUNDLE_PREFIX),
        )
        with self.assertRaises(ValueError):
            self.module.parse_gcs_uri(f"{COMMON}/{BUNDLE_PREFIX}")
        sha = "a" * 64
        self.assertEqual(
            self.module.parse_subdistrict_manifest(f"995\t{sha}\tREADME.txt\n\n"),
            [(995, sha, "README.txt")],
        )
        for bad in (
            f"995 {sha} README.txt",
            f"995\t{sha.upper()}\tREADME.txt",
            f"995\t{sha}\tdir/README.txt",
            f"-1\t{sha}\tREADME.txt",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.module.parse_subdistrict_manifest(bad)

    def test_probe_reports_reachability(self):
        portal = types.SimpleNamespace(list_folder=lambda path: [{"name": "2026"}])
        with (
            mock.patch.object(
                self.module.requests,
                "head",
                return_value=types.SimpleNamespace(status_code=200),
            ),
            mock.patch.object(self.module, "ncmrwf_module", return_value=portal),
        ):
            self.assertEqual(self.module.probe(), 0)

        def refused(path):
            raise RuntimeError("NCMRWF portal: listing refused")

        with (
            mock.patch.object(
                self.module.requests,
                "head",
                return_value=types.SimpleNamespace(status_code=200),
            ),
            mock.patch.object(
                self.module,
                "ncmrwf_module",
                return_value=types.SimpleNamespace(list_folder=refused),
            ),
            self.assertLogs(self.module.logger, level="ERROR"),
        ):
            self.assertEqual(self.module.probe(), 1)


class SharedConventionsTest(unittest.TestCase):
    """The job and pipeline-state must agree on paths and config names."""

    def test_paths_and_configs_match_pipeline_state(self):
        wrapper = load_wrapper()
        source = PIPELINE_STATE_PATH.read_text()
        self.assertIn(
            'DRYWETCAST_CONFIGS = ("gefs_reduced", "gefs_full", "ncmrwf_reduced", "ncmrwf_full")',
            source,
        )
        self.assertEqual(list(wrapper.CONFIGS), CONFIGS)
        for expected in (
            'return f"output/{model}/{date}/{model}/drywetcast/init_{date[:8]}T00.zarr"',
            'return f"intermediate/drywetcast_{region}_{config}_{date}_done"',
            'return f"drywetcast-state/{date}/claim.json"',
            'return f"drywetcast-state/{date}/status.json"',
        ):
            self.assertIn(expected, source)
        self.assertEqual(
            wrapper.store_path(DATE),
            f"output/AIFS_ENS_v2/{DATE}/AIFS_ENS_v2/drywetcast/init_{DAY}T00.zarr",
        )
        self.assertEqual(
            wrapper.config_marker_path("india", "gefs_full", DATE),
            f"intermediate/drywetcast_india_gefs_full_{DATE}_done",
        )
        self.assertEqual(
            wrapper.claim_path(DATE), f"drywetcast-state/{DATE}/claim.json"
        )
        self.assertEqual(
            wrapper.status_path(DATE), f"drywetcast-state/{DATE}/status.json"
        )
        # State objects must not look like workflow trigger markers.
        self.assertFalse(wrapper.claim_path(DATE).startswith("intermediate/"))


if __name__ == "__main__":
    unittest.main()
