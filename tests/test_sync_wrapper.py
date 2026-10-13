import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "docker/sync/src/main.py"


class FakeStorageClient:
    pass


@dataclass(frozen=True)
class FakeRule:
    name: str
    gcs_prefix: str
    local_root: str
    gcs_stage_dir: str | None = None
    gcs_date_kind: str | None = None


@dataclass(frozen=True)
class FakeConfig:
    region: str
    sync_root: Path
    cluster: str = "gcp"
    drive_root: str = "/drive"


def load_sync_wrapper():
    google_module = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud_module = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    api_core_module = sys.modules.setdefault("google.api_core", types.ModuleType("google.api_core"))
    exceptions_module = sys.modules.setdefault(
        "google.api_core.exceptions", types.ModuleType("google.api_core.exceptions")
    )
    storage_module = sys.modules.setdefault("google.cloud.storage", types.ModuleType("google.cloud.storage"))
    exceptions_module.NotFound = type("NotFound", (Exception,), {})
    exceptions_module.PreconditionFailed = type("PreconditionFailed", (Exception,), {})
    api_core_module.exceptions = exceptions_module
    storage_module.Client = FakeStorageClient
    cloud_module.storage = storage_module
    google_module.cloud = cloud_module

    sync_module = types.ModuleType("sync")
    sync_utils_module = types.ModuleType("sync.utils")
    sync_config_module = types.ModuleType("sync.utils.sync_config")
    sync_engine_module = types.ModuleType("sync.utils.sync_engine")
    sync_inventory_module = types.ModuleType("sync.utils.sync_inventory")
    sync_config_module.SyncConfig = object
    sync_config_module.SyncRule = FakeRule
    sync_config_module.load_sync_config = lambda *args, **kwargs: None
    sync_engine_module.SyncEngine = object
    sync_inventory_module.SyncInventory = object
    sys.modules["sync"] = sync_module
    sys.modules["sync.utils"] = sync_utils_module
    sys.modules["sync.utils.sync_config"] = sync_config_module
    sys.modules["sync.utils.sync_engine"] = sync_engine_module
    sys.modules["sync.utils.sync_inventory"] = sync_inventory_module

    module_name = "sync_wrapper_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class SyncWrapperTest(unittest.TestCase):
    def setUp(self):
        self.module = load_sync_wrapper()
        self.config = FakeConfig(region="ethiopia", sync_root=Path("/tmp/sync-root"))

    def test_blend_rule_is_filtered_to_selected_item_subtree(self):
        targets = self.module._staging_targets_for_rule(
            FakeRule(
                name="blend",
                gcs_prefix="output/blend/{date}/ethiopia2026/",
                local_root="blend/output/ethiopia2026",
            ),
            self.config,
            "20260605T00",
            [{"type": "blend", "name": "AIFS_single_v2_AIFS_ENS_v2"}],
        )

        self.assertEqual(
            targets,
            [
                (
                    "output/blend/20260605T00/ethiopia2026/20260605T00/AIFS_single_v2_AIFS_ENS_v2/",
                    Path("/tmp/sync-root/blend/output/ethiopia2026/20260605T00/AIFS_single_v2_AIFS_ENS_v2"),
                )
            ],
        )

    def test_diagnostics_rule_is_filtered_to_selected_item_subtree(self):
        targets = self.module._staging_targets_for_rule(
            FakeRule(
                name="model_diagnostics",
                gcs_prefix="output/model_diagnostics/{date}/ethiopia/",
                local_root="model_diagnostics/output/ethiopia",
            ),
            self.config,
            "20260605T00",
            [{"type": "model_diagnostics", "name": "AIFS_single_v2_gencast"}],
        )

        self.assertEqual(
            targets,
            [
                (
                    "output/model_diagnostics/20260605T00/ethiopia/20260605T00/AIFS_single_v2_gencast/",
                    Path("/tmp/sync-root/model_diagnostics/output/ethiopia/20260605T00/AIFS_single_v2_gencast"),
                )
            ],
        )

    def test_model_rules_are_not_filtered_by_sync_items(self):
        targets = self.module._staging_targets_for_rule(
            FakeRule(
                name="AIFS_single_v2",
                gcs_prefix="output/AIFS_single_v2/{date}/",
                local_root="AIFS/output/ethiopia",
            ),
            self.config,
            "20260605T00",
            [{"type": "blend", "name": "AIFS_single_v2_AIFS_ENS_v2"}],
        )

        self.assertEqual(
            targets,
            [
                (
                    "output/AIFS_single_v2/20260605T00/",
                    Path("/tmp/sync-root/AIFS/output/ethiopia"),
                )
            ],
        )

    def test_parse_sync_items_accepts_valid_json_list(self):
        self.assertEqual(
            self.module._parse_sync_items(
                '[{"type":"blend","name":"AIFS_single_v2_AIFS_ENS_v2"}]'
            ),
            [{"type": "blend", "name": "AIFS_single_v2_AIFS_ENS_v2"}],
        )

    def test_sync_items_limit_rules_to_matching_blend_and_diagnostics_rules(self):
        self.assertEqual(
            self.module._rule_names_for_sync(
                ["AIFS_single_v2", "blend", "model_diagnostics"],
                [{"type": "model_diagnostics", "name": "AIFS_single_v2_gencast"}],
            ),
            {"model_diagnostics"},
        )
        self.assertEqual(
            self.module._rule_names_for_sync(
                ["AIFS_single_v2", "blend", "model_diagnostics"],
                [{"type": "blend", "name": "AIFS_single_v2_AIFS_ENS_v2"}],
            ),
            {"blend"},
        )

    def test_empty_sync_items_keep_legacy_rule_set(self):
        self.assertEqual(
            self.module._rule_names_for_sync(
                ["AIFS_single_v2", "blend", "model_diagnostics"],
                [],
            ),
            {"AIFS_single_v2", "blend", "model_diagnostics"},
        )

    def test_sync_item_marker_path_is_per_date_type_and_name(self):
        self.assertEqual(
            self.module._sync_item_marker_path(
                "20260605T00",
                {"type": "blend", "name": "AIFS_single_v2/AIFS_ENS_v2"},
                "done",
            ),
            "sync-items/20260605T00/blend/AIFS_single_v2_AIFS_ENS_v2.done.json",
        )



