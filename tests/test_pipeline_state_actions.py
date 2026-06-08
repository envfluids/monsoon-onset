import importlib.util
import json
import logging
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "docker/pipeline-state/src/main.py"
DATE = "20260605T00"
COMMON_BUCKET = "common"
INDIA_BUCKET = "india-bucket"
ETHIOPIA_BUCKET = "ethiopia-bucket"


class NotFound(Exception):
    pass


class FakeBlob:
    def __init__(self, store, bucket_name, name):
        self.store = store
        self.bucket_name = bucket_name
        self.name = name

    def exists(self):
        return self.name in self.store.objects.get(self.bucket_name, {})

    def download_as_text(self):
        try:
            return self.store.objects[self.bucket_name][self.name]
        except KeyError as exc:
            raise NotFound(self.name) from exc


class FakeBucket:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def blob(self, name):
        return FakeBlob(self.store, self.name, name)


class FakeStorageClient:
    def __init__(self):
        self.objects = {}

    def bucket(self, name):
        self.objects.setdefault(name, {})
        return FakeBucket(self, name)

    def put(self, bucket_name, path, content="x"):
        self.objects.setdefault(bucket_name, {})[path] = content

    def put_json(self, bucket_name, path, payload):
        self.put(bucket_name, path, json.dumps(payload, sort_keys=True))

    def list_blobs(self, bucket_name, prefix="", max_results=None):
        names = sorted(
            name
            for name in self.objects.get(bucket_name, {})
            if name.startswith(prefix)
        )
        if max_results is not None:
            names = names[:max_results]
        return [FakeBlob(self, bucket_name, name) for name in names]


@dataclass(frozen=True)
class ForecastInput:
    model: str
    role: str
    path_template: str


@dataclass(frozen=True)
class BlendConfig:
    region: str
    name: str
    deterministic_model: str
    ensemble_model: str
    inputs: tuple[ForecastInput, ...]
    output_dir_template: str = "blend/output/{region}/{date}/{name}"
    diagnostic_inputs: tuple[ForecastInput, ...] | None = None
    blend_implemented: bool = True
    diagnostic_plots: bool = False
    diagnostic_output_dir_template: str | None = None

    def models(self):
        return {self.deterministic_model, self.ensemble_model}


def install_import_stubs():
    google_module = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud_module = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    api_core_module = sys.modules.setdefault("google.api_core", types.ModuleType("google.api_core"))
    exceptions_module = sys.modules.setdefault(
        "google.api_core.exceptions", types.ModuleType("google.api_core.exceptions")
    )
    storage_module = sys.modules.setdefault("google.cloud.storage", types.ModuleType("google.cloud.storage"))
    exceptions_module.NotFound = NotFound
    api_core_module.exceptions = exceptions_module
    storage_module.Client = FakeStorageClient
    cloud_module.storage = storage_module
    google_module.cloud = cloud_module

    blend_module = types.ModuleType("blend")
    blend_utils_module = types.ModuleType("blend.utils")
    blend_main_module = types.ModuleType("blend.utils.main")
    blend_main_module.BLENDS = default_blends()
    blend_main_module.BlendConfig = BlendConfig
    blend_main_module.ForecastInput = ForecastInput
    sys.modules["blend"] = blend_module
    sys.modules["blend.utils"] = blend_utils_module
    sys.modules["blend.utils.main"] = blend_main_module


def load_pipeline_state():
    install_import_stubs()
    module_name = "pipeline_state_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.logger.setLevel(logging.CRITICAL)
    return module


def default_regions():
    return {
        "india": {
            "models": ["AIFS_single_v2", "neuralgcm"],
            "stages": ["model_diagnostics", "sync"],
            "sync": {"date_kind": "date"},
        },
        "ethiopia": {
            "models": ["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm", "gencast"],
            "stages": ["model_diagnostics", "sync"],
            "sync": {"date_kind": "aifs_date"},
        },
    }


