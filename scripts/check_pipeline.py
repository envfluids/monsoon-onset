#!/usr/bin/env python3
"""Audit (and optionally clean) operational pipeline outputs on the HPC.

This is a debugging utility for the monsoon-onset operational workflow. For a
set of forecast dates it discovers which expected artifacts exist at each stage
of the pipeline -- downloaded inputs, raw model output, per-region
post-processed products, blend output, and model diagnostics -- so a user can
quickly see *where* a pipeline run went wrong. It can then delete the discovered
artifacts with fine granularity (by stage, model/blend, region, product, and
date) so the failed portion can be re-run cleanly.

The artifact patterns are static and mirror the layout documented in
``README.md``. Blend and diagnostic outputs are read from the single source of
truth, ``blend.utils.main.BLENDS``, so this tool stays in sync with the blend
dispatcher automatically.

Discovery is the default; nothing is ever deleted unless ``--delete`` is passed,
and even then the targets are listed and confirmed first (use ``--yes`` to skip
the prompt or ``--dry-run`` to only preview).

Examples:
    # What exists for one date, across everything?
    python scripts/check_pipeline.py --date 20260604T00

    # Only show the gaps for a range of dates.
    python scripts/check_pipeline.py --start-date 20260601 --end-date 20260604 \
        --missing-only

    # Inspect one model's products in one region.
    python scripts/check_pipeline.py --date 20260604T00 --model AIFS_single_v2 \
        --region india --stage postprocessed

    # Delete just the raw NeuralGCM output for a date so it can be re-run.
    python scripts/check_pipeline.py --date 20260604T00 --model NeuralGCM \
        --stage raw --delete

    # Delete every artifact for a bad date (with confirmation).
    python scripts/check_pipeline.py --date 20260604T00 --delete
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blend.utils.main import BLENDS  # noqa: E402  (needs REPO_ROOT on sys.path)

STAGES = ("inputs", "raw", "postprocessed", "blend", "diagnostics")
REGIONS = ("india", "ethiopia")
DATE_CANONICAL_RE = re.compile(r"^\d{8}T\d{2}$")


class Kind(str, Enum):
    """How an artifact lives on disk, which drives existence checks and removal."""

    FILE = "file"
    ZARR = "zarr"  # directory-backed store; treated as a directory tree
    DIR = "dir"  # plain directory tree
    ZARR_OR_DIR = "zarr_or_dir"  # ``{date}.zarr`` or a bare ``{date}`` directory
    GLOB = "glob"  # a wildcard pattern that may match zero or more paths


@dataclass(frozen=True)
class Artifact:
    """A single expected pipeline output, described by a static path template.

    Attributes:
        stage: Pipeline stage; one of :data:`STAGES`.
        group: Logical owner used for filtering -- a model name, an observation
            product (e.g. ``IMERG``), a shared input source (``ecmwf``/``ncep``),
            or a blend name.
        region: ``india``/``ethiopia`` or ``None`` for region-agnostic artifacts.
        label: Short product label (e.g. ``tp_0p25``, ``raw``, ``blend``).
        template: Path relative to the repo root containing a ``{date}`` field.
        kind: How the artifact is stored (see :class:`Kind`).
        date_token: ``model`` keeps the canonical ``YYYYMMDDTHH`` date; ``ymd``
            renders only the ``YYYYMMDD`` calendar day.
        shared: ``True`` when one file is shared by several models, so deleting
            it affects more than the selected model.
        expected: ``True`` when absence is a genuine gap. Disabled/diagnostics-
            only blends set this to ``False`` so they are not reported as broken.
    """

    stage: str
    group: str
    region: str | None
    label: str
    template: str
    kind: Kind
    date_token: str = "model"
    shared: bool = False
    expected: bool = True


@dataclass
class Resolved:
    """An artifact resolved against a concrete date and repository root."""

    artifact: Artifact
    date: str
    candidate: Path  # the canonical expected path, shown even when missing
    matches: list[Path]  # existing paths (one for files, many for globs)

    @property
    def exists(self) -> bool:
        return bool(self.matches)


# --------------------------------------------------------------------------- #
# Static product tables (mirror README.md "Model and Product Paths").
# --------------------------------------------------------------------------- #

# Per-region post-processed product labels -> filename template within the
# model's region directory.
_PRODUCT_FILES = {
    "sji": "sji/sji_{date}.nc",
    "tcw": "tcw/tcw_{date}.nc",
    "tp_2p0": "tp/tp_2p0_{date}.nc",
    "tp_2p8": "tp/tp_2p8_{date}.nc",
    "tp_0p25": "tp/tp_0p25_{date}.nc",
}

# Which product labels each region produces, by model family.
_AIFS_PRODUCTS = {
    "india": ("sji", "tcw", "tp_2p0", "tp_0p25"),
    "ethiopia": ("tp_0p25",),
}
_NEURALGCM_PRODUCTS = {
    "india": ("sji", "tcw", "tp_2p0", "tp_0p25"),
    "ethiopia": ("tp_2p8",),
}
_GENCAST_PRODUCTS = {
    "ethiopia": ("tp_0p25",),
}

AIFS_MODELS = (
    "AIFS_single_v1p1",
    "AIFS_single_v2",
    "AIFS_ENS_v1",
    "AIFS_ENS_v2",
)


def _is_ensemble(model: str) -> bool:
    return "ENS" in model


def _build_aifs_artifacts() -> list[Artifact]:
    """Build input/raw/post-processed artifacts for every AIFS model."""
    artifacts: list[Artifact] = []
    for model in AIFS_MODELS:
        ensemble = _is_ensemble(model)
        # Deterministic single models serve both regions; ensembles serve only
        # Ethiopia (see config/models.json and the configured blends).
        regions = ("ethiopia",) if ensemble else ("india", "ethiopia")

        raw_suffix = "zarr" if ensemble else "nc"
        artifacts.append(
            Artifact(
                stage="raw",
                group=model,
                region=None,
                label="raw",
                template=f"AIFS/output/raw/{model}/init_{{date}}.{raw_suffix}",
                kind=Kind.ZARR if ensemble else Kind.FILE,
            )
        )
        for region in regions:
            for label in _AIFS_PRODUCTS[region]:
                artifacts.append(
                    Artifact(
                        stage="postprocessed",
                        group=model,
                        region=region,
                        label=label,
                        template=(
                            f"AIFS/output/{region}/{model}/"
                            + _PRODUCT_FILES[label]
                        ),
                        kind=Kind.FILE,
                    )
                )
    return artifacts


def _build_neuralgcm_artifacts() -> list[Artifact]:
    artifacts = [
        Artifact(
            stage="inputs",
            group="NeuralGCM",
            region=None,
            label="processed_ic",
            template="NeuralGCM/raw/ncep_ic/processed/gdas_{date}.nc",
            kind=Kind.FILE,
        ),
        Artifact(
            stage="raw",
            group="NeuralGCM",
            region=None,
            label="raw",
            template="NeuralGCM/output/raw/{date}",
            kind=Kind.ZARR_OR_DIR,
        ),
    ]
    for region, labels in _NEURALGCM_PRODUCTS.items():
        for label in labels:
            artifacts.append(
                Artifact(
                    stage="postprocessed",
                    group="NeuralGCM",
                    region=region,
                    label=label,
                    template=f"NeuralGCM/output/{region}/" + _PRODUCT_FILES[label],
                    kind=Kind.FILE,
                )
            )
    return artifacts


def _build_gencast_artifacts() -> list[Artifact]:
    artifacts = [
        Artifact(
            stage="inputs",
            group="gencast",
            region=None,
            label="sst_ecmwf",
            template="IC/output/ecmwf/sst_{date}.nc",
            kind=Kind.FILE,
        ),
        Artifact(
            stage="inputs",
            group="gencast",
            region=None,
            label="sst_ic",
            template="gencast/raw/sst_ic/sst_{date}.nc",
            kind=Kind.FILE,
        ),
        Artifact(
            stage="raw",
            group="gencast",
            region=None,
            label="raw",
            template="gencast/raw/output/init_{date}.zarr",
            kind=Kind.ZARR,
        ),
    ]
    for region, labels in _GENCAST_PRODUCTS.items():
        for label in labels:
            artifacts.append(
                Artifact(
                    stage="postprocessed",
                    group="gencast",
                    region=region,
                    label=label,
                    template=f"gencast/output/{region}/" + _PRODUCT_FILES[label],
                    kind=Kind.FILE,
                )
            )
    return artifacts


def _build_shared_input_artifacts() -> list[Artifact]:
    """Downloaded initial conditions shared across all models of a source."""
    return [
        Artifact(
            stage="inputs",
            group="ecmwf",
            region=None,
            label="ic_grib",
            template="IC/output/ecmwf/{date}*-fc.grib2",
            kind=Kind.GLOB,
            date_token="ymd",
            shared=True,
        ),
        Artifact(
            stage="inputs",
            group="ncep",
            region=None,
            label="gdas",
            template="IC/output/ncep/gdas_{date}.pgrb2",
            kind=Kind.FILE,
            shared=True,
        ),
    ]


def _build_observation_artifacts() -> list[Artifact]:
    """IMERG, IMD, S2S, and NCUM inputs and products."""
    return [
        # IMERG: daily raw granules (calendar day) + the per-day output dir.
        Artifact(
            stage="inputs",
            group="IMERG",
            region=None,
            label="imerg_daily",
            template="IMERG/raw/IMERG_daily/3B-DAY-*.3IMERG.{date}-*.nc4",
            kind=Kind.GLOB,
            date_token="ymd",
        ),
        Artifact(
            stage="postprocessed",
            group="IMERG",
            region="india",
            label="output",
            template="IMERG/output/{date}",
            kind=Kind.DIR,
            date_token="ymd",
        ),
        # IMD daily bulletin PDF.
        Artifact(
            stage="postprocessed",
            group="IMD",
            region="india",
            label="bulletin",
            template="IMD/output/AIWFB_{date}.pdf",
            kind=Kind.FILE,
            date_token="ymd",
        ),
        # S2S: raw GRIB (control + perturbed) and NetCDF inputs + output dir.
        Artifact(
            stage="inputs",
            group="S2S",
            region="india",
            label="grib",
            template="S2S/raw/grib/ifs_s2s_*init_{date}.grib",
            kind=Kind.GLOB,
        ),
        Artifact(
            stage="inputs",
            group="S2S",
            region="india",
            label="netcdf",
            template="S2S/raw/netcdf/ifs_s2s_init_{date}.nc",
            kind=Kind.FILE,
        ),
        Artifact(
            stage="postprocessed",
            group="S2S",
            region="india",
            label="output",
            template="S2S/output/india/{date}",
            kind=Kind.DIR,
        ),
        # NCUM: raw download and the precipitation product (a blend input).
        Artifact(
            stage="inputs",
            group="NCUM",
            region="india",
            label="raw",
            template="NCUM/raw/precipitation_amount/precipitation_amount_{date}.nc",
            kind=Kind.FILE,
        ),
        Artifact(
            stage="postprocessed",
            group="NCUM",
            region="india",
            label="precipitation_amount",
            template="NCUM/output/precipitation_amount/precipitation_amount_{date}.nc",
            kind=Kind.FILE,
        ),
    ]


def _diagnostic_template(blend) -> str:
    """Return the diagnostics output dir template (relative, with ``{date}``)."""
    if blend.diagnostic_output_dir_template:
        return blend.diagnostic_output_dir_template
    return f"model_diagnostics/output/{blend.region}/{{date}}/{blend.name}"


def _build_blend_artifacts() -> list[Artifact]:
    """Blend and diagnostics artifacts, derived from ``blend.utils.main.BLENDS``."""
    artifacts: list[Artifact] = []
    for blend in BLENDS:
        artifacts.append(
            Artifact(
                stage="blend",
                group=blend.name,
                region=blend.region,
                label="blend",
                template=blend.output_dir_template,
                kind=Kind.DIR,
                # Diagnostics-only blends have no blend output to expect.
                expected=blend.blend_implemented,
            )
        )
        if blend.diagnostic_plots:
            artifacts.append(
                Artifact(
                    stage="diagnostics",
                    group=blend.name,
                    region=blend.region,
                    label="diagnostics",
                    template=_diagnostic_template(blend),
                    kind=Kind.DIR,
                )
            )
    return artifacts


def build_registry() -> list[Artifact]:
    """Assemble the full static artifact registry for every model and blend."""
    return [
        *_build_shared_input_artifacts(),
        *_build_aifs_artifacts(),
        *_build_neuralgcm_artifacts(),
        *_build_gencast_artifacts(),
        *_build_observation_artifacts(),
        *_build_blend_artifacts(),
    ]


# --------------------------------------------------------------------------- #
# Date handling
# --------------------------------------------------------------------------- #


def normalize_date(raw: str) -> str:
    """Normalize a user date to canonical ``YYYYMMDDTHH``.

    Accepts ``YYYYMMDD`` (defaults to the 00 cycle), ``YYYYMMDDHH``, or
    ``YYYYMMDDTHH``.

    Args:
        raw: The date string supplied on the command line.

    Returns:
        The canonical ``YYYYMMDDTHH`` representation.

    Raises:
        ValueError: If the string does not match a supported format.
    """
    text = raw.strip().upper()
    if re.fullmatch(r"\d{8}", text):
        return f"{text}T00"
    if re.fullmatch(r"\d{10}", text):
        return f"{text[:8]}T{text[8:]}"
    if DATE_CANONICAL_RE.match(text):
        return text
    raise ValueError(
        f"Invalid date {raw!r}; expected YYYYMMDD, YYYYMMDDHH, or YYYYMMDDTHH."
    )


def expand_date_range(start: str, end: str) -> list[str]:
    """Return one canonical ``YYYYMMDDT00`` date per day from start to end."""
    start_day = datetime.strptime(normalize_date(start)[:8], "%Y%m%d").replace(
        tzinfo=timezone.utc
    )
    end_day = datetime.strptime(normalize_date(end)[:8], "%Y%m%d").replace(
        tzinfo=timezone.utc
    )
    if end_day < start_day:
        raise ValueError("--end-date must not be before --start-date.")
    dates = []
    current = start_day
    while current <= end_day:
        dates.append(f"{current:%Y%m%d}T00")
        current += timedelta(days=1)
    return dates


def render_date(canonical: str, token: str) -> str:
    """Render a canonical date for an artifact's ``date_token``."""
    if token == "model":
        return canonical
    if token == "ymd":
        return canonical[:8]
    raise ValueError(f"Unknown date token {token!r}.")


