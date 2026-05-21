import datetime as dt
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path


def install_google_stubs() -> None:
    google_module = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud_module = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    api_core_module = sys.modules.setdefault("google.api_core", types.ModuleType("google.api_core"))
    exceptions_module = sys.modules.setdefault(
        "google.api_core.exceptions", types.ModuleType("google.api_core.exceptions")
    )
    retry_module = sys.modules.setdefault("google.api_core.retry", types.ModuleType("google.api_core.retry"))
    auth_module = sys.modules.setdefault("google.auth", types.ModuleType("google.auth"))
    storage_module = sys.modules.setdefault("google.cloud.storage", types.ModuleType("google.cloud.storage"))
    tpu_module = sys.modules.setdefault("google.cloud.tpu_v2alpha1", types.ModuleType("google.cloud.tpu_v2alpha1"))

    class NotFound(Exception):
        pass

    class AlreadyExists(Exception):
        pass

    exceptions_module.NotFound = NotFound
    exceptions_module.AlreadyExists = AlreadyExists
    api_core_module.exceptions = exceptions_module
    retry_module.if_transient_error = lambda _exc: False

    class Retry:
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, func):
            return func

    retry_module.Retry = Retry
    api_core_module.retry = retry_module
    auth_module.default = lambda scopes=None: (types.SimpleNamespace(), "project")
    cloud_module.storage = storage_module
    cloud_module.tpu_v2alpha1 = tpu_module
    google_module.auth = auth_module
    google_module.cloud = cloud_module


install_google_stubs()

MODULE_PATH = Path(__file__).resolve().parents[1] / "docker/tpu-dispatch/src/main.py"
SPEC = importlib.util.spec_from_file_location("tpu_dispatch_main", MODULE_PATH)
tpu_dispatch = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = tpu_dispatch
SPEC.loader.exec_module(tpu_dispatch)
tpu_dispatch.logger.setLevel(logging.CRITICAL)


class FakeStatusStore:
    def __init__(self, reads=None, existing_paths=None, listed=None):
        self.reads = list(reads or [])
        self.existing_paths = set(existing_paths or [])
        self.listed = dict(listed or {})
        self.writes = []

    def read(self, bucket_name, path):
        if self.reads:
            return self.reads.pop(0)
        return None

    def write(self, bucket_name, path, payload):
        self.writes.append((bucket_name, path, payload))

    def exists(self, bucket_name, path):
        return path in self.existing_paths

    def list_json(self, bucket_name, prefix):
        return self.listed.get(prefix, {})


class FakeTpuClient:
    def __init__(self, states=None, create_error=None):
        self.states = list(states or ["ACTIVE"])
        self.create_error = create_error
        self.created = []
        self.deleted = []

    def create(self, queued_resource_id, node_id, metadata):
        if self.create_error:
            raise self.create_error
        self.created.append((queued_resource_id, node_id, metadata))

    def state(self, queued_resource_id):
        if self.states:
            return self.states.pop(0)
        return "ACTIVE"

    def delete(self, queued_resource_id, wait=True):
        self.deleted.append((queued_resource_id, wait))


def make_config(**overrides):
    values = {
        "project_id": "project",
        "workload_name": "gencast",
        "run_id": "gencast-20260515-00",
        "date": "20260515T00",
        "workload_image": "us-central1-docker.pkg.dev/project/repo/monsoon-gencast:latest",
        "forecast_regions": '["ethiopia"]',
        "common_bucket": "common",
        "region_buckets": '{"ethiopia":"ethiopia-bucket"}',
        "tpu_zone": "us-central1-a",
        "tpu_network": "net",
        "tpu_subnetwork": "subnet",
        "tpu_service_account": "pipeline@example.iam.gserviceaccount.com",
        "artifact_registry_host": "us-central1-docker.pkg.dev",
        "poll_interval_seconds": 0,
        "queue_timeout_seconds": 1,
        "run_timeout_seconds": 1,
    }
    values.update(overrides)
    return tpu_dispatch.DispatchConfig(**values)


