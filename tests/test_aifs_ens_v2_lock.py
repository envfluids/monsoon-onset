"""The aifs-ens-v2 image rebuilds to what runs today.

The Dockerfile starts from a digest-pinned NVIDIA PyTorch base and installs an
exact requirements.lock with pip --no-deps. Unlike the CPU images, the lock lists
only what pip adds to (or replaces in) the base; everything else, torch and numpy
included, comes from the base, whose packages are recorded in base-packages.txt.
"""

import re
import unittest
from pathlib import Path

import yaml
from packaging.utils import canonicalize_name
from test_cpu_image_pins import direct_requirements, lock_versions

ROOT = Path(__file__).resolve().parents[1]
IMAGE = ROOT / "docker/aifs-ens-v2"
PINNED_FROM = re.compile(
    r"^FROM nvcr\.io/nvidia/pytorch:25\.08-py3@sha256:[0-9a-f]{64}$"
)
# Must come from the base: replacing them would swap NVIDIA's CUDA build for PyPI's.
BASE_ONLY = re.compile(r"^(torch|numpy|nvidia-.*)$")


class AifsEnsV2LockTest(unittest.TestCase):
    def setUp(self):
        self.locked = lock_versions((IMAGE / "requirements.lock").read_text())
        self.base = lock_versions((IMAGE / "base-packages.txt").read_text())

    def test_dockerfile_uses_a_digest_pinned_base_and_the_lock(self):
        dockerfile = (IMAGE / "Dockerfile").read_text()
        froms = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
        self.assertEqual(len(froms), 1, froms)
        self.assertRegex(froms[0], PINNED_FROM)
        self.assertIn(
            "COPY docker/aifs-ens-v2/requirements.lock .\n"
            "RUN pip install --no-cache-dir --no-deps --no-build-isolation"
            " -r requirements.lock\n",
            dockerfile,
        )
        instructions = "\n".join(
            line
            for line in dockerfile.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("requirements.txt", instructions)

    def test_lock_and_base_satisfy_requirements_txt(self):
        installed = {**self.base, **self.locked}
        for requirement in direct_requirements(
            (IMAGE / "requirements.txt").read_text()
        ):
            name = canonicalize_name(requirement.name)
            with self.subTest(requirement=str(requirement)):
                self.assertIn(name, installed, f"{name} is in neither lock nor base")
                self.assertTrue(
                    requirement.specifier.contains(installed[name], prereleases=True),
                    f"{name}=={installed[name]} is outside {requirement}",
                )

    def test_lock_only_adds_to_or_replaces_the_base(self):
        self.assertTrue(self.locked)
        for name, version in self.locked.items():
            with self.subTest(package=name):
                self.assertNotRegex(name, BASE_ONLY)
                self.assertNotEqual(
                    self.base.get(name), version, f"{name}=={version} is the base's"
                )


class AifsEnsV2VerifyStepTest(unittest.TestCase):
    """cloudbuild.yaml checks the built image before it is pushed."""

    IMPORTS = (
        "torch, numpy, anemoi.inference, anemoi.models, anemoi.transform, "
        "earthkit.data, cfunits, zarr, gcsfs, xarray, netCDF4, h5netcdf, "
        "huggingface_hub, torch_geometric"
    )

    @classmethod
    def setUpClass(cls):
        cls.cloudbuild = (ROOT / "cloudbuild.yaml").read_text()
        cls.deps = yaml.safe_load((ROOT / ".image-deps.yaml").read_text())["images"]

    def step(self, step_id):
        return self.cloudbuild.split(f"- id: {step_id}\n", 1)[1].split("- id:", 1)[0]

    def test_push_waits_for_the_checks_which_wait_for_the_build(self):
        verify = self.step("verify-aifs-ens-v2")
        packages = self.step("verify-aifs-ens-v2-packages")
        self.assertIn("waitFor: [build-aifs-ens-v2]", verify)
        self.assertIn("waitFor: [verify-aifs-ens-v2]", packages)
        self.assertIn(
            "waitFor: [verify-aifs-ens-v2-packages]", self.step("push-aifs-ens-v2")
        )
        for block in (verify, packages):
            self.assertIn(
                'grep -qx aifs-ens-v2 targets || { echo "skip"; exit 0; }', block
            )

    def test_checks_packages_and_imports_of_the_built_image(self):
        verify = self.step("verify-aifs-ens-v2")
        self.assertIn("set -e\n", verify)
        self.assertIn(
            "/monsoon-aifs-ens-v2:$$(cat code_version)\n", verify.split("py()", 1)[0]
        )
        self.assertIn(
            'py "$$image" -m pip list --format=freeze > aifs-ens-v2.freeze', verify
        )
        self.assertIn(f'py "$$image" -c "import {self.IMPORTS};', verify)
        self.assertIn(
            "python scripts/aifs_ens_v2_packages.py check aifs-ens-v2.freeze",
            self.step("verify-aifs-ens-v2-packages"),
        )
        self.assertIn("aifs-ens-v2.previous", self.step("pull-cache-aifs-ens-v2"))

    def test_changing_the_check_script_rebuilds_the_image(self):
        self.assertIn("scripts/aifs_ens_v2_packages.py", self.deps["aifs-ens-v2"])


if __name__ == "__main__":
    unittest.main()