# --------------------------------------------------------------------------- #
# Resolution and existence
# --------------------------------------------------------------------------- #


def resolve(artifact: Artifact, canonical_date: str, root: Path) -> Resolved:
    """Resolve an artifact against a date, listing any existing paths."""
    rendered = render_date(canonical_date, artifact.date_token)
    rel = artifact.template.format(date=rendered)

    if artifact.kind is Kind.GLOB:
        matches = sorted(root.glob(rel))
        candidate = root / rel  # the (wildcard) pattern, for display only
        return Resolved(artifact, canonical_date, candidate, matches)

    if artifact.kind is Kind.ZARR_OR_DIR:
        zarr = root / f"{rel}.zarr"
        bare = root / rel
        matches = [p for p in (zarr, bare) if p.exists()]
        return Resolved(artifact, canonical_date, zarr, matches)

    candidate = root / rel
    if artifact.kind is Kind.FILE:
        exists = candidate.is_file()
    else:  # ZARR or DIR
        exists = candidate.is_dir()
    return Resolved(artifact, canonical_date, candidate, [candidate] if exists else [])


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


@dataclass
class Filters:
    """User-supplied selection filters; empty sets mean "match everything"."""

    stages: set[str]
    groups: set[str]
    regions: set[str]
    labels: set[str]

    def matches(self, artifact: Artifact) -> bool:
        if self.stages and artifact.stage not in self.stages:
            return False
        if self.groups and artifact.group not in self.groups:
            return False
        if self.labels and artifact.label not in self.labels:
            return False
        if self.regions:
            # Region-agnostic (shared) artifacts always pass a region filter so
            # that, e.g., a per-region audit still surfaces the shared inputs it
            # depends on.
            if artifact.region is not None and artifact.region not in self.regions:
                return False
        return True


