#!/usr/bin/env python3
"""Bump the DryWetCast-India commit pinned in docker/drywetcast/Dockerfile.

Usage:
  .venv/bin/python scripts/bump_drywetcast.py <commit-or-tag> [--dry-run]

Steps:
  1. Resolve the ref (short or full hash, tag, branch) to a full commit hash
     with the GitHub API.
  2. Download the same tarball the Dockerfile fetches and compute its SHA-256.
  3. Rewrite ARG DRYWETCAST_COMMIT and ARG DRYWETCAST_SHA256 (skipped with
     --dry-run) and print the Dockerfile diff.
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

REPO = "pdasiitism/DryWetCast-India"
REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "docker/drywetcast/Dockerfile"
REQUIREMENTS_IN = REPO_ROOT / "docker/drywetcast/requirements.in"

COMMIT_RE = re.compile(r"^(ARG DRYWETCAST_COMMIT=)([0-9a-f]*)$", re.MULTILINE)
SHA256_RE = re.compile(r"^(ARG DRYWETCAST_SHA256=)([0-9a-f]*)$", re.MULTILINE)
# First line of requirements.in: "# DryWetCast-India <commit> requirements (requirements.txt), ..."
DERIVED_FROM_RE = re.compile(r"^# DryWetCast-India ([0-9a-f]{7,40}) requirements\b")


class BumpError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Dockerfile editing (no network; unit-tested)
# ---------------------------------------------------------------------------


def pinned_values(text: str) -> tuple[str, str]:
    """Return (commit, sha256) from the Dockerfile text."""
    return _single(COMMIT_RE, text, "DRYWETCAST_COMMIT").group(2), _single(
        SHA256_RE, text, "DRYWETCAST_SHA256"
    ).group(2)


def update_dockerfile(text: str, commit: str, sha256: str) -> str:
    """Return `text` with the commit and tarball checksum ARGs replaced."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BumpError(f"commit must be a full 40-character hash, got {commit!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise BumpError(f"sha256 must be 64 lowercase hex characters, got {sha256!r}")
    for pattern, name in (
        (COMMIT_RE, "DRYWETCAST_COMMIT"),
        (SHA256_RE, "DRYWETCAST_SHA256"),
    ):
        _single(pattern, text, name)
    text = COMMIT_RE.sub(lambda m: m.group(1) + commit, text)
    return SHA256_RE.sub(lambda m: m.group(1) + sha256, text)


def _single(pattern: re.Pattern, text: str, name: str) -> re.Match:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise BumpError(
            f"expected exactly one 'ARG {name}=' line, found {len(matches)}"
        )
    return matches[0]


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
        "--dry-run",
        action="store_true",
        help="print the changes without writing the Dockerfile",
    )
    args = parser.parse_args(argv)

    try:
        old_text = DOCKERFILE.read_text()
        old_commit, old_sha256 = pinned_values(old_text)
        commit = resolve_commit(args.ref)
        print(f"{args.ref} -> {commit}")
        if commit == old_commit:
            print(f"Already pinned to {commit}; nothing to do.")
            return 0

        print(f"Downloading https://github.com/{REPO}/archive/{commit}.tar.gz ...")
        sha256 = tarball_sha256(commit)
        print(f"Tarball SHA-256: {sha256}")

        new_text = update_dockerfile(old_text, commit, sha256)
        rel = DOCKERFILE.relative_to(REPO_ROOT)
        print()
        print(unified_diff(old_text, new_text, f"a/{rel}", f"b/{rel}"), end="")
        if args.dry_run:
            print(f"\n--dry-run: {rel} not written.")
        else:
            DOCKERFILE.write_text(new_text)
            print(f"\nUpdated {rel}.")

        base = derived_from_commit(REQUIREMENTS_IN.read_text())
        base_full = resolve_commit(base)
        if base_full != old_commit:
            print(
                f"\nNote: requirements.in names {base}, but the Dockerfile pinned "
                f"{old_commit[:7]}; comparing against {base}."
            )
        old_reqs, new_reqs = requirements_txt(base_full), requirements_txt(commit)
        if old_reqs == new_reqs:
            print(f"\nrequirements.txt is unchanged since {base}.")
        else:
            print(
                f"\nWARNING: requirements.txt differs from {base} (which requirements.in "
                "was derived from). Update docker/drywetcast/requirements.in and "
                "requirements.lock to match, and check the import list in the Dockerfile:\n"
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

        stale = [p for p in files_mentioning(old_commit[:7]) if p != str(rel)]
        if stale:
            print(f"\nFiles that still mention {old_commit[:7]} (review by hand):")
            for path in stale:
                print(f"  {path}")
        print(f"\nOld pin: {old_commit} {old_sha256}")
        print(f"New pin: {commit} {sha256}")
        print("Nothing was committed.")
        return 0
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
