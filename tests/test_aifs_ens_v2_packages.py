import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts/aifs_ens_v2_packages.py"

BASE = "# base\nclick==8.2.1\nnumpy==1.26.4\nPyYAML==6.0.2\n"
LOCK = "# lock\nclick==8.4.1\nanemoi-models==0.11.2\n"


def load_module():
    """Load scripts/aifs_ens_v2_packages.py as an isolated module under test."""
    module_name = "aifs_ens_v2_packages_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ExpectedPackagesTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def freeze(self, text):
        return self.mod.parse(text, "freeze")

    def test_lock_replaces_base_versions_and_adds_packages(self):
        want = self.mod.expected(BASE, LOCK)
        self.assertEqual(
            sorted(f"{n}=={v}" for n, v in want.values()),
            [
                "PyYAML==6.0.2",
                "anemoi-models==0.11.2",
                "click==8.4.1",
                "numpy==1.26.4",
            ],
        )

    def test_matching_freeze_has_no_differences(self):
        # pip prints names as installed; comparison is by canonical name.
        have = self.freeze(
            "anemoi_models==0.11.2\nclick==8.4.1\nnumpy==1.26.4\npyyaml==6.0.2\n"
        )
        self.assertEqual(self.mod.differences(self.mod.expected(BASE, LOCK), have), [])

    def test_reports_missing_unexpected_and_different(self):
        # click at the base's version: the lock's replacement didn't happen.
        have = self.freeze("click==8.2.1\nnumpy==1.26.4\nPyYAML==6.0.2\ntorch==2.9.0\n")
        self.assertEqual(
            self.mod.differences(self.mod.expected(BASE, LOCK), have),
            [
                "missing     anemoi-models==0.11.2",
                "different   click: expected 8.4.1, image has 8.2.1",
                "unexpected  torch==2.9.0",
            ],
        )

    def test_parse_rejects_other_lines_and_duplicates(self):
        for text in (
            "numpy>=1\n",
            "numpy==1\nNumPy==1\n",
            "-e .\n",
            "numpy @ file:///x\n",
        ):
            with self.subTest(text=text), self.assertRaises(self.mod.PackagesError):
                self.freeze(text)

    def test_check_command_exit_codes(self):
        want = self.mod.expected(
            self.mod.BASE_FILE.read_text(), self.mod.LOCK_FILE.read_text()
        )
        good = "".join(f"{n}=={v}\n" for n, v in want.values())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "freeze.txt"
            for text, code in ((good, 0), (good + "not-in-base==1.0\n", 1)):
                path.write_text(text)
                out = io.StringIO()
                with self.subTest(code=code), redirect_stdout(out):
                    self.assertEqual(self.mod.main(["check", str(path)]), code)

    def test_expected_command_prints_base_and_lock(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.mod.main(["expected"]), 0)
        lines = out.getvalue().splitlines()
        self.assertIn("zarr==3.1.5", lines)
        self.assertNotIn("zarr==2.18.7", lines)
        self.assertIn("torch==2.8.0a0+34c6371d24.nv25.8", lines)

    def test_versions_are_written_as_pip_prints_them(self):
        # pip list prints normalized versions; the check compares strings.
        for path in (self.mod.BASE_FILE, self.mod.LOCK_FILE):
            for name, version in self.mod.parse(path.read_text(), path.name).values():
                with self.subTest(package=name):
                    self.assertEqual(str(Version(version)), version)


if __name__ == "__main__":
    unittest.main()