def select(
    registry: list[Artifact], filters: Filters, dates: list[str], root: Path
) -> list[Resolved]:
    """Resolve every artifact that passes the filters, for every date."""
    selected = [a for a in registry if filters.matches(a)]
    resolved = []
    for date in dates:
        for artifact in selected:
            resolved.append(resolve(artifact, date, root))
    return resolved


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

_OK = "[ok]"
_MISSING = "[--]"
_ABSENT = "[..]"  # not present, and not required


def _resolved_sort_key(r: Resolved) -> tuple[str, str, str]:
    return (r.artifact.group, r.artifact.region or "", r.artifact.label)


def _status_symbol(resolved: Resolved) -> str:
    if resolved.exists:
        return _OK
    return _MISSING if resolved.artifact.expected else _ABSENT


def _display_path(resolved: Resolved, root: Path) -> str:
    if resolved.matches:
        rels = [str(p.relative_to(root)) for p in resolved.matches]
        if len(rels) == 1:
            return rels[0]
        return f"{rels[0]}  (+{len(rels) - 1} more)"
    return str(resolved.candidate.relative_to(root))


def render_report(
    resolved: list[Resolved],
    dates: list[str],
    root: Path,
    *,
    missing_only: bool,
    present_only: bool,
) -> str:
    """Render a human-readable, per-date, per-stage status report."""
    lines: list[str] = []
    by_date: dict[str, list[Resolved]] = {date: [] for date in dates}
    for item in resolved:
        by_date[item.date].append(item)

    for date in dates:
        items = by_date[date]
        shown_total = 0
        date_lines: list[str] = []
        for stage in STAGES:
            stage_items = [r for r in items if r.artifact.stage == stage]
            if not stage_items:
                continue
            rows = []
            for r in sorted(stage_items, key=_resolved_sort_key):
                if missing_only and r.exists:
                    continue
                if present_only and not r.exists:
                    continue
                region = r.artifact.region or "-"
                shared = " (shared)" if r.artifact.shared else ""
                rows.append(
                    f"    {_status_symbol(r)} {r.artifact.group}/{region}/"
                    f"{r.artifact.label}{shared}: {_display_path(r, root)}"
                )
            if rows:
                date_lines.append(f"  [{stage}]")
                date_lines.extend(rows)
                shown_total += len(rows)

        present = sum(1 for r in items if r.exists)
        missing = sum(1 for r in items if not r.exists and r.artifact.expected)
        header = (
            f"=== {date} ===  "
            f"{present} present, {missing} missing (of {len(items)} checked)"
        )
        lines.append(header)
        if date_lines:
            lines.extend(date_lines)
        elif missing_only:
            lines.append("  (no gaps)")
        elif present_only:
            lines.append("  (nothing present)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(resolved: list[Resolved], root: Path) -> str:
    """Render the resolved inventory as JSON for machine consumption."""
    payload = []
    for r in resolved:
        payload.append(
            {
                "date": r.date,
                "stage": r.artifact.stage,
                "group": r.artifact.group,
                "region": r.artifact.region,
                "label": r.artifact.label,
                "kind": r.artifact.kind.value,
                "shared": r.artifact.shared,
                "expected": r.artifact.expected,
                "exists": r.exists,
                "candidate": str(r.candidate.relative_to(root)),
                "matches": [str(p.relative_to(root)) for p in r.matches],
            }
        )
    return json.dumps(payload, indent=2)


# --------------------------------------------------------------------------- #
# Deletion
# --------------------------------------------------------------------------- #


def _delete_path(path: Path, root: Path) -> None:
    """Delete a single file or directory, refusing anything outside the repo."""
    resolved_path = path.resolve()
    if root not in resolved_path.parents and resolved_path != root:
        raise ValueError(f"Refusing to delete path outside repo root: {path}")
    if resolved_path == root:
        raise ValueError("Refusing to delete the repository root.")
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def collect_delete_targets(resolved: list[Resolved]) -> list[tuple[Resolved, Path]]:
    """Flatten resolved artifacts into the concrete existing paths to remove."""
    targets = []
    for r in resolved:
        for path in r.matches:
            targets.append((r, path))
    return targets


def run_deletion(
    resolved: list[Resolved],
    root: Path,
    *,
    dry_run: bool,
    assume_yes: bool,
) -> int:
    """List and (after confirmation) delete the selected existing artifacts.

    Returns:
        Process exit code (0 on success or nothing-to-do).
    """
    targets = collect_delete_targets(resolved)
    if not targets:
        print("Nothing to delete: no matching artifacts exist for this selection.")
        return 0

    shared = [t for t in targets if t[0].artifact.shared]
    print(f"The following {len(targets)} artifact(s) will be deleted:\n")
    for r, path in targets:
        flag = " (SHARED across models)" if r.artifact.shared else ""
        print(
            f"  - [{r.date}] {r.artifact.stage}/{r.artifact.group}/"
            f"{r.artifact.label}{flag}: {path.relative_to(root)}"
        )
    print()
    if shared:
        print(
            f"WARNING: {len(shared)} target(s) are shared inputs; deleting them "
            "affects every model that consumes that date.\n"
        )

    if dry_run:
        print("--dry-run: no files were deleted.")
        return 0

    if not assume_yes:
        answer = input(f"Delete these {len(targets)} item(s)? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Aborted; nothing was deleted.")
            return 1

    deleted = 0
    for _r, path in targets:
        _delete_path(path, root)
        deleted += 1
    print(f"Deleted {deleted} artifact(s).")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Discover (and optionally delete) operational pipeline outputs across "
            "all models, blends, and stages for given forecast dates."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dates = parser.add_argument_group("date selection")
    dates.add_argument(
        "--date",
        action="append",
        default=[],
        metavar="YYYYMMDD[THH]",
        help="A forecast date to check. May be repeated.",
    )
    dates.add_argument(
        "--start-date",
        metavar="YYYYMMDD",
        help="Start of an inclusive daily (00 cycle) date range.",
    )
    dates.add_argument(
        "--end-date",
        metavar="YYYYMMDD",
        help="End of an inclusive daily (00 cycle) date range.",
    )

    filt = parser.add_argument_group("granularity filters (repeatable)")
    filt.add_argument(
        "--stage",
        action="append",
        default=[],
        choices=STAGES,
        help="Restrict to one or more pipeline stages.",
    )
    filt.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="NAME",
        help="Restrict to one or more model/observation groups "
        "(e.g. AIFS_single_v2, NeuralGCM, gencast, NCUM, IMERG, ecmwf, ncep).",
    )
    filt.add_argument(
        "--blend",
        action="append",
        default=[],
        metavar="NAME",
        help="Restrict to one or more blend names (matches blend/diagnostics).",
    )
    filt.add_argument(
        "--region",
        action="append",
        default=[],
        choices=REGIONS,
        help="Restrict to a region. Shared/region-agnostic artifacts are kept.",
    )
    filt.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="PRODUCT",
        help="Restrict to one or more product labels (e.g. tp_0p25, sji, raw).",
    )

    out = parser.add_argument_group("output")
    out.add_argument(
        "--missing-only", action="store_true", help="Only show missing artifacts."
    )
    out.add_argument(
        "--present-only", action="store_true", help="Only show existing artifacts."
    )
    out.add_argument(
        "--json", action="store_true", help="Emit the inventory as JSON instead."
    )

    delete = parser.add_argument_group("deletion (off by default)")
    delete.add_argument(
        "--delete",
        action="store_true",
        help="Delete the selected EXISTING artifacts (lists and confirms first).",
    )
    delete.add_argument(
        "--dry-run",
        action="store_true",
        help="With --delete, list what would be deleted without deleting.",
    )
    delete.add_argument(
        "--yes",
        action="store_true",
        help="With --delete, skip the interactive confirmation prompt.",
    )
    return parser.parse_args(argv)