DAY = "20261013T00"
INDIA = "india-bucket"


class FakeGcs:
    """In-memory GCS with generations and if_generation_match preconditions."""

    def __init__(self, module):
        self.module = module
        self.objects = {}  # (bucket, name) -> [bytes, generation]
        self.counter = 0

    def bucket(self, name):
        return FakeGcsBucket(self, name)

    def list_blobs(self, bucket, prefix=""):
        return [
            FakeGcsBlob(self, b, n)
            for (b, n) in sorted(self.objects)
            if b == bucket and n.startswith(prefix)
        ]

    def put(self, bucket, name, data):
        self.counter += 1
        data = data if isinstance(data, bytes) else data.encode()
        self.objects[(bucket, name)] = [data, self.counter]

    def json(self, bucket, name):
        import json

        return json.loads(self.objects[(bucket, name)][0])

    def has(self, bucket, name):
        return (bucket, name) in self.objects


class FakeGcsBucket:
    def __init__(self, gcs, name):
        self.gcs, self.name = gcs, name

    def blob(self, path):
        return FakeGcsBlob(self.gcs, self.name, path)


class FakeGcsBlob:
    def __init__(self, gcs, bucket, name):
        self.gcs, self.bucket, self.name = gcs, bucket, name
        self.generation = None

    def exists(self):
        return self.gcs.has(self.bucket, self.name)

    def reload(self):
        if not self.exists():
            raise self.gcs.module.NotFound(self.name)
        self.generation = self.gcs.objects[(self.bucket, self.name)][1]

    def download_as_text(self):
        if not self.exists():
            raise self.gcs.module.NotFound(self.name)
        return self.gcs.objects[(self.bucket, self.name)][0].decode()

    def download_to_filename(self, filename):
        Path(filename).write_bytes(self.gcs.objects[(self.bucket, self.name)][0])

    def upload_from_string(self, data, if_generation_match=None):
        if if_generation_match is not None:
            current = self.gcs.objects.get((self.bucket, self.name), [None, 0])[1]
            if current != if_generation_match:
                raise self.gcs.module.PreconditionFailed(self.name)
        self.gcs.put(self.bucket, self.name, data)

    def delete(self):
        if not self.exists():
            raise self.gcs.module.NotFound(self.name)
        del self.gcs.objects[(self.bucket, self.name)]