def default_blends():
    return [
        BlendConfig(
            region="india",
            name="AIFS_single_v2_NeuralGCM",
            deterministic_model="AIFS_single_v2",
            ensemble_model="NeuralGCM",
            diagnostic_plots=True,
            output_dir_template="blend/output/india2026/{date}/AIFS_single_v2_NeuralGCM",
            inputs=(
                ForecastInput(
                    "AIFS_single_v2",
                    "deterministic",
                    "AIFS/output/india/AIFS_single_v2/tp/tp_2p0_{date}.nc",
                ),
                ForecastInput(
                    "NeuralGCM",
                    "ensemble",
                    "NeuralGCM/output/india/tp/tp_2p0_{date}.nc",
                ),
            ),
        ),
        BlendConfig(
            region="ethiopia",
            name="AIFS_single_v2_AIFS_ENS_v2",
            deterministic_model="AIFS_single_v2",
            ensemble_model="AIFS_ENS_v2",
            diagnostic_plots=True,
            output_dir_template="blend/output/ethiopia2026/{date}/AIFS_single_v2_AIFS_ENS_v2",
            inputs=(
                ForecastInput(
                    "AIFS_single_v2",
                    "deterministic",
                    "AIFS/output/ethiopia/AIFS_single_v2/tp/tp_0p25_{date}.nc",
                ),
                ForecastInput(
                    "AIFS_ENS_v2",
                    "ensemble",
                    "AIFS_ENS/output/ethiopia/AIFS_ENS_v2/tp/tp_0p25_{date}.nc",
                ),
            ),
        ),
        BlendConfig(
            region="ethiopia",
            name="AIFS_single_v2_NeuralGCM",
            deterministic_model="AIFS_single_v2",
            ensemble_model="NeuralGCM",
            diagnostic_plots=True,
            output_dir_template="blend/output/ethiopia2026/{date}/AIFS_single_v2_NeuralGCM",
            inputs=(
                ForecastInput(
                    "AIFS_single_v2",
                    "deterministic",
                    "AIFS/output/ethiopia/AIFS_single_v2/tp/tp_0p25_{date}.nc",
                ),
                ForecastInput(
                    "NeuralGCM",
                    "ensemble",
                    "NeuralGCM/output/ethiopia/tp/tp_2p8_{date}.nc",
                ),
            ),
        ),
        BlendConfig(
            region="ethiopia",
            name="AIFS_single_v2_gencast",
            deterministic_model="AIFS_single_v2",
            ensemble_model="gencast",
            diagnostic_plots=True,
            blend_implemented=False,
            output_dir_template="blend/output/ethiopia2026/{date}/AIFS_single_v2_gencast",
            inputs=(
                ForecastInput(
                    "AIFS_single_v2",
                    "deterministic",
                    "AIFS/output/ethiopia/AIFS_single_v2/tp/tp_0p25_{date}.nc",
                ),
                ForecastInput(
                    "gencast",
                    "ensemble",
                    "gencast/output/ethiopia/tp/tp_0p25_{date}.nc",
                ),
            ),
        ),
    ]


def workflow_blend():
    return BlendConfig(
        region="india",
        name="AIFS_single_v2_NeuralGCM",
        deterministic_model="AIFS_single_v2",
        ensemble_model="NeuralGCM",
        blend_implemented=True,
        diagnostic_plots=False,
        output_dir_template="blend/output/india2026/{date}/AIFS_single_v2_NeuralGCM",
        inputs=(
            ForecastInput(
                "AIFS_single_v2",
                "deterministic",
                "AIFS/output/india/AIFS_single_v2/tp/tp_2p0_{date}.nc",
            ),
            ForecastInput(
                "NeuralGCM",
                "ensemble",
                "NeuralGCM/output/india/tp/tp_2p0_{date}.nc",
            ),
        ),
    )


