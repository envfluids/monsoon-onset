#!/usr/bin/env python3
"""Bump a DryWetCast-India commit pinned in products/drywetcast/pins.yaml.

Usage:
  .venv/bin/python scripts/bump_drywetcast.py <commit-or-tag> [--role ROLE] [--dry-run]

ROLE is live (default), fallback or candidate; see pins.yaml.

Steps:
  1. Resolve the ref (short or full hash, tag, branch) to a full commit hash
     with the GitHub API.
  2. Download the same tarball the Dockerfile fetches and compute its SHA-256.
  3. Rewrite the role's commit and sha256 in pins.yaml (skipped with --dry-run)
     and print the diff.
  4. Compare the new commit's requirements.txt with the one requirements.in was
     derived from (the commit named on its first line) and warn if they differ:
     requirements.in and requirements.lock then need updating by hand.
  5. List other files that still mention the old commit.

Nothing is committed. Set GITHUB_TOKEN to avoid the unauthenticated API rate
limit (60 requests/hour).
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drywetcast_pins

REPO = "pdasiitism/DryWetCast-India"
REPO_ROOT = Path(__file__).resolve().parents[1]
PINS_FILE = drywetcast_pins.PINS_FILE
REQUIREMENTS_IN = REPO_ROOT / "docker/drywetcast/requirements.in"
# First line of requirements.in: "# DryWetCast-India <commit> requirements (requirements.txt), ..."
DERIVED_FROM_RE = re.compile(r"^# DryWetCast-India ([0-9a-f]{7,40}) requirements\b")


class BumpError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Local helpers (no network; unit-tested)
# ---------------------------------------------------------------------------


def derived_from_commit(requirements_in: str) -> str:
    """The DryWetCast commit whose requirements.txt requirements.in was derived from."""
    first_line = requirements_in.splitlines()[0] if requirements_in else ""
    match = DERIVED_FROM_RE.match(first_line)
    if not match:
        raise BumpError(
            "requirements.in's first line doesn't name the DryWetCast commit it was "
            f"derived from: {first_line!r}"
        )
    return match.group(1)


def unified_diff(old: str, new: str, old_name: str, new_name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=old_name,
            tofile=new_name,
        )
    )


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def _get(url: str, accept: str | None = None) -> bytes:
    headers = {"User-Agent": "monsoon-onset-bump-drywetcast"}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise BumpError(f"GET {url}: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise BumpError(f"GET {url}: {exc.reason}") from exc


def resolve_commit(ref: str) -> str:
    """Full commit hash for a hash prefix, tag or branch."""
    body = _get(
        f"https://api.github.com/repos/{REPO}/commits/{ref}",
        accept="application/vnd.github+json",
    )
    sha = json.loads(body).get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise BumpError(f"GitHub returned no commit hash for {ref!r}")
    return sha


def tarball_sha256(commit: str) -> str:
    # The URL the Dockerfile downloads.
    data = _get(f"https://github.com/{REPO}/archive/{commit}.tar.gz")
    return hashlib.sha256(data).hexdigest()


def requirements_txt(commit: str) -> str:
    return _get(
        f"https://raw.githubusercontent.com/{REPO}/{commit}/requirements.txt"
    ).decode()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def files_mentioning(short_commit: str) -> list[str]:
    result = subprocess.run(
        ["git", "grep", "-l", "-F", short_commit],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.split()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "ref", help=f"commit hash (short or full), tag or branch of {REPO}"
    )
    parser.add_argument(
        "--role",
        choices=drywetcast_pins.ROLES,
        default="live",
        help="pins.yaml role to change (default: live)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the changes without writing pins.yaml",
    )
    args = parser.parse_args(argv)

    try:
        old_text = PINS_FILE.read_text()
        pins = drywetcast_pins.parse(old_text)
        if pins.repo != REPO:
            raise BumpError(f"pins.yaml names repo {pins.repo}, expected {REPO}")
        old_pin = pins.roles[args.role]
        old_commit = old_pin.commit if old_pin else None
        commit = resolve_commit(args.ref)
        print(f"{args.ref} -> {commit}")
        if commit == old_commit:
            print(f"{args.role} is already pinned to {commit}; nothing to do.")
            return 0

        print(f"Downloading https://github.com/{REPO}/archive/{commit}.tar.gz ...")
        sha256 = tarball_sha256(commit)
        print(f"Tarball SHA-256: {sha256}")

        new_text = drywetcast_pins.update(old_text, args.role, commit, sha256)
        rel = PINS_FILE.relative_to(REPO_ROOT)
        print()
        print(unified_diff(old_text, new_text, f"a/{rel}", f"b/{rel}"), end="")
        if args.dry_run:
            print(f"\n--dry-run: {rel} not written.")
        else:
            PINS_FILE.write_text(new_text)
            print(f"\nUpdated {rel}.")

        base = derived_from_commit(REQUIREMENTS_IN.read_text())
        base_full = resolve_commit(base)
        if old_commit and base_full != old_commit:
            print(
                f"\nNote: requirements.in names {base}, but {args.role} was pinned "
                f"to {old_commit[:7]}; comparing against {base}."
            )
        old_reqs, new_reqs = requirements_txt(base_full), requirements_txt(commit)
        if old_reqs == new_reqs:
            print(f"\nrequirements.txt is unchanged since {base}.")
        else:
            print(
                f"\nWARNING: requirements.txt differs from {base} (which requirements.in "
                "was derived from). Update docker/drywetcast/requirements.in and "
                "requirements.lock to match, and check the import list in the Dockerfile. "
                "If other roles must keep the current lock, give this role its own lock "
                "file in pins.yaml.\n"
            )
            print(
                unified_diff(
                    old_reqs,
                    new_reqs,
                    f"requirements.txt@{base}",
                    f"requirements.txt@{commit[:7]}",
                ),
                end="",
            )

        if old_pin:
            stale = [p for p in files_mentioning(old_commit[:7]) if p != str(rel)]
            if stale:
                print(f"\nFiles that still mention {old_commit[:7]} (review by hand):")
                for path in stale:
                    print(f"  {path}")
            print(f"\nOld {args.role} pin: {old_commit} {old_pin.sha256}")
        else:
            print(f"\n{args.role} was not pinned.")
        print(f"New {args.role} pin: {commit} {sha256}")
        print("Nothing was committed.")
        return 0
    except (BumpError, drywetcast_pins.PinsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
