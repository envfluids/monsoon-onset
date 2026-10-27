import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts/bump_drywetcast.py"

OLD_COMMIT = "cb08f146bc616f2682e717f1934bc7bc232b1034"
OLD_SHA256 = "f687b6f816dd59c9abb2fac9e83ffcc72f0685664622581be4a0e3ea7bbe6bc9"
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


class DockerfileEditTest(unittest.TestCase):
    def setUp(self):
        self.bump = load_module()
        self.dockerfile = (REPO_ROOT / "docker/drywetcast/Dockerfile").read_text()

    def test_reads_the_current_pin(self):
        self.assertEqual(
            self.bump.pinned_values(self.dockerfile), (OLD_COMMIT, OLD_SHA256)
        )

    def test_replaces_only_the_two_arg_lines(self):
        updated = self.bump.update_dockerfile(self.dockerfile, NEW_COMMIT, NEW_SHA256)

        self.assertEqual(self.bump.pinned_values(updated), (NEW_COMMIT, NEW_SHA256))
        old_lines, new_lines = self.dockerfile.splitlines(), updated.splitlines()
        self.assertEqual(len(old_lines), len(new_lines))
        changed = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
        self.assertEqual(
            changed,
            [
                (
                    f"ARG DRYWETCAST_COMMIT={OLD_COMMIT}",
                    f"ARG DRYWETCAST_COMMIT={NEW_COMMIT}",
                ),
                (
                    f"ARG DRYWETCAST_SHA256={OLD_SHA256}",
                    f"ARG DRYWETCAST_SHA256={NEW_SHA256}",
                ),
            ],
        )
        # The download and checksum lines use the ARGs, so they follow the bump.
        self.assertIn("archive/${DRYWETCAST_COMMIT}.tar.gz", updated)
        self.assertIn(
            'echo "${DRYWETCAST_SHA256}  /tmp/drywetcast.tar.gz" | sha256sum -c -',
            updated,
        )

    def test_is_idempotent(self):
        once = self.bump.update_dockerfile(self.dockerfile, NEW_COMMIT, NEW_SHA256)
        self.assertEqual(
            self.bump.update_dockerfile(once, NEW_COMMIT, NEW_SHA256), once
        )

    def test_rejects_short_or_malformed_values(self):
        for commit, sha256 in (
            ("22cdbe5", NEW_SHA256),
            (NEW_COMMIT.upper(), NEW_SHA256),
            (NEW_COMMIT, "ab" * 31),
            (NEW_COMMIT, "zz" * 32),
        ):
            with (
                self.subTest(commit=commit, sha256=sha256),
                self.assertRaises(self.bump.BumpError),
            ):
                self.bump.update_dockerfile(self.dockerfile, commit, sha256)

    def test_refuses_a_missing_or_repeated_arg(self):
        missing = self.dockerfile.replace(f"ARG DRYWETCAST_SHA256={OLD_SHA256}\n", "")
        repeated = self.dockerfile.replace(
            f"ARG DRYWETCAST_COMMIT={OLD_COMMIT}\n",
            f"ARG DRYWETCAST_COMMIT={OLD_COMMIT}\nARG DRYWETCAST_COMMIT={OLD_COMMIT}\n",
        )
        # A commented-out or indented line isn't the ARG.
        commented = self.dockerfile.replace(
            f"ARG DRYWETCAST_COMMIT={OLD_COMMIT}",
            f"# ARG DRYWETCAST_COMMIT={OLD_COMMIT}",
        )
        for name, text in (
            ("missing", missing),
            ("repeated", repeated),
            ("commented", commented),
        ):
            with self.subTest(name), self.assertRaises(self.bump.BumpError):
                self.bump.update_dockerfile(text, NEW_COMMIT, NEW_SHA256)

    def test_reads_the_commit_requirements_in_was_derived_from(self):
        text = (REPO_ROOT / "docker/drywetcast/requirements.in").read_text()
        self.assertEqual(self.bump.derived_from_commit(text), REQUIREMENTS_BASE)
        with self.assertRaises(self.bump.BumpError):
            self.bump.derived_from_commit("numpy==2.4.6\n")


class BumpMainTest(unittest.TestCase):
    """main() end to end with GitHub stubbed out and a temporary Dockerfile."""

    def setUp(self):
        self.bump = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dockerfile = Path(self.tmp.name) / "docker/drywetcast/Dockerfile"
        self.dockerfile.parent.mkdir(parents=True)
        self.original = (REPO_ROOT / "docker/drywetcast/Dockerfile").read_text()
        self.dockerfile.write_text(self.original)
        requirements_in = self.dockerfile.parent / "requirements.in"
        requirements_in.write_text(
            (REPO_ROOT / "docker/drywetcast/requirements.in").read_text()
        )
        self.requirements = {OLD_COMMIT: "numpy>=2\n", NEW_COMMIT: "numpy>=2\n"}
        for name, value in (
            ("REPO_ROOT", Path(self.tmp.name)),
            ("DOCKERFILE", self.dockerfile),
            ("REQUIREMENTS_IN", requirements_in),
            ("resolve_commit", lambda ref: NEW_COMMIT if ref == "v2" else OLD_COMMIT),
            ("tarball_sha256", lambda commit: NEW_SHA256),
            ("requirements_txt", lambda commit: self.requirements[commit]),
            ("files_mentioning", lambda short: ["README.md"]),
        ):
            patcher = mock.patch.object(self.bump, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.bump.main(list(argv))
        return code, out.getvalue()

    def test_writes_the_new_pin_and_prints_the_diff(self):
        code, out = self.run_main("v2")

        self.assertEqual(code, 0)
        self.assertEqual(
            self.bump.pinned_values(self.dockerfile.read_text()),
            (NEW_COMMIT, NEW_SHA256),
        )
        self.assertIn(f"-ARG DRYWETCAST_COMMIT={OLD_COMMIT}", out)
        self.assertIn(f"+ARG DRYWETCAST_COMMIT={NEW_COMMIT}", out)
        self.assertIn(f"+ARG DRYWETCAST_SHA256={NEW_SHA256}", out)
        self.assertIn("requirements.txt is unchanged", out)
        self.assertNotIn("WARNING", out)
        self.assertIn("README.md", out)

    def test_dry_run_leaves_the_dockerfile_alone(self):
        code, out = self.run_main("v2", "--dry-run")

        self.assertEqual(code, 0)
        self.assertEqual(self.dockerfile.read_text(), self.original)
        self.assertIn(f"+ARG DRYWETCAST_COMMIT={NEW_COMMIT}", out)

    def test_warns_when_requirements_changed(self):
        self.requirements[NEW_COMMIT] = "numpy>=2\nseaborn>=0.13\n"

        code, out = self.run_main("v2")

        self.assertEqual(code, 0)
        self.assertIn("WARNING: requirements.txt differs from 22cdbe5", out)
        self.assertIn("+seaborn>=0.13", out)

    def test_same_commit_is_a_no_op(self):
        code, out = self.run_main(OLD_COMMIT[:7])

        self.assertEqual(code, 0)
        self.assertIn("Already pinned", out)
        self.assertEqual(self.dockerfile.read_text(), self.original)


if __name__ == "__main__":
    unittest.main()
