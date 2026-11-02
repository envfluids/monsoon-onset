import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts/bump_drywetcast.py"

# The live and fallback pins in products/drywetcast/pins.yaml.
OLD_COMMIT = "cb08f146bc616f2682e717f1934bc7bc232b1034"
FALLBACK_COMMIT = "22cdbe5beccd3a776ce9e6039a1f603e3d404604"
# requirements.in was derived from this commit's requirements.txt (unchanged since).
REQUIREMENTS_BASE = "22cdbe5"
NEW_COMMIT = "0123456789abcdef0123456789abcdef01234567"
NEW_SHA256 = "ab" * 32


def load_module():
    """Load scripts/bump_drywetcast.py as an isolated module under test."""
    module_name = "bump_drywetcast_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def resolve(ref):
    """Stand-in for the GitHub lookup: v2 is the new commit, prefixes resolve."""
    if ref == "v2":
        return NEW_COMMIT
    for commit in (OLD_COMMIT, FALLBACK_COMMIT):
        if commit.startswith(ref):
            return commit
    raise AssertionError(f"unexpected ref {ref!r}")


class RequirementsBaseTest(unittest.TestCase):
    def test_reads_the_commit_requirements_in_was_derived_from(self):
        bump = load_module()
        text = (REPO_ROOT / "docker/drywetcast/requirements.in").read_text()
        self.assertEqual(bump.derived_from_commit(text), REQUIREMENTS_BASE)
        with self.assertRaises(bump.BumpError):
            bump.derived_from_commit("numpy==2.4.6\n")


class BumpMainTest(unittest.TestCase):
    """main() end to end with GitHub stubbed out and a temporary pins.yaml."""

    def setUp(self):
        self.bump = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.pins_file = root / "products/drywetcast/pins.yaml"
        self.pins_file.parent.mkdir(parents=True)
        self.original = (REPO_ROOT / "products/drywetcast/pins.yaml").read_text()
        self.pins_file.write_text(self.original)
        requirements_in = root / "docker/drywetcast/requirements.in"
        requirements_in.parent.mkdir(parents=True)
        requirements_in.write_text(
            (REPO_ROOT / "docker/drywetcast/requirements.in").read_text()
        )
        self.requirements = {
            OLD_COMMIT: "numpy>=2\n",
            NEW_COMMIT: "numpy>=2\n",
            FALLBACK_COMMIT: "numpy>=2\n",
        }
        self.mentions = mock.Mock(return_value=["README.md"])
        for name, value in (
            ("REPO_ROOT", root),
            ("PINS_FILE", self.pins_file),
            ("REQUIREMENTS_IN", requirements_in),
            ("resolve_commit", resolve),
            ("tarball_sha256", lambda commit: NEW_SHA256),
            ("requirements_txt", lambda commit: self.requirements[commit]),
            ("files_mentioning", self.mentions),
        ):
            patcher = mock.patch.object(self.bump, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.bump.main(list(argv))
        return code, out.getvalue()

    def roles(self):
        return self.bump.drywetcast_pins.parse(self.pins_file.read_text()).roles

    def test_writes_the_new_live_pin_and_prints_the_diff(self):
        code, out = self.run_main("v2")

        self.assertEqual(code, 0)
        roles = self.roles()
        self.assertEqual(
            (roles["live"].commit, roles["live"].sha256), (NEW_COMMIT, NEW_SHA256)
        )
        self.assertEqual(roles["fallback"].commit, FALLBACK_COMMIT)
        self.assertIsNone(roles["candidate"])
        self.assertIn(f"-    commit: {OLD_COMMIT}", out)
        self.assertIn(f"+    commit: {NEW_COMMIT}", out)
        self.assertIn(f"+    sha256: {NEW_SHA256}", out)
        self.assertIn("requirements.txt is unchanged", out)
        self.assertNotIn("WARNING", out)
        self.assertIn("README.md", out)
        self.mentions.assert_called_once_with(OLD_COMMIT[:7])

    def test_pins_a_candidate_that_was_null(self):
        code, out = self.run_main("v2", "--role", "candidate")

        self.assertEqual(code, 0)
        roles = self.roles()
        self.assertEqual(roles["candidate"].commit, NEW_COMMIT)
        self.assertEqual(roles["candidate"].lock, "docker/drywetcast/requirements.lock")
        self.assertEqual(roles["live"].commit, OLD_COMMIT)
        self.assertIn("-  candidate: null", out)
        self.assertIn("candidate was not pinned", out)
        self.mentions.assert_not_called()

    def test_dry_run_leaves_pins_alone(self):
        code, out = self.run_main("v2", "--dry-run")

        self.assertEqual(code, 0)
        self.assertEqual(self.pins_file.read_text(), self.original)
        self.assertIn(f"+    commit: {NEW_COMMIT}", out)

    def test_warns_when_requirements_changed(self):
        self.requirements[NEW_COMMIT] = "numpy>=2\nseaborn>=0.13\n"

        code, out = self.run_main("v2")

        self.assertEqual(code, 0)
        self.assertIn("WARNING: requirements.txt differs from 22cdbe5", out)
        self.assertIn("+seaborn>=0.13", out)

    def test_same_commit_is_a_no_op(self):
        code, out = self.run_main(FALLBACK_COMMIT[:7], "--role", "fallback")

        self.assertEqual(code, 0)
        self.assertIn("fallback is already pinned", out)
        self.assertEqual(self.pins_file.read_text(), self.original)

    def test_refuses_a_pins_file_for_another_repo(self):
        self.pins_file.write_text(
            self.original.replace(
                "repo: pdasiitism/DryWetCast-India", "repo: someone/fork"
            )
        )

        with redirect_stderr(io.StringIO()) as err:
            code, _ = self.run_main("v2")

        self.assertEqual(code, 1)
        self.assertIn("someone/fork", err.getvalue())


if __name__ == "__main__":
    unittest.main()
