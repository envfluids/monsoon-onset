#!/usr/bin/env python3
"""Remove Python __pycache__ directories from this repository."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ALWAYS_PRUNE_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".terraform",
    ".venv",
    "node_modules",
}

GENERATED_PRUNE_DIRS = {
    "data",
    "debug",
    "EKR",
    "forcings",
    "large",
    "latest",
    "logs",
    "model_ds",
    "output",
    "output_google",
    "raw",
    "weights",
}


def iter_pycache_dirs(root: Path, include_generated: bool):
    prune_dirs = set(ALWAYS_PRUNE_DIRS)
    if not include_generated:
        prune_dirs.update(GENERATED_PRUNE_DIRS)

    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in prune_dirs and not name.endswith(".egg-info")
        ]
        if "__pycache__" in dirnames:
            pycache = Path(dirpath) / "__pycache__"
            yield pycache
            dirnames.remove("__pycache__")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove all Python __pycache__ directories under the repo root."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List directories that would be removed without deleting them.",
    )
    parser.add_argument(
        "--include-generated",
        action="store_true",
        help=(
            "Also scan generated data/output directories. This can be slow on "
            "the operational filesystem."
        ),
    )
    args = parser.parse_args()

    pycache_dirs = sorted(iter_pycache_dirs(REPO_ROOT, args.include_generated))
    for path in pycache_dirs:
        rel_path = path.relative_to(REPO_ROOT)
        if args.dry_run:
            print(rel_path)
        else:
            try:
                shutil.rmtree(path)
            except OSError as exc:
                print(f"failed {rel_path}: {exc}")
            else:
                print(f"removed {rel_path}")

    print(f"{'would remove' if args.dry_run else 'removed'} {len(pycache_dirs)} directories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
