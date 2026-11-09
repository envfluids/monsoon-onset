#!/usr/bin/env python3
"""Check the Python packages in a built aifs-ens-v2 image against its lock.

The image should hold exactly the base's packages (docker/aifs-ens-v2/base-packages.txt)
with the lock's replacements applied, plus the lock's additions.

Usage:
  python scripts/aifs_ens_v2_packages.py expected
      Print the expected `pip list --format=freeze` output.
  python scripts/aifs_ens_v2_packages.py check <freeze-file>
      Compare a `pip list --format=freeze` output with it; exit 1 on any
      missing, unexpected or different package (used by cloudbuild.yaml).

Standard library only, so it runs in a plain python image.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_FILE = REPO_ROOT / "docker/aifs-ens-v2/base-packages.txt"
LOCK_FILE = REPO_ROOT / "docker/aifs-ens-v2/requirements.lock"

LINE_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")


class PackagesError(ValueError):
    pass


def canonical(name: str) -> str:
    """PEP 503 name, as pip compares them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(text: str, source: str) -> dict[str, tuple[str, str]]:
    """{canonical name: (name, version)} from name==version lines; # comments allowed."""
    packages = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE_RE.fullmatch(line)
        if not match:
            raise PackagesError(f"{source}:{number}: not name==version: {line!r}")
        key = canonical(match.group(1))
        if key in packages:
            raise PackagesError(f"{source}:{number}: {key} is listed twice")
        packages[key] = (match.group(1), match.group(2))
    return packages


def expected(base_text: str, lock_text: str) -> dict[str, tuple[str, str]]:
    """The base's packages with the lock's replacements applied, plus its additions."""
    return {**parse(base_text, "base"), **parse(lock_text, "lock")}


def differences(
    want: dict[str, tuple[str, str]], have: dict[str, tuple[str, str]]
) -> list[str]:
    lines = []
    for key in sorted(want.keys() | have.keys()):
        if key not in have:
            lines.append(f"missing     {want[key][0]}=={want[key][1]}")
        elif key not in want:
            lines.append(f"unexpected  {have[key][0]}=={have[key][1]}")
        elif want[key][1] != have[key][1]:
            lines.append(
                f"different   {key}: expected {want[key][1]}, image has {have[key][1]}"
            )
    return lines


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        want = expected(BASE_FILE.read_text(), LOCK_FILE.read_text())
        if argv == ["expected"]:
            for name, version in sorted(want.values(), key=lambda nv: canonical(nv[0])):
                print(f"{name}=={version}")
            return 0
        if len(argv) == 2 and argv[0] == "check":
            have = parse(Path(argv[1]).read_text(), argv[1])
            diff = differences(want, have)
            for line in diff:
                print(line)
            if diff:
                print(
                    f"{len(diff)} package(s) differ from base-packages.txt + requirements.lock"
                )
                return 1
            print(f"packages match base-packages.txt + requirements.lock ({len(want)})")
            return 0
    except (OSError, PackagesError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
