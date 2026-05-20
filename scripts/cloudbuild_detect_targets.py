#!/usr/bin/env python3
"""Compute which container images need rebuilding from a git diff.

Reads .image-deps.yaml at the repo root. Writes the resulting newline-separated
list of image names to ./targets (i.e. /workspace/targets inside Cloud Build).
Subsequent steps in cloudbuild.yaml `grep -qx <image> targets` to decide whether
to run.

Inputs (env vars):
  TARGETS    Explicit override. "all" → every image; "auto"/empty → git-diff
             mode; otherwise a comma-separated subset to validate against the
             config.
  BASE_REF   Git ref to diff HEAD against in auto mode. Defaults to HEAD~1
             which matches the squash-merge-to-main workflow.
"""
import os
import subprocess
import sys
from pathlib import Path

import yaml

CONFIG_PATH = Path(".image-deps.yaml")
TARGETS_PATH = Path("targets")


def matches(changed: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return changed.startswith(pattern)
    return changed == pattern


def changed_files(base_ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", base_ref, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff against {base_ref} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line]


def resolve_targets(config: dict, override: str) -> list[str]:
    all_images = list(config["images"])

    if override == "all":
        return all_images

    if override and override != "auto":
        requested = [t.strip() for t in override.split(",") if t.strip()]
        unknown = sorted(set(requested) - set(all_images))
        if unknown:
            raise SystemExit(
                f"unknown image(s) in TARGETS={override!r}: {unknown}; "
                f"known: {all_images}"
            )
        return requested

    base_ref = os.environ.get("BASE_REF", "HEAD~1")
    try:
        changed = changed_files(base_ref)
    except RuntimeError as exc:
        print(
            f"{exc}; rebuilding all images because the diff base is unavailable",
            file=sys.stderr,
        )
        return all_images
    print(f"changed files vs {base_ref}: {changed or 'NONE'}", file=sys.stderr)

    global_paths = config.get("global", []) or []
    for pattern in global_paths:
        if any(matches(c, pattern) for c in changed):
            print(f"global trigger: {pattern}", file=sys.stderr)
            return all_images

    matched = []
    for image, paths in config["images"].items():
        if any(matches(c, p) for c in changed for p in paths):
            matched.append(image)
    return matched


def main() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text())
    override = os.environ.get("TARGETS", "").strip()
    targets = sorted(set(resolve_targets(config, override)))
    TARGETS_PATH.write_text("\n".join(targets) + ("\n" if targets else ""))
    print(f"build targets: {targets or 'NONE'}", file=sys.stderr)


if __name__ == "__main__":
    main()
