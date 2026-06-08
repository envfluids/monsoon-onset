import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass
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
    storage_module = sys.modules.setdefault("google.cloud.storage", types.ModuleType("google.cloud.storage"))
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


if __name__ == "__main__":
    unittest.main()
