import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "docker/blend/src/main.py"


class FakeStorageClient:
    pass


def load_blend_wrapper():
    google_module = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud_module = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    storage_module = sys.modules.setdefault("google.cloud.storage", types.ModuleType("google.cloud.storage"))
    storage_module.Client = FakeStorageClient
    cloud_module.storage = storage_module
    google_module.cloud = cloud_module

    module_name = "blend_wrapper_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class BlendWrapperTest(unittest.TestCase):
    def setUp(self):
        self.module = load_blend_wrapper()
        self.module.REGIONS = {
            "ethiopia": {
                "models": ["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm", "gencast"],
            }
        }

    def test_parse_blend_names_accepts_json_and_csv(self):
        self.assertEqual(
            self.module._parse_blend_names('["AIFS_single_v2_gencast"]'),
            {"AIFS_single_v2_gencast"},
        )
        self.assertEqual(
            self.module._parse_blend_names("AIFS_single_v2_AIFS_ENS_v2, AIFS_single_v2_NeuralGCM"),
            {"AIFS_single_v2_AIFS_ENS_v2", "AIFS_single_v2_NeuralGCM"},
        )

    def test_selected_names_filter_blend_and_diagnostics_modes(self):
        blends = self.module._select_blends(
            "ethiopia",
            "blend",
            {"AIFS_single_v2_AIFS_ENS_v2"},
        )
        self.assertEqual([blend.name for blend in blends], ["AIFS_single_v2_AIFS_ENS_v2"])

        diagnostics = self.module._select_blends(
            "ethiopia",
            "diagnostics",
            {"AIFS_single_v2_gencast"},
        )
        self.assertEqual([blend.name for blend in diagnostics], ["AIFS_single_v2_gencast"])

    def test_gencast_is_not_available_for_blend_mode(self):
        with self.assertRaises(Exception):
            self.module._select_blends(
                "ethiopia",
                "blend",
                {"AIFS_single_v2_gencast"},
            )

    def test_ethiopia_blend_climatology_cache_preserves_repo_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self.module.REPO_ROOT = repo_root
            clim_dir = (
                repo_root
                / "blend"
                / "utils"
                / "ethiopia2026"
                / "operational"
                / "Monsoon_Data"
                / "Processed_Data"
                / "AIFS_single_v2_AIFS_ENS_v2_ICPAC"
            )
            existing = {
                "cache/blend/utils/ethiopia2026/operational/"
                "Monsoon_Data/Processed_Data/AIFS_single_v2_AIFS_ENS_v2_ICPAC/"
                "imd_clim_mok_date_clim_issue.pkl"
            }
            downloaded = []
            self.module.gcs_object_exists = lambda _bucket, path: path in existing
            self.module.download_gcs_file = lambda _bucket, path, local: downloaded.append(
                (path, local)
            )

            self.module._download_cached_climatologies(
                "common",
                "ethiopia",
                "blend",
                [types.SimpleNamespace(name="AIFS_single_v2_AIFS_ENS_v2")],
            )

            self.assertEqual(
                downloaded,
                [
                    (
                        "cache/blend/utils/ethiopia2026/operational/"
                        "Monsoon_Data/Processed_Data/AIFS_single_v2_AIFS_ENS_v2_ICPAC/"
                        "imd_clim_mok_date_clim_issue.pkl",
                        clim_dir / "imd_clim_mok_date_clim_issue.pkl",
                    )
                ],
            )

    def test_ethiopia_diagnostics_climatology_cache_preserves_repo_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self.module.REPO_ROOT = repo_root
            clim_dir = (
                repo_root
                / "model_diagnostics"
                / "data"
                / "climatology"
                / "era5_clim_africa_1990-2019.zarr"
            )
            clim_dir.mkdir(parents=True)
            (clim_dir / "zarr.json").write_text("{}", encoding="utf-8")
            self.module.ETHIOPIA_DIAGNOSTICS_CLIMATOLOGY_DIR = clim_dir
            uploaded = []
            self.module.upload_directory = lambda _bucket, local, prefix: uploaded.append(
                (local, prefix)
            )

            self.module._upload_cached_climatologies(
                "common",
                "ethiopia",
                "diagnostics",
                [types.SimpleNamespace(name="AIFS_single_v2_gencast")],
            )

            self.assertEqual(
                uploaded,
                [
                    (
                        clim_dir,
                        "cache/model_diagnostics/data/climatology/"
                        "era5_clim_africa_1990-2019.zarr",
                    )
                ],
            )


if __name__ == "__main__":
    unittest.main()