class TpuDispatchTest(unittest.TestCase):
    def test_state_helpers_normalize_api_values(self):
        self.assertEqual(
            tpu_dispatch.enum_name(types.SimpleNamespace(name="ACTIVE")), "ACTIVE"
        )
        self.assertEqual(tpu_dispatch.enum_name("State.PREEMPTED"), "PREEMPTED")
        self.assertEqual(tpu_dispatch.normalize_state("failed"), "FAILED")

    def test_startup_script_streams_and_uploads_node_logs(self):
        script = tpu_dispatch.build_startup_script()

        self.assertIn("WORKLOAD_LOG_FILE=/var/log/monsoon-tpu-workload.log", script)
        self.assertIn("metadata node-workload-log-path", script)
        self.assertIn("-e GENCAST_ZARR_MIRROR_TARGET=\"$GENCAST_ZARR_MIRROR_TARGET\"", script)
        self.assertNotIn("apt-get install -y gcsfuse", script)
        self.assertNotIn("-v \"${COMMON_BUCKET_MOUNT}:${COMMON_BUCKET_MOUNT}\"", script)
        self.assertIn("monsoon-tpu-vm", script)
        self.assertIn("worker_hostname", script)
        self.assertIn('workload-stdout "$WORKLOAD_LOG_FILE"', script)
        self.assertIn('workload-stderr "$WORKLOAD_LOG_FILE"', script)
        self.assertNotIn("logger -t monsoon-tpu-workload", script)
        self.assertIn("upload_logs || true", script)
        self.assertIn(
            '"workload_log_uri": os.environ["NODE_WORKLOAD_LOG_URI"]', script
        )

    def test_attempt_node_log_paths_include_hostname_placeholder(self):
        config = make_config()

        self.assertEqual(
            config.attempt_node_workload_log_path(2),
            "intermediate/tpu-dispatch/gencast/20260515T00/"
            "gencast-20260515-00/attempts/2/logs/__HOSTNAME__.log",
        )
        self.assertEqual(
            config.attempt_node_startup_log_path(2),
            "intermediate/tpu-dispatch/gencast/20260515T00/"
            "gencast-20260515-00/attempts/2/logs/__HOSTNAME__-startup.log",
        )

    def test_startup_metadata_includes_node_log_paths(self):
        config = make_config()
        controller = tpu_dispatch.Controller(
            config,
            FakeTpuClient(),
            FakeStatusStore(),
            sleep=lambda _seconds: None,
            now=lambda: dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
        )

        metadata = controller.startup_metadata(
            2,
            "gencast-20260515-00-a2",
            "node",
        )

        self.assertEqual(
            metadata["node-workload-log-path"],
            config.attempt_node_workload_log_path(2),
        )
        self.assertEqual(
            metadata["node-startup-log-path"],
            config.attempt_node_startup_log_path(2),
        )

    def test_workload_failure_retry_decision_defaults_to_non_retryable(self):
        config = make_config(retry_on_workload_failure=False)
        status = FakeStatusStore(
            listed={
                f"{config.status_prefix}/attempts/1/nodes/": {
                    "node-a": {
                        "state": "FAILED",
                        "exit_code": 2,
                        "workload_log_uri": "gs://common/logs/node-a.log",
                    },
                }
            }
        )
        controller = tpu_dispatch.Controller(
            config,
            FakeTpuClient(),
            status,
            sleep=lambda _seconds: None,
            now=lambda: dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
        )

        result = controller.wait_for_attempt(1, "gencast-20260515-00-a1", "node")

        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertIn("exit_code=2", result.message)
        self.assertIn("gs://common/logs/node-a.log", result.message)

    def test_node_failure_with_different_queued_resource_is_allowed(self):
        config = make_config()
        status = FakeStatusStore(
            listed={
                f"{config.status_prefix}/attempts/1/nodes/": {
                    "node": {
                        "state": "FAILED",
                        "exit_code": 1,
                        "queued_resource_id": "tpu-api-generated-id",
                        "updated_at": "2026-05-15T01:01:00+00:00",
                    },
                }
            }
        )
        controller = tpu_dispatch.Controller(
            config,
            FakeTpuClient(),
            status,
            sleep=lambda _seconds: None,
            now=lambda: dt.datetime(2026, 5, 15, 1, 0, 0, tzinfo=dt.UTC),
        )

        result = controller.node_failure_status(
            1,
            "gencast-20260515-00-a1",
            dt.datetime(2026, 5, 15, 1, 0, 0, tzinfo=dt.UTC),
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["exit_code"], 1)

    def test_stale_node_failure_before_attempt_start_is_ignored(self):
        config = make_config()
        status = FakeStatusStore(
            listed={
                f"{config.status_prefix}/attempts/1/nodes/": {
                    "old-node": {
                        "state": "FAILED",
                        "exit_code": 1,
                        "queued_resource_id": "gencast-20260515-00-a1",
                        "updated_at": "2026-05-15T00:00:00+00:00",
                    },
                }
            }
        )
        controller = tpu_dispatch.Controller(
            config,
            FakeTpuClient(),
            status,
            sleep=lambda _seconds: None,
            now=lambda: dt.datetime(2026, 5, 15, 1, 0, 0, tzinfo=dt.UTC),
        )

        result = controller.node_failure_status(
            1,
            "gencast-20260515-00-a1",
            dt.datetime(2026, 5, 15, 1, 0, 0, tzinfo=dt.UTC),
        )

        self.assertIsNone(result)

    def test_completion_markers_report_success(self):
        config = make_config(forecast_regions='["ethiopia", "india"]')
        status = FakeStatusStore(
            existing_paths={
                "intermediate/gencast_ethiopia_20260515T00_done",
                "intermediate/gencast_india_20260515T00_done",
            }
        )
        controller = tpu_dispatch.Controller(
            config,
            FakeTpuClient(),
            status,
            sleep=lambda _seconds: None,
            now=lambda: dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
        )

        result = controller.wait_for_attempt(1, "gencast-20260515-00-a1", "node")

        self.assertTrue(result.success)

    def test_queue_timeout_is_retryable(self):
        times = [
            dt.datetime(2026, 5, 15, 0, 0, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 5, 15, 0, 0, 2, tzinfo=dt.UTC),
        ]
        config = make_config(queue_timeout_seconds=1)
        controller = tpu_dispatch.Controller(
            config,
            FakeTpuClient(states=["CREATING"]),
            FakeStatusStore(reads=[None]),
            sleep=lambda _seconds: None,
            now=lambda: times.pop(0)
            if times
            else dt.datetime(2026, 5, 15, 0, 0, 3, tzinfo=dt.UTC),
        )

        result = controller.wait_for_attempt(1, "gencast-20260515-00-a1", "node")

        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertIn("allocation", result.message)

    def test_run_attempt_deletes_queued_resource_after_create_exception(self):
        config = make_config()
        tpu = FakeTpuClient(create_error=RuntimeError("create failed"))
        controller = tpu_dispatch.Controller(
            config,
            tpu,
            FakeStatusStore(),
            sleep=lambda _seconds: None,
            now=lambda: dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
        )

        result = controller.run_attempt(1)

        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertEqual(tpu.deleted, [("gencast-20260515-00-a1", True)])

    def test_run_attempt_waits_before_delete_after_success(self):
        config = make_config(success_grace_seconds=5)
        status = FakeStatusStore(
            existing_paths={"intermediate/gencast_ethiopia_20260515T00_done"}
        )
        tpu = FakeTpuClient()
        sleeps = []
        controller = tpu_dispatch.Controller(
            config,
            tpu,
            status,
            sleep=sleeps.append,
            now=lambda: dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
        )

        result = controller.run_attempt(1)

        self.assertTrue(result.success)
        self.assertIn(5, sleeps)
        self.assertEqual(tpu.deleted, [("gencast-20260515-00-a1", True)])

    def test_interrupted_cleanup_does_not_wait_for_delete(self):
        config = make_config()
        tpu = FakeTpuClient(create_error=tpu_dispatch.ControllerInterrupted("stop"))
        controller = tpu_dispatch.Controller(
            config,
            tpu,
            FakeStatusStore(),
            sleep=lambda _seconds: None,
            now=lambda: dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
        )

        with self.assertRaises(tpu_dispatch.ControllerInterrupted):
            controller.run_attempt(1)

        self.assertEqual(tpu.deleted, [("gencast-20260515-00-a1", False)])


if __name__ == "__main__":
    unittest.main()