class PipelineStateActionsTest(unittest.TestCase):
    def setUp(self):
        self.module = load_pipeline_state()
        self.storage = FakeStorageClient()
        self.module._storage_client = self.storage
        self.module.GCS_COMMON_BUCKET = COMMON_BUCKET
        self.module.REGION_BUCKETS = {
            "india": INDIA_BUCKET,
            "ethiopia": ETHIOPIA_BUCKET,
        }
        self.module.REGIONS = default_regions()
        self.module.BLENDS = default_blends()
        self.today = datetime(2026, 6, 5, tzinfo=timezone.utc)

    def compute(self, date=DATE):
        return self.module.compute_state(date, 7, self.today)

    def add_ic(self, source="both"):
        if source in {"both", "ecmwf"}:
            for path in self.module.ic_ecmwf_paths(DATE):
                self.storage.put(COMMON_BUCKET, path)
            self.storage.put(COMMON_BUCKET, f"ic/gencast_sst/{DATE}/sst_{DATE}.nc")
        if source in {"both", "ncep"}:
            for path in self.module.ic_ncep_paths(DATE):
                self.storage.put(COMMON_BUCKET, path)

    def add_model_done(self, model, region):
        self.storage.put(COMMON_BUCKET, self.module.model_marker_path(model, region, DATE), "done")
        bucket = self.module.REGION_BUCKETS[region]
        if model == "AIFS_single_v2" and region == "india":
            self.storage.put(bucket, f"output/{model}/{DATE}/{model}/tp/tp_2p0_{DATE}.nc")
        elif model == "AIFS_single_v2" and region == "ethiopia":
            self.storage.put(bucket, f"output/{model}/{DATE}/{model}/tp/tp_0p25_{DATE}.nc")
        elif model == "AIFS_ENS_v2":
            self.storage.put(bucket, f"output/{model}/{DATE}/{model}/tp/tp_0p25_{DATE}.nc")
        elif model == "neuralgcm" and region == "india":
            self.storage.put(bucket, f"output/neuralgcm/{DATE}/tp/tp_2p0_{DATE}.nc")
        elif model == "neuralgcm" and region == "ethiopia":
            self.storage.put(bucket, f"output/neuralgcm/{DATE}/tp/tp_2p8_{DATE}.nc")
        elif model == "gencast":
            self.storage.put(bucket, f"output/gencast/{DATE}/tp_0p25_{DATE}.nc")

    def add_diagnostics_done(self, region):
        names = [
            blend.name
            for blend in self.module.BLENDS
            if blend.region == region and blend.diagnostic_plots
        ]
        for name in names:
            self.add_diagnostics_config_done(region, name)

    def add_diagnostics_config_done(self, region, name):
        self.storage.put(
            COMMON_BUCKET,
            self.module.diagnostics_config_marker_path(region, name, DATE),
            "done",
        )
        self.storage.put(
            self.module.REGION_BUCKETS[region],
            f"output/model_diagnostics/{DATE}/{region}/{DATE}/{name}/plot.png",
        )

    def add_blend_config_done(self, region, name):
        self.storage.put(
            COMMON_BUCKET,
            self.module.blend_config_marker_path(region, name, DATE),
            "done",
        )
        self.storage.put(
            self.module.REGION_BUCKETS[region],
            f"output/blend/{DATE}/{region}2026/{DATE}/{name}/forecast.csv",
        )

    def add_sync_state(self, region, state):
        self.storage.put(
            self.module.REGION_BUCKETS[region],
            self.module.sync_state_path(DATE),
            state["per_region"][region]["sync"]["fingerprint"],
        )

    def add_sync_item_marker(self, region, item, state="done", updated_at=None):
        marker_time = updated_at or datetime.now(timezone.utc).isoformat()
        payload = {
            "region": region,
            "date": DATE,
            "type": item["type"],
            "name": item["name"],
            "started_at": marker_time,
            "updated_at": marker_time,
        }
        self.storage.put_json(
            self.module.REGION_BUCKETS[region],
            self.module.sync_item_marker_path(DATE, item, state),
            payload,
        )

    def set_gencast_dispatch_status(self, state, updated_at=None):
        marker_time = updated_at or datetime.now(timezone.utc).isoformat()
        payload = {
            "run_id": f"gencast-{DATE.replace('T', '-')}",
            "date": DATE,
            "state": state,
            "updated_at": marker_time,
        }
        self.storage.put_json(
            COMMON_BUCKET,
            self.module.tpu_dispatch_status_path("gencast", DATE),
            payload,
        )

    def test_missing_external_ic_blocks_models_when_no_requested_date(self):
        self.module.latest_external_00z = lambda _source, _lookback_days, _today: ""

        state = self.module.compute_state("", 1, self.today)

        self.assertEqual(state["date"], "")
        self.assertIn({"type": "ic_unavailable", "source": "ecmwf"}, state["actions"]["blocked"])
        self.assertIn({"type": "ic_unavailable", "source": "ncep"}, state["actions"]["blocked"])
        self.assertEqual(state["actions"]["models_to_run"], [])

    def test_missing_cached_ic_requests_download_and_blocks_dependent_models(self):
        self.add_ic("ecmwf")

        state = self.compute()

        self.assertEqual(state["ic"]["ncep"]["present"], False)
        self.assertEqual(
            state["actions"]["ic_to_download_by_source"]["ncep"]["date"],
            DATE,
        )
        blocked = {
            (item.get("type"), item.get("model"), item.get("reason"))
            for item in state["actions"]["blocked"]
        }
        self.assertIn(("model_blocked", "neuralgcm", "ic_missing"), blocked)
        self.assertNotIn("neuralgcm", {action["model"] for action in state["actions"]["models_to_run"]})

    def test_ready_model_action_contains_only_pending_regions(self):
        self.add_ic()
        self.add_model_done("neuralgcm", "india")

        state = self.compute()

        self.assertEqual(
            state["actions"]["models_to_run_by_model"]["neuralgcm"],
            {"model": "neuralgcm", "date": DATE, "regions": ["ethiopia"]},
        )

    def test_complete_models_do_not_rerun(self):
        self.add_ic()
        for model, regions in {
            "AIFS_single_v2": ["india", "ethiopia"],
            "AIFS_ENS_v2": ["ethiopia"],
            "neuralgcm": ["india", "ethiopia"],
            "gencast": ["ethiopia"],
        }.items():
            for region in regions:
                self.add_model_done(model, region)

        state = self.compute()

        self.assertEqual(state["actions"]["models_to_run"], [])
        self.assertTrue(state["models"]["neuralgcm"]["complete"])
        self.assertTrue(state["models"]["gencast"]["complete"])

    def test_active_gencast_dispatch_suppresses_action(self):
        self.add_ic()
        self.set_gencast_dispatch_status("RUNNING")

        state = self.compute()

        self.assertEqual(
            state["actions"]["models_to_run_by_model"]["gencast"],
            {"model": "gencast", "date": "", "regions": []},
        )
        self.assertIn(
            ("model_in_progress", "gencast", "RUNNING"),
            {
                (item.get("type"), item.get("model"), item.get("state"))
                for item in state["actions"]["blocked"]
            },
        )

    def test_inactive_or_stale_gencast_dispatch_statuses_do_not_suppress_action(self):
        for dispatch_state in ("CLEANING_UP", "FAILED", "SUCCEEDED"):
            with self.subTest(dispatch_state=dispatch_state):
                self.setUp()
                self.add_ic()
                self.set_gencast_dispatch_status(dispatch_state)

                state = self.compute()

                self.assertEqual(
                    state["actions"]["models_to_run_by_model"]["gencast"],
                    {"model": "gencast", "date": DATE, "regions": ["ethiopia"]},
                )

        self.setUp()
        self.add_ic()
        stale = self.today - timedelta(hours=31)
        self.set_gencast_dispatch_status("RUNNING", stale.isoformat())

        state = self.compute()

        self.assertEqual(
            state["actions"]["models_to_run_by_model"]["gencast"],
            {"model": "gencast", "date": DATE, "regions": ["ethiopia"]},
        )

    def test_invalid_gencast_dispatch_status_json_is_ignored(self):
        self.add_ic()
        self.storage.put(
            COMMON_BUCKET,
            self.module.tpu_dispatch_status_path("gencast", DATE),
            "{not-json",
        )

        state = self.compute()

        self.assertEqual(
            state["actions"]["models_to_run_by_model"]["gencast"],
            {"model": "gencast", "date": DATE, "regions": ["ethiopia"]},
        )

    def test_diagnostics_schedules_ready_pairs_without_waiting_for_all_inputs(self):
        self.add_ic()
        self.add_model_done("AIFS_single_v2", "india")
        self.add_model_done("AIFS_single_v2", "ethiopia")
        self.add_model_done("AIFS_ENS_v2", "ethiopia")

        state = self.compute()

        blocked = {
            (item.get("type"), item.get("region"), item.get("reason"))
            for item in state["actions"]["blocked"]
        }
        self.assertIn(("model_diagnostics_blocked", "india", "inputs_missing"), blocked)
        ethiopia_diagnostics = state["per_region"]["ethiopia"]["model_diagnostics"]
        gencast_input = next(
            item
            for item in ethiopia_diagnostics["inputs"]
            if item["model"] == "gencast"
        )
        self.assertEqual(
            gencast_input["path"],
            f"gs://{ETHIOPIA_BUCKET}/output/gencast/{DATE}/tp_0p25_{DATE}.nc",
        )
        self.assertFalse(gencast_input["present"])
        self.assertEqual(
            state["actions"]["regions_to_diagnose_by_region"]["ethiopia"]["blends"],
            ["AIFS_single_v2_AIFS_ENS_v2"],
        )

    def test_diagnostics_sync_runs_per_completed_combination(self):
        self.module.REGIONS = {
            "india": {
                "models": ["AIFS_single_v2", "neuralgcm"],
                "stages": ["model_diagnostics", "sync"],
                "sync": {"date_kind": "date"},
            }
        }
        self.module.REGION_BUCKETS = {"india": INDIA_BUCKET}
        self.module.BLENDS = [default_blends()[0]]
        self.add_ic()
        self.add_model_done("AIFS_single_v2", "india")
        self.add_model_done("neuralgcm", "india")

        state = self.compute()

        self.assertEqual(
            {
                key: state["actions"]["regions_to_diagnose_by_region"]["india"][key]
                for key in ("region", "date", "blends")
            },
            {
                "region": "india",
                "date": DATE,
                "blends": ["AIFS_single_v2_NeuralGCM"],
            },
        )
        self.assertEqual(state["actions"]["regions_to_sync_by_region"]["india"]["date"], "")

        self.add_diagnostics_done("india")
        self.storage.put(INDIA_BUCKET, "latest.txt", "20260601T00")

        state = self.compute()

        self.assertEqual(state["per_region"]["india"]["sync"]["date"], DATE)
        self.assertEqual(
            state["actions"]["regions_to_sync_by_region"]["india"],
            {
                "region": "india",
                "date": DATE,
                "fingerprint": state["per_region"]["india"]["sync"]["fingerprint"],
                "items": [
                    {
                        "type": "model_diagnostics",
                        "name": "AIFS_single_v2_NeuralGCM",
                    }
                ],
            },
        )

        self.add_sync_item_marker(
            "india",
            {
                "type": "model_diagnostics",
                "name": "AIFS_single_v2_NeuralGCM",
            },
        )
        state = self.compute()
        self.assertEqual(state["actions"]["regions_to_sync_by_region"]["india"]["date"], "")

    def test_ethiopia_gencast_diagnostics_use_flat_output_path(self):
        self.module.REGIONS = {
            "ethiopia": {
                "models": ["AIFS_single_v2", "gencast"],
                "stages": ["model_diagnostics"],
            }
        }
        self.module.REGION_BUCKETS = {"ethiopia": ETHIOPIA_BUCKET}
        self.module.BLENDS = [default_blends()[3]]
        self.add_ic("ecmwf")
        self.add_model_done("AIFS_single_v2", "ethiopia")
        self.add_model_done("gencast", "ethiopia")

        state = self.compute()

        diagnostics = state["per_region"]["ethiopia"]["model_diagnostics"]
        gencast_input = next(
            item
            for item in diagnostics["inputs"]
            if item["model"] == "gencast"
        )
        self.assertEqual(
            gencast_input["path"],
            f"gs://{ETHIOPIA_BUCKET}/output/gencast/{DATE}/tp_0p25_{DATE}.nc",
        )
        self.assertTrue(gencast_input["present"])
        self.assertEqual(
            {
                key: state["actions"]["regions_to_diagnose_by_region"]["ethiopia"][key]
                for key in ("region", "date", "blends")
            },
            {
                "region": "ethiopia",
                "date": DATE,
                "blends": ["AIFS_single_v2_gencast"],
            },
        )

    def test_blend_action_waits_for_configured_inputs_and_existing_output(self):
        self.module.REGIONS = {
            "india": {
                "models": ["AIFS_single_v2", "neuralgcm"],
                "stages": ["blend", "sync"],
                "sync": {"date_kind": "date"},
            }
        }
        self.module.REGION_BUCKETS = {"india": INDIA_BUCKET}
        self.module.BLENDS = [workflow_blend()]
        self.add_ic()
        self.add_model_done("AIFS_single_v2", "india")

        state = self.compute()

        self.assertEqual(state["actions"]["regions_to_blend"], [])
        self.assertIn(
            ("blend_blocked", "india", "inputs_missing"),
            {
                (item.get("type"), item.get("region"), item.get("reason"))
                for item in state["actions"]["blocked"]
            },
        )

        self.add_model_done("neuralgcm", "india")
        state = self.compute()

        self.assertEqual(
            {
                key: state["actions"]["regions_to_blend_by_region"]["india"][key]
                for key in ("region", "date", "blends")
            },
            {
                "region": "india",
                "date": DATE,
                "blends": ["AIFS_single_v2_NeuralGCM"],
            },
        )

        self.add_blend_config_done("india", "AIFS_single_v2_NeuralGCM")
        state = self.compute()

        self.assertEqual(state["actions"]["regions_to_blend_by_region"]["india"]["date"], "")

    def test_ethiopia_blend_does_not_assume_aifs_is_ready_first(self):
        self.module.REGIONS = {
            "ethiopia": {
                "models": ["AIFS_single_v2", "AIFS_ENS_v2"],
                "stages": ["blend"],
            }
        }
        self.module.REGION_BUCKETS = {"ethiopia": ETHIOPIA_BUCKET}
        self.module.BLENDS = [default_blends()[1]]
        self.add_ic("ecmwf")
        self.add_model_done("AIFS_ENS_v2", "ethiopia")

        state = self.compute()

        self.assertEqual(
            state["actions"]["regions_to_blend_by_region"]["ethiopia"]["date"],
            "",
        )
        missing_models = {
            item["model"]
            for item in state["per_region"]["ethiopia"]["blend"]["missing"]
        }
        self.assertIn("AIFS_single_v2", missing_models)

    def test_ethiopia_blend_schedules_ready_configs_independently(self):
        self.module.REGIONS = {
            "ethiopia": {
                "models": ["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm"],
                "stages": ["blend"],
            }
        }
        self.module.REGION_BUCKETS = {"ethiopia": ETHIOPIA_BUCKET}
        self.module.BLENDS = [default_blends()[1], default_blends()[2]]
        self.add_ic()
        self.add_model_done("AIFS_single_v2", "ethiopia")
        self.add_model_done("AIFS_ENS_v2", "ethiopia")

        state = self.compute()

        self.assertEqual(
            state["actions"]["regions_to_blend_by_region"]["ethiopia"]["blends"],
            ["AIFS_single_v2_AIFS_ENS_v2"],
        )

        self.add_blend_config_done("ethiopia", "AIFS_single_v2_AIFS_ENS_v2")
        self.add_model_done("neuralgcm", "ethiopia")

        state = self.compute()

        self.assertEqual(
            state["actions"]["regions_to_blend_by_region"]["ethiopia"]["blends"],
            ["AIFS_single_v2_NeuralGCM"],
        )

    def test_sync_runs_again_after_later_same_date_diagnostics_completion(self):
        self.module.REGIONS = {
            "ethiopia": {
                "models": ["AIFS_single_v2", "AIFS_ENS_v2", "gencast"],
                "stages": ["model_diagnostics", "sync"],
                "sync": {"date_kind": "aifs_date"},
            }
        }
        self.module.REGION_BUCKETS = {"ethiopia": ETHIOPIA_BUCKET}
        self.module.BLENDS = [default_blends()[1], default_blends()[3]]
        self.add_ic("ecmwf")
        self.add_model_done("AIFS_single_v2", "ethiopia")
        self.add_model_done("gencast", "ethiopia")
        self.add_diagnostics_config_done("ethiopia", "AIFS_single_v2_gencast")

        state = self.compute()
        self.assertEqual(state["actions"]["regions_to_sync_by_region"]["ethiopia"]["date"], DATE)
        self.assertIn(
            {"type": "model_diagnostics", "name": "AIFS_single_v2_gencast"},
            state["actions"]["regions_to_sync_by_region"]["ethiopia"]["items"],
        )
        self.add_sync_state("ethiopia", state)

        state = self.compute()
        self.assertEqual(state["actions"]["regions_to_sync_by_region"]["ethiopia"]["date"], "")

        self.add_model_done("AIFS_ENS_v2", "ethiopia")
        self.add_diagnostics_config_done("ethiopia", "AIFS_single_v2_AIFS_ENS_v2")

        state = self.compute()

        self.assertEqual(state["actions"]["regions_to_sync_by_region"]["ethiopia"]["date"], DATE)
        self.assertNotIn(
            {"type": "model_diagnostics", "name": "AIFS_single_v2_gencast"},
            state["actions"]["regions_to_sync_by_region"]["ethiopia"]["items"],
        )
        self.assertIn(
            {"type": "model_diagnostics", "name": "AIFS_single_v2_AIFS_ENS_v2"},
            state["actions"]["regions_to_sync_by_region"]["ethiopia"]["items"],
        )

    def test_active_sync_item_suppresses_duplicate_sync_action(self):
        self.module.REGIONS = {
            "ethiopia": {
                "models": ["AIFS_single_v2", "AIFS_ENS_v2"],
                "stages": ["blend", "sync"],
                "sync": {"date_kind": "aifs_date"},
            }
        }
        self.module.REGION_BUCKETS = {"ethiopia": ETHIOPIA_BUCKET}
        self.module.BLENDS = [default_blends()[1]]
        self.add_ic("ecmwf")
        self.add_model_done("AIFS_single_v2", "ethiopia")
        self.add_model_done("AIFS_ENS_v2", "ethiopia")
        self.add_blend_config_done("ethiopia", "AIFS_single_v2_AIFS_ENS_v2")

        item = {"type": "blend", "name": "AIFS_single_v2_AIFS_ENS_v2"}
        self.add_sync_item_marker("ethiopia", item, state="active")

        state = self.compute()

        self.assertEqual(state["actions"]["regions_to_sync_by_region"]["ethiopia"]["date"], "")

    def test_stale_active_sync_item_allows_resubmission(self):
        self.module.REGIONS = {
            "ethiopia": {
                "models": ["AIFS_single_v2", "AIFS_ENS_v2"],
                "stages": ["blend", "sync"],
                "sync": {"date_kind": "aifs_date"},
            }
        }
        self.module.REGION_BUCKETS = {"ethiopia": ETHIOPIA_BUCKET}
        self.module.BLENDS = [default_blends()[1]]
        self.add_ic("ecmwf")
        self.add_model_done("AIFS_single_v2", "ethiopia")
        self.add_model_done("AIFS_ENS_v2", "ethiopia")
        self.add_blend_config_done("ethiopia", "AIFS_single_v2_AIFS_ENS_v2")

        stale = (self.today - timedelta(hours=7)).isoformat()
        item = {"type": "blend", "name": "AIFS_single_v2_AIFS_ENS_v2"}
        self.add_sync_item_marker("ethiopia", item, state="active", updated_at=stale)

        state = self.compute()

        self.assertEqual(state["actions"]["regions_to_sync_by_region"]["ethiopia"]["date"], DATE)
        self.assertEqual(state["actions"]["regions_to_sync_by_region"]["ethiopia"]["items"], [item])


if __name__ == "__main__":
    unittest.main()
