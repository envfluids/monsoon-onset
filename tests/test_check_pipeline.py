import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/check_pipeline.py"


def load_module():
    """Load scripts/check_pipeline.py as an isolated module under test."""
    module_name = "check_pipeline_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


cp = load_module()


class DateHandlingTest(unittest.TestCase):
    def test_normalize_accepts_supported_forms(self):
        self.assertEqual(cp.normalize_date("20260604"), "20260604T00")
        self.assertEqual(cp.normalize_date("2026060412"), "20260604T12")
        self.assertEqual(cp.normalize_date("20260604T00"), "20260604T00")
        self.assertEqual(cp.normalize_date(" 20260604t06 "), "20260604T06")

    def test_normalize_rejects_garbage(self):
        with self.assertRaises(ValueError):
            cp.normalize_date("not-a-date")

    def test_expand_date_range_is_inclusive_daily(self):
        dates = cp.expand_date_range("20260601", "20260603")
        self.assertEqual(dates, ["20260601T00", "20260602T00", "20260603T00"])

    def test_expand_date_range_rejects_reversed(self):
        with self.assertRaises(ValueError):
            cp.expand_date_range("20260603", "20260601")

    def test_render_date_tokens(self):
        self.assertEqual(cp.render_date("20260604T00", "model"), "20260604T00")
        self.assertEqual(cp.render_date("20260604T00", "ymd"), "20260604")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = cp.build_registry()

    def test_every_stage_is_represented(self):
        stages = {a.stage for a in self.registry}
        self.assertEqual(stages, set(cp.STAGES))

    def test_core_groups_present(self):
        groups = {a.group for a in self.registry}
        for expected in ("AIFS_single_v2", "NeuralGCM", "gencast", "NCUM", "IMERG"):
            self.assertIn(expected, groups)

    def test_ensemble_models_have_no_india_products(self):
        for art in self.registry:
            if art.group.startswith("AIFS_ENS") and art.stage == "postprocessed":
                self.assertEqual(art.region, "ethiopia")

    def test_blend_artifacts_track_implemented_flag(self):
        gencast_blend = [
            a
            for a in self.registry
            if a.group == "AIFS_single_v2_gencast" and a.stage == "blend"
        ]
        self.assertEqual(len(gencast_blend), 1)
        # gencast blend is diagnostics-only, so its blend output is not expected.
        self.assertFalse(gencast_blend[0].expected)


class ResolutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, rel):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        return path

    def _mkdir(self, rel):
        path = self.root / rel
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_file_artifact_existence(self):
        art = cp.Artifact(
            stage="postprocessed",
            group="NeuralGCM",
            region="india",
            label="tp_0p25",
            template="NeuralGCM/output/india/tp/tp_0p25_{date}.nc",
            kind=cp.Kind.FILE,
        )
        missing = cp.resolve(art, "20260604T00", self.root)
        self.assertFalse(missing.exists)
        self._touch("NeuralGCM/output/india/tp/tp_0p25_20260604T00.nc")
        found = cp.resolve(art, "20260604T00", self.root)
        self.assertTrue(found.exists)
        self.assertEqual(len(found.matches), 1)

    def test_zarr_or_dir_matches_either_form(self):
        art = cp.Artifact(
            stage="raw",
            group="NeuralGCM",
            region=None,
            label="raw",
            template="NeuralGCM/output/raw/{date}",
            kind=cp.Kind.ZARR_OR_DIR,
        )
        self._mkdir("NeuralGCM/output/raw/20260604T00")  # legacy bare dir form
        resolved = cp.resolve(art, "20260604T00", self.root)
        self.assertTrue(resolved.exists)

    def test_glob_matches_multiple(self):
        art = cp.Artifact(
            stage="inputs",
            group="ecmwf",
            region=None,
            label="ic_grib",
            template="IC/output/ecmwf/{date}*-fc.grib2",
            kind=cp.Kind.GLOB,
            date_token="ymd",
        )
        self._touch("IC/output/ecmwf/20260604000000-0h-oper-fc.grib2")
        self._touch("IC/output/ecmwf/20260604120000-0h-oper-fc.grib2")
        resolved = cp.resolve(art, "20260604T00", self.root)
        self.assertTrue(resolved.exists)
        self.assertEqual(len(resolved.matches), 2)


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.art = cp.Artifact(
            stage="postprocessed",
            group="NeuralGCM",
            region="india",
            label="tp_0p25",
            template="x/{date}.nc",
            kind=cp.Kind.FILE,
        )
        self.shared = cp.Artifact(
            stage="inputs",
            group="ecmwf",
            region=None,
            label="ic_grib",
            template="x/{date}",
            kind=cp.Kind.GLOB,
            shared=True,
        )

    def test_empty_filters_match_all(self):
        f = cp.Filters(stages=set(), groups=set(), regions=set(), labels=set())
        self.assertTrue(f.matches(self.art))

    def test_region_filter_keeps_shared(self):
        f = cp.Filters(
            stages=set(), groups=set(), regions={"ethiopia"}, labels=set()
        )
        # region-specific india artifact excluded, but shared one kept
        self.assertFalse(f.matches(self.art))
        self.assertTrue(f.matches(self.shared))

    def test_stage_and_label_filters(self):
        f = cp.Filters(
            stages={"postprocessed"},
            groups={"NeuralGCM"},
            regions=set(),
            labels={"tp_0p25"},
        )
        self.assertTrue(f.matches(self.art))
        f2 = cp.Filters(
            stages={"raw"}, groups=set(), regions=set(), labels=set()
        )
        self.assertFalse(f2.matches(self.art))


class DeletionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_delete_path_refuses_outside_root(self):
        outside = Path(tempfile.gettempdir()) / "definitely_outside_repo_root"
        with self.assertRaises(ValueError):
            cp._delete_path(outside, self.root)

    def test_delete_path_refuses_root_itself(self):
        with self.assertRaises(ValueError):
            cp._delete_path(self.root, self.root)

    def test_delete_path_removes_file_and_dir(self):
        f = self.root / "a" / "f.nc"
        f.parent.mkdir(parents=True)
        f.write_text("x")
        cp._delete_path(f, self.root)
        self.assertFalse(f.exists())

        d = self.root / "store.zarr"
        (d / "chunk").mkdir(parents=True)
        cp._delete_path(d, self.root)
        self.assertFalse(d.exists())

    def test_run_deletion_dry_run_keeps_files(self):
        f = self.root / "x.nc"
        f.write_text("x")
        art = cp.Artifact(
            stage="raw",
            group="m",
            region=None,
            label="raw",
            template="x.nc",
            kind=cp.Kind.FILE,
        )
        resolved = [cp.Resolved(art, "20260604T00", f, [f])]
        rc = cp.run_deletion(resolved, self.root, dry_run=True, assume_yes=True)
        self.assertEqual(rc, 0)
        self.assertTrue(f.exists())

    def test_run_deletion_removes_when_confirmed(self):
        f = self.root / "x.nc"
        f.write_text("x")
        art = cp.Artifact(
            stage="raw",
            group="m",
            region=None,
            label="raw",
            template="x.nc",
            kind=cp.Kind.FILE,
        )
        resolved = [cp.Resolved(art, "20260604T00", f, [f])]
        rc = cp.run_deletion(resolved, self.root, dry_run=False, assume_yes=True)
        self.assertEqual(rc, 0)
        self.assertFalse(f.exists())

    def test_collect_delete_targets_skips_missing(self):
        present = self.root / "present.nc"
        present.write_text("x")
        art = cp.Artifact(
            stage="raw",
            group="m",
            region=None,
            label="raw",
            template="x",
            kind=cp.Kind.FILE,
        )
        missing = cp.Resolved(art, "d", self.root / "gone.nc", [])
        found = cp.Resolved(art, "d", present, [present])
        targets = cp.collect_delete_targets([missing, found])
        self.assertEqual([p for _, p in targets], [present])


if __name__ == "__main__":
    unittest.main()
