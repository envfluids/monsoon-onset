import importlib.util
import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts/drywetcast_pins.py"

LIVE_COMMIT = "f08e7c95a30542dabe91be70a21bd2350668ad85"
LIVE_SHA256 = "cad0f188d6ca1c740e44b0dad6a9ff390bb1005d3e810d84774b4fb74a65aaa1"
# cb08f14 ran in production from 2026-10-27; the 20261105T00 manifest reports this commit.
FALLBACK_COMMIT = "cb08f146bc616f2682e717f1934bc7bc232b1034"
FALLBACK_SHA256 = "f687b6f816dd59c9abb2fac9e83ffcc72f0685664622581be4a0e3ea7bbe6bc9"
NEW_COMMIT = "0123456789abcdef0123456789abcdef01234567"
NEW_SHA256 = "ab" * 32


def load_module():
    """Load scripts/drywetcast_pins.py as an isolated module under test."""
    module_name = "drywetcast_pins_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class PinsFileTest(unittest.TestCase):
    """The checked-in pins.yaml."""

    def setUp(self):
        self.pins = load_module()
        self.text = self.pins.PINS_FILE.read_text()

    def test_pins_live_and_fallback_with_no_candidate(self):
        parsed = self.pins.parse(self.text)

        self.assertEqual(parsed.repo, "pdasiitism/DryWetCast-India")
        self.assertEqual(parsed.roles["live"], self.pins.Pin(LIVE_COMMIT, LIVE_SHA256))
        self.assertEqual(
            parsed.roles["fallback"], self.pins.Pin(FALLBACK_COMMIT, FALLBACK_SHA256)
        )
        self.assertIsNone(parsed.roles["candidate"])

    def test_lock_files_exist(self):
        for role, pin in self.pins.parse(self.text).roles.items():
            if pin:
                with self.subTest(role):
                    self.assertTrue((REPO_ROOT / pin.lock).is_file())

    def test_build_args_match_the_dockerfile_args(self):
        dockerfile = (REPO_ROOT / "docker/drywetcast/Dockerfile").read_text()
        args = self.pins.build_args(self.pins.parse(self.text), "live")

        self.assertEqual(args[0::2], ["--build-arg"] * 5)
        names = [arg.split("=", 1)[0] for arg in args[1::2]]
        for name in names:
            with self.subTest(name):
                self.assertRegex(dockerfile, rf"(?m)^ARG {name}(=|$)")
        self.assertIn(f"DRYWETCAST_COMMIT={LIVE_COMMIT}", args)
        self.assertIn("DRYWETCAST_ROLE=live", args)
        # cloudbuild.yaml word-splits these, so no value may contain shell syntax.
        for arg in args:
            self.assertRegex(arg, r"^[A-Za-z0-9_./=-]+$")

    def test_unpinned_role_has_no_build_args(self):
        with self.assertRaises(self.pins.PinsError):
            self.pins.build_args(self.pins.parse(self.text), "candidate")


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.pins = load_module()
        self.text = self.pins.PINS_FILE.read_text()

    def test_rejects_malformed_files(self):
        cases = {
            "no live": self.text.replace(
                f"  live:\n    commit: {LIVE_COMMIT}\n    sha256: {LIVE_SHA256}\n"
                "    lock: docker/drywetcast/requirements.lock\n",
                "  live: null\n",
            ),
            "unknown role": self.text.replace("  candidate: null", "  canary: null"),
            "missing role": self.text.replace("  candidate: null\n", ""),
            "short commit": self.text.replace(LIVE_COMMIT, LIVE_COMMIT[:7]),
            "upper-case sha256": self.text.replace(LIVE_SHA256, LIVE_SHA256.upper()),
            "absolute lock": self.text.replace(
                "lock: docker/drywetcast/requirements.lock", "lock: /etc/passwd", 1
            ),
            "lock outside the repo": self.text.replace(
                "lock: docker/drywetcast/requirements.lock", "lock: ../x.lock", 1
            ),
            "lock with shell syntax": self.text.replace(
                "lock: docker/drywetcast/requirements.lock", "lock: a.lock;rm", 1
            ),
            "extra key": self.text.replace(
                "    lock: docker/drywetcast/requirements.lock\n",
                "    lock: docker/drywetcast/requirements.lock\n    url: x\n",
                1,
            ),
            "bad repo": self.text.replace(
                "repo: pdasiitism/DryWetCast-India", "repo: $(id)"
            ),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(self.pins.PinsError):
                self.pins.parse(text)

    def test_lock_defaults_when_omitted(self):
        text = self.text.replace(
            "    lock: docker/drywetcast/requirements.lock\n", "", 1
        )
        self.assertEqual(
            self.pins.parse(text).roles["live"].lock,
            "docker/drywetcast/requirements.lock",
        )


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.pins = load_module()
        self.text = self.pins.PINS_FILE.read_text()

    def changed_lines(self, old, new):
        """(lines only in new, lines only in old)."""
        old_lines, new_lines = old.splitlines(), new.splitlines()
        return [line for line in new_lines if line not in old_lines], [
            line for line in old_lines if line not in new_lines
        ]

    def test_replaces_only_the_role_commit_and_checksum(self):
        updated = self.pins.update(self.text, "live", NEW_COMMIT, NEW_SHA256)

        added, removed = self.changed_lines(self.text, updated)
        self.assertEqual(
            added, [f"    commit: {NEW_COMMIT}", f"    sha256: {NEW_SHA256}"]
        )
        self.assertEqual(
            removed, [f"    commit: {LIVE_COMMIT}", f"    sha256: {LIVE_SHA256}"]
        )
        self.assertEqual(len(updated.splitlines()), len(self.text.splitlines()))
        roles = self.pins.parse(updated).roles
        self.assertEqual(roles["fallback"].commit, FALLBACK_COMMIT)

    def test_pins_a_null_role_with_the_default_lock(self):
        updated = self.pins.update(self.text, "candidate", NEW_COMMIT, NEW_SHA256)

        self.assertNotIn("candidate: null", updated)
        self.assertEqual(
            self.pins.parse(updated).roles["candidate"],
            self.pins.Pin(NEW_COMMIT, NEW_SHA256),
        )
        # Everything above the candidate entry is untouched.
        self.assertTrue(updated.startswith(self.text.split("  candidate: null")[0]))

    def test_keeps_a_role_specific_lock(self):
        text = self.text.replace(
            "lock: docker/drywetcast/requirements.lock",
            "lock: docker/drywetcast/requirements-next.lock",
            1,
        )
        updated = self.pins.update(text, "live", NEW_COMMIT, NEW_SHA256)
        self.assertEqual(
            self.pins.parse(updated).roles["live"].lock,
            "docker/drywetcast/requirements-next.lock",
        )

    def test_is_idempotent(self):
        once = self.pins.update(self.text, "fallback", NEW_COMMIT, NEW_SHA256)
        self.assertEqual(
            self.pins.update(once, "fallback", NEW_COMMIT, NEW_SHA256), once
        )

    def test_rejects_bad_values_and_roles(self):
        for role, commit, sha256 in (
            ("live", "22cdbe5", NEW_SHA256),
            ("live", NEW_COMMIT.upper(), NEW_SHA256),
            ("live", NEW_COMMIT, "ab" * 31),
            ("canary", NEW_COMMIT, NEW_SHA256),
        ):
            with (
                self.subTest(role=role, commit=commit, sha256=sha256),
                self.assertRaises(self.pins.PinsError),
            ):
                self.pins.update(self.text, role, commit, sha256)


class WriteBuildArgsTest(unittest.TestCase):
    def setUp(self):
        self.pins = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def test_writes_one_file_per_pinned_role_and_drops_stale_ones(self):
        stale = self.out / "drywetcast-candidate.buildargs"
        stale.write_text("--build-arg DRYWETCAST_COMMIT=old\n")
        parsed = self.pins.parse(self.pins.PINS_FILE.read_text())

        written = self.pins.write_build_args(parsed, self.out)

        self.assertEqual(written, ["live", "fallback"])
        self.assertFalse(stale.exists())
        fallback = (self.out / "drywetcast-fallback.buildargs").read_text()
        self.assertIn(f"--build-arg DRYWETCAST_COMMIT={FALLBACK_COMMIT}", fallback)
        self.assertIn("--build-arg DRYWETCAST_ROLE=fallback", fallback)
        self.assertTrue(fallback.endswith("\n"))
        self.assertEqual(len(fallback.splitlines()), 1)

    def test_cli_prints_build_args(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.pins.main(["build-args", "live"])

        self.assertEqual(code, 0)
        self.assertTrue(
            re.fullmatch(r"(--build-arg [A-Z0-9_]+=\S+ ?){5}\n", out.getvalue())
        )


if __name__ == "__main__":
    unittest.main()
