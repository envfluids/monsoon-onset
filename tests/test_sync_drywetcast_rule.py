import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY = "20261013T00"


def load_real_sync_utils():
    """The real sync/utils modules under their own package name.

    tests/test_sync_wrapper.py replaces sync.utils.* with stubs in sys.modules,
    so this loads the files again as `real_sync_utils.*`.
    """
    package = sys.modules.get("real_sync_utils")
    if package is None:
        package = types.ModuleType("real_sync_utils")
        package.__path__ = [str(ROOT / "sync/utils")]
        sys.modules["real_sync_utils"] = package
    return (
        importlib.import_module("real_sync_utils.sync_config"),
        importlib.import_module("real_sync_utils.sync_engine"),
    )


class DryWetCastRuleTest(unittest.TestCase):
    """The sync.yaml drywetcast rule, with the real config loader and engine."""

    def setUp(self):
        self.sync_config, self.sync_engine = load_real_sync_utils()
        self.root = Path(tempfile.mkdtemp())
        staged = self.root / "drywetcast/output/india" / DAY
        for config in ("gefs_reduced", "gefs_full"):
            (staged / config).mkdir(parents=True)
            for name in ("20261013_00.png", "20261013_00_outlook.png"):
                (staged / config / name).write_bytes(b"x")

    def discover(self, drive_root):
        config = self.sync_config.load_sync_config(
            ROOT / "sync/config/sync.yaml",
            sync_root=self.root,
            cluster="gcp-dev",
            drive_root=drive_root,
            inventory_path=self.root / "inventory.sqlite3",
            region="india",
        )
        engine = self.sync_engine.SyncEngine(config, None, None)
        return engine.discover(dates={DAY}, rule_names={"drywetcast"})

    def test_config_folders_map_under_the_drive_folder(self):
        # With MONSOON_DRIVE_FOLDER_ID the wrapper passes drive_root="/".
        items = self.discover("/")
        self.assertEqual(len(items), 4)
        for item in items:
            parts = [part for part in item.drive_path.split("/") if part]
            config = item.relative_path.split("/")[0]
            self.assertEqual(parts, ["drywetcast", DAY, config])
            self.assertEqual(item.rule, "drywetcast")

    def test_without_a_folder_id_it_uses_the_my_drive_path(self):
        items = self.discover(None)
        self.assertEqual(
            {item.drive_path for item in items},
            {
                f"/MO_operational_data_2026/gcp-dev/drywetcast/{DAY}/gefs_full",
                f"/MO_operational_data_2026/gcp-dev/drywetcast/{DAY}/gefs_reduced",
            },
        )


if __name__ == "__main__":
    unittest.main()
