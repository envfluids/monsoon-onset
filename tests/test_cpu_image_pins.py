"""The daily-use CPU images rebuild to what runs today.

Each Dockerfile starts from a digest-pinned base image and installs an exact
requirements.lock with pip --no-deps; requirements.txt stays the edited list of
direct dependencies, and the lock must satisfy it.
"""

import re
import unittest
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ("downloader", "pipeline-state", "sync", "tpu-dispatch", "blend")
PINNED_FROM = re.compile(r"^FROM python:3\.11-slim@sha256:[0-9a-f]{64}$")
LOCK_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)$")


def lock_versions(text: str) -> dict[str, str]:
    """{canonical name: version} from a requirements.lock; fails on any other line."""
    versions = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK_LINE.match(line)
        if not match:
            raise AssertionError(f"line {number} is not name==version: {line!r}")
        name = canonicalize_name(match.group(1))
        if name in versions:
            raise AssertionError(f"line {number}: {name} is listed twice")
        versions[name] = match.group(2)
    return versions


def direct_requirements(text: str) -> list[Requirement]:
    return [
        Requirement(line.split("#", 1)[0].strip())
        for line in text.splitlines()
        if line.split("#", 1)[0].strip()
    ]


class CpuImagePinsTest(unittest.TestCase):
    def test_dockerfiles_use_a_digest_pinned_base_and_the_lock(self):
        for image in IMAGES:
            with self.subTest(image=image):
                dockerfile = (ROOT / f"docker/{image}/Dockerfile").read_text()
                froms = [
                    line for line in dockerfile.splitlines() if line.startswith("FROM ")
                ]
                self.assertEqual(len(froms), 1, froms)
                self.assertRegex(froms[0], PINNED_FROM)
                self.assertIn(
                    f"COPY docker/{image}/requirements.lock .\n"
                    "RUN pip install --no-cache-dir --no-deps -r requirements.lock\n",
                    dockerfile,
                )
                instructions = "\n".join(
                    line
                    for line in dockerfile.splitlines()
                    if not line.lstrip().startswith("#")
                )
                self.assertNotIn("requirements.txt", instructions)

    def test_lock_pins_every_package_and_satisfies_requirements_txt(self):
        for image in IMAGES:
            with self.subTest(image=image):
                locked = lock_versions(
                    (ROOT / f"docker/{image}/requirements.lock").read_text()
                )
                self.assertTrue(locked)
                for requirement in direct_requirements(
                    (ROOT / f"docker/{image}/requirements.txt").read_text()
                ):
                    name = canonicalize_name(requirement.name)
                    self.assertIn(name, locked, f"{name} is not in the lock")
                    self.assertTrue(
                        requirement.specifier.contains(locked[name], prereleases=True),
                        f"{name}=={locked[name]} is outside {requirement}",
                    )

    def test_lock_line_parser_rejects_ranges_and_duplicates(self):
        self.assertEqual(lock_versions("# c\nNumPy==2.4.6\n"), {"numpy": "2.4.6"})
        for text in ("numpy>=2\n", "numpy==2.4.6\nnumpy==2.4.6\n", "-e .\n"):
            with self.subTest(text=text), self.assertRaises(AssertionError):
                lock_versions(text)


if __name__ == "__main__":
    unittest.main()