@dataclass
class FakeRunConfig:
    region: str
    sync_root: Path
    rules: tuple
    drive_root: str
    cluster: str = "gcp-dev"


DRYWETCAST_RULE = FakeRule(
    name="drywetcast",
    gcs_prefix="output/drywetcast/{date}/",
    local_root="drywetcast/output/india",
    gcs_stage_dir="drywetcast/output/india/{date}",
)


class DriveSyncRunTest(unittest.TestCase):
    """main(): drywetcast items, failures, attempt cap, the per-date lock, probe."""

    def setUp(self):
        import json
        import logging
        import tempfile

        self.json = json
        self.module = load_sync_wrapper()
        self.gcs = FakeGcs(self.module)
        self.module._client = lambda: self.gcs
        self.tmp = Path(tempfile.mkdtemp())
        self.module.DRIVE_AUTH_DIR = self.tmp / "auth"
        self.loaded = []
        self.synced = []
        self.engine_error = None

        def load_config(path, *, sync_root, cluster, drive_root, region):
            config = FakeRunConfig(region, sync_root, (DRYWETCAST_RULE,), drive_root)
            self.loaded.append(config)
            return config

        def run_engine(sync_config, rule_names, dates):
            if self.engine_error:
                raise self.engine_error
            root = sync_config.sync_root
            self.synced.append(
                sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
            )

        self.module.load_sync_config = load_config
        self.module._run_sync_engine = run_engine
        self.module._materialize_drive_auth = lambda: None
        for config in ("gefs_reduced", "gefs_full"):
            for name in ("20261013_00.png", "20261013_00_outlook.png"):
                self.gcs.put(INDIA, f"output/drywetcast/{DAY}/{config}/{name}", b"x")
        self.gcs.put(INDIA, f"output/drywetcast/{DAY}/manifest.json", b"{}")
        self.records = []
        handler = logging.Handler(level=logging.INFO)
        handler.emit = lambda record: self.records.append(
            f"{record.levelname}:{record.getMessage()}"
        )
        self.module.logger.addHandler(handler)
        self.addCleanup(self.module.logger.removeHandler, handler)
        level = self.module.logger.level
        self.module.logger.setLevel(logging.INFO)
        self.addCleanup(self.module.logger.setLevel, level)

    def invoke(self, execution="e1", items=("gefs_full",), git_push=True, extra=None):
        from unittest import mock

        from click.testing import CliRunner

        env = {
            "ACTION": "run",
            "DATE": DAY,
            "FORECAST_REGION": "india",
            "GCS_REGION_BUCKETS": self.json.dumps({"india": INDIA}),
            "SYNC_SPEC": self.json.dumps({"rules": ["drywetcast"], "git_push": git_push}),
            "SYNC_ITEMS": self.json.dumps([{"type": "drywetcast", "name": n} for n in items]),
            "ENABLE_DRIVE": "true",
            "CLOUD_RUN_EXECUTION": execution,
            "MONSOON_DRIVE_FOLDER_ID": "FOLDER",
            **(extra or {}),
        }

        def no_process(*args, **kwargs):
            raise AssertionError("sync must not start processes (no git push)")

        with (
            mock.patch("subprocess.run", no_process),
            mock.patch("subprocess.Popen", no_process),
            mock.patch("os.system", no_process),
            mock.patch.dict("os.environ", env),
        ):
            return CliRunner().invoke(self.module.main, [], env=env)

    def marker(self, name, state):
        return self.module._sync_item_marker_path(
            DAY, {"type": "drywetcast", "name": name}, state
        )

    def test_drywetcast_items_stage_only_their_config_and_resolve_in_the_folder(self):
        result = self.invoke(items=("gefs_full",))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.loaded[0].drive_root, "/")
        self.assertEqual(
            self.synced,
            [
                [
                    f"drywetcast/output/india/{DAY}/gefs_full/20261013_00.png",
                    f"drywetcast/output/india/{DAY}/gefs_full/20261013_00_outlook.png",
                ]
            ],
        )
        self.assertTrue(self.gcs.has(INDIA, self.marker("gefs_full", "done")))
        self.assertFalse(self.gcs.has(INDIA, self.module._date_lock_path(DAY, "drywetcast")))
        self.assertTrue(any("git_push" in line and "no-op" in line for line in self.records))

    def test_failure_releases_the_claim_and_counts_executions(self):
        self.engine_error = RuntimeError("Drive HTTP 403")
        result = self.invoke(execution="e1")
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(self.gcs.has(INDIA, self.marker("gefs_full", "active")))
        self.assertFalse(self.gcs.has(INDIA, self.module._date_lock_path(DAY, "drywetcast")))
        failed = self.gcs.json(INDIA, self.marker("gefs_full", "failed"))
        self.assertEqual(failed["executions"], ["e1"])
        self.assertIn("403", failed["last_error"])
        self.assertTrue(any(line.startswith("ERROR:SYNC_FAILED") for line in self.records))
        # A Cloud Run task retry of the same execution is not a new attempt.
        self.invoke(execution="e1")
        self.assertEqual(self.gcs.json(INDIA, self.marker("gefs_full", "failed"))["executions"], ["e1"])
        self.assertFalse(any("SYNC_ITEMS_GAVE_UP" in line for line in self.records))

        self.invoke(execution="e2")
        self.invoke(execution="e3")
        self.assertTrue(
            any(line.startswith("ERROR:SYNC_ITEMS_GAVE_UP") for line in self.records)
        )
        self.engine_error = None
        calls = len(self.synced)
        result = self.invoke(execution="e4")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(self.synced), calls)  # given up: not claimed again
        self.assertFalse(self.gcs.has(INDIA, self.marker("gefs_full", "done")))

    def test_a_held_lock_makes_a_second_execution_wait_then_fail_and_release(self):
        lock = self.module._date_lock_path(DAY, "drywetcast")
        now = self.module.datetime.now(self.module.timezone.utc).isoformat()
        self.gcs.put(INDIA, lock, self.json.dumps({"execution": "other", "started_at": now}))
        self.module.SYNC_LOCK_WAIT_SECONDS = 0
        result = self.invoke(execution="e1")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("waiting for sync lock", result.output)
        self.assertEqual(self.synced, [])
        self.assertTrue(self.gcs.has(INDIA, lock))  # still the other execution's
        self.assertFalse(self.gcs.has(INDIA, self.marker("gefs_full", "active")))

    def test_a_stale_lock_is_taken_over(self):
        lock = self.module._date_lock_path(DAY, "drywetcast")
        self.gcs.put(
            INDIA, lock, self.json.dumps({"execution": "dead", "started_at": "2026-10-13T07:00:00+00:00"})
        )
        result = self.invoke(execution="e1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(self.synced), 1)
        self.assertFalse(self.gcs.has(INDIA, lock))

    def test_a_killed_executions_claim_can_be_taken_over_after_45_minutes(self):
        self.assertEqual(self.module.SYNC_ITEM_CLAIM_TTL_SECONDS, 45 * 60)
        now = self.module.datetime.now(self.module.timezone.utc)
        active = self.marker("gefs_full", "active")
        for minutes, taken in ((40, False), (50, True)):
            with self.subTest(claim_age_minutes=minutes):
                claimed = (now - timedelta(minutes=minutes)).isoformat()
                self.gcs.put(INDIA, active, self.json.dumps({"updated_at": claimed}))
                self.synced.clear()
                result = self.invoke(execution=f"e-{minutes}")
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(len(self.synced), 1 if taken else 0)

    def test_run_requires_its_settings(self):
        from click.testing import CliRunner

        result = CliRunner().invoke(self.module.main, [], env={"ACTION": "run", "DATE": ""})
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Missing required settings", result.output)

    def install_fake_drive(self, fail_step=None):
        test = self

        class FakeClient:
            root_folder_id = "FOLDER"

            @classmethod
            def authenticated(cls):
                if fail_step == "credentials":
                    raise RuntimeError("Could not authenticate with Google Drive")
                return cls()

            def account_email(self):
                return "adam@uchicago.edu"

            def folder_info(self):
                return {"id": "FOLDER", "name": "DryWetCast", "driveId": None}

            def list_children(self):
                return [{"name": "drywetcast"}]

            def create_small_file(self, name, data):
                if fail_step == "create":
                    raise RuntimeError("HttpError 403 insufficientFilePermissions")
                test.created = (name, data)
                return {"id": "probe-file"}

            def get_file(self, file_id):
                return {"id": file_id, "size": str(len(test.created[1]))}

            def remove_file(self, file_id):
                test.removed = file_id
                return "deleted"

        drive = types.ModuleType("sync.utils.drive")
        drive.GoogleDriveClient = FakeClient
        sys.modules["sync.utils.drive"] = drive
        self.addCleanup(sys.modules.pop, "sync.utils.drive", None)

    def probe(self):
        from unittest import mock

        from click.testing import CliRunner

        def no_gcs():
            raise AssertionError("probe must not touch GCS")

        self.module._client = no_gcs
        env = {
            "ACTION": "probe",
            "GOOGLE_DRIVE_TOKEN_JSON": '{"refresh_token": "SECRET-TOKEN"}',
            "MONSOON_DRIVE_FOLDER_ID": "FOLDER",
        }
        with mock.patch.dict("os.environ", env):
            return CliRunner().invoke(self.module.main, [], env=env)

    def probe_result(self):
        line = next(r for r in self.records if r.startswith("INFO:SYNC_PROBE "))
        return self.json.loads(line.split(" ", 1)[1])

    def test_probe_checks_login_folder_and_write_without_gcs(self):
        self.install_fake_drive()
        result = self.probe()
        self.assertEqual(result.exit_code, 0, result.output)
        probe = self.probe_result()
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["account"], "adam@uchicago.edu")
        self.assertFalse(probe["shared_drive"])
        self.assertEqual(probe["first_children"], ["drywetcast"])
        self.assertTrue(probe["readback_ok"])
        self.assertEqual(probe["removed"], "deleted")
        self.assertEqual(self.removed, "probe-file")
        self.assertTrue(self.created[0].startswith("_monsoon_sync_probe_"))
        self.assertFalse(any("SECRET-TOKEN" in r for r in self.records))

    def test_probe_reports_the_failed_step_and_exits_nonzero(self):
        for step in ("credentials", "create"):
            with self.subTest(step=step):
                self.records.clear()
                self.install_fake_drive(fail_step=step)
                result = self.probe()
                self.assertEqual(result.exit_code, 1)
                probe = self.probe_result()
                self.assertFalse(probe["ok"])
                self.assertEqual(probe["failed_step"], step)


if __name__ == "__main__":
    unittest.main()
