#!/usr/bin/env python3
"""Read and edit products/drywetcast/pins.yaml, the DryWetCast-India versions to build.

Usage:
  python scripts/drywetcast_pins.py build-args <role>
      Print the docker build arguments for one role, e.g. for a local build:
        docker build $(.venv/bin/python scripts/drywetcast_pins.py build-args live) \\
          -f docker/drywetcast/Dockerfile .
  python scripts/drywetcast_pins.py write-build-args <dir>
      Write <dir>/drywetcast-<role>.buildargs for each pinned role (used by
      cloudbuild.yaml; a role set to null gets no file, so its image isn't built).

Every value is checked before it is printed or written, so the arguments are
safe to word-split in a shell.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PINS_FILE = REPO_ROOT / "products/drywetcast/pins.yaml"
ROLES = ("live", "fallback", "candidate")
DEFAULT_LOCK = "docker/drywetcast/requirements.lock"

REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
LOCK_RE = re.compile(r"[A-Za-z0-9_./-]+")
ROLE_LINE_RE = r"^  {role}:.*$"


class PinsError(ValueError):
    pass


@dataclass(frozen=True)
class Pin:
    commit: str
    sha256: str
    lock: str = DEFAULT_LOCK


@dataclass(frozen=True)
class Pins:
    repo: str
    roles: dict[str, Pin | None]


def check_pin(commit: str, sha256: str, lock: str = DEFAULT_LOCK) -> Pin:
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise PinsError(f"commit must be a full 40-character hash, got {commit!r}")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise PinsError(f"sha256 must be 64 lowercase hex characters, got {sha256!r}")
    if (
        not isinstance(lock, str)
        or not LOCK_RE.fullmatch(lock)
        or lock.startswith("/")
        or ".." in lock.split("/")
    ):
        raise PinsError(f"lock must be a relative path in the repo, got {lock!r}")
    return Pin(commit, sha256, lock)


def parse(text: str) -> Pins:
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or set(data) != {"repo", "roles"}:
        raise PinsError("pins file must have exactly the keys 'repo' and 'roles'")
    repo = data["repo"]
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise PinsError(f"repo must be owner/name, got {repo!r}")
    roles = data["roles"]
    if not isinstance(roles, dict) or set(roles) != set(ROLES):
        raise PinsError(f"roles must be exactly {', '.join(ROLES)}")
    pins: dict[str, Pin | None] = {}
    for role in ROLES:
        entry = roles[role]
        if entry is None:
            pins[role] = None
            continue
        if (
            not isinstance(entry, dict)
            or not {"commit", "sha256"} <= set(entry)
            or (set(entry) - {"commit", "sha256", "lock"})
        ):
            raise PinsError(f"{role}: expected commit, sha256 and optional lock")
        try:
            pins[role] = check_pin(
                entry["commit"], entry["sha256"], entry.get("lock", DEFAULT_LOCK)
            )
        except PinsError as exc:
            raise PinsError(f"{role}: {exc}") from exc
    if pins["live"] is None:
        raise PinsError("live must be pinned")
    return Pins(repo, pins)


def build_args(pins: Pins, role: str) -> list[str]:
    pin = pins.roles[role]
    if pin is None:
        raise PinsError(f"{role} is not pinned")
    values = {
        "DRYWETCAST_REPO": pins.repo,
        "DRYWETCAST_COMMIT": pin.commit,
        "DRYWETCAST_SHA256": pin.sha256,
        "DRYWETCAST_LOCK": pin.lock,
        "DRYWETCAST_ROLE": role,
    }
    return [
        arg
        for name, value in values.items()
        for arg in ("--build-arg", f"{name}={value}")
    ]


def update(text: str, role: str, commit: str, sha256: str) -> str:
    """Return `text` with `role`'s entry set to the given commit and checksum.

    The role's lock is kept (the default for a role that was null). Only that
    role's lines change; comments elsewhere are kept.
    """
    if role not in ROLES:
        raise PinsError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
    current = parse(text).roles[role]
    pin = check_pin(commit, sha256, current.lock if current else DEFAULT_LOCK)

    lines = text.splitlines(keepends=True)
    starts = [
        i
        for i, line in enumerate(lines)
        if re.match(ROLE_LINE_RE.format(role=re.escape(role)), line.rstrip("\n"))
    ]
    if len(starts) != 1:
        raise PinsError(f"expected exactly one '  {role}:' line, found {len(starts)}")
    start = end = starts[0]
    while end + 1 < len(lines) and lines[end + 1].startswith("    "):
        end += 1
    entry = (
        f"  {role}:\n"
        f"    commit: {pin.commit}\n"
        f"    sha256: {pin.sha256}\n"
        f"    lock: {pin.lock}\n"
    )
    updated = "".join(lines[:start]) + entry + "".join(lines[end + 1 :])
    if parse(updated).roles[role] != pin:
        raise PinsError(f"rewriting {role} did not produce the expected entry")
    return updated


def write_build_args(pins: Pins, out_dir: Path) -> list[str]:
    """Write drywetcast-<role>.buildargs for each pinned role; return those roles."""
    written = []
    for role in ROLES:
        path = out_dir / f"drywetcast-{role}.buildargs"
        if pins.roles[role] is None:
            path.unlink(missing_ok=True)
            continue
        path.write_text(" ".join(build_args(pins, role)) + "\n")
        written.append(role)
    return written


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        pins = parse(PINS_FILE.read_text())
        if len(argv) == 2 and argv[0] == "build-args":
            if argv[1] not in ROLES:
                raise PinsError(f"unknown role {argv[1]!r}")
            print(" ".join(build_args(pins, argv[1])))
            return 0
        if len(argv) == 2 and argv[0] == "write-build-args":
            roles = write_build_args(pins, Path(argv[1]))
            for role in ROLES:
                pin = pins.roles[role]
                print(f"{role}: {pin.commit if pin else 'not pinned; not built'}")
            return 0 if roles else 1
    except PinsError as exc:
        print(f"error: {PINS_FILE.relative_to(REPO_ROOT)}: {exc}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