def resolve_dates(args: argparse.Namespace) -> list[str]:
    """Build the sorted, de-duplicated canonical date list from CLI args."""
    dates: list[str] = [normalize_date(d) for d in args.date]
    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            raise ValueError("--start-date and --end-date must be used together.")
        dates.extend(expand_date_range(args.start_date, args.end_date))
    return sorted(set(dates))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.missing_only and args.present_only:
        print("error: --missing-only and --present-only are mutually exclusive.")
        return 2

    try:
        target_dates = resolve_dates(args)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    if not target_dates:
        print("error: supply at least one --date or a --start-date/--end-date range.")
        return 2

    if args.delete and not (args.date or (args.start_date and args.end_date)):
        print("error: --delete requires an explicit date selection.")
        return 2

    filters = Filters(
        stages=set(args.stage),
        groups=set(args.model) | set(args.blend),
        regions=set(args.region),
        labels=set(args.label),
    )

    registry = build_registry()
    resolved = select(registry, filters, target_dates, REPO_ROOT)
    if not resolved:
        print("No artifacts matched the supplied filters.")
        return 0

    if args.delete:
        return run_deletion(
            resolved, REPO_ROOT, dry_run=args.dry_run, assume_yes=args.yes
        )

    if args.json:
        print(render_json(resolved, REPO_ROOT))
    else:
        print(
            render_report(
                resolved,
                target_dates,
                REPO_ROOT,
                missing_only=args.missing_only,
                present_only=args.present_only,
            ),
            end="",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
