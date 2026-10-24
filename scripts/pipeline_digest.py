#!/usr/bin/env python3
"""Daily status digest for the dev pipeline (India ensemble, DryWetCast, Drive sync).

Usage:
  .venv/bin/python scripts/pipeline_digest.py [--date 20261023] [--days 7] [--cost] [--summarize]

Read-only: every GCP call is a gcloud/bq read made with your existing gcloud
credentials (Batch jobs describe, Cloud Storage cat/list, one BigQuery SELECT
for --cost). The markdown is printed and written to ~/monsoon-scratch/digest/<date>.md.

Anything that can't be read is shown as "not found" or "missing" (or the error),
never estimated.

--summarize sends only the structured facts (JSON, no bucket names or paths) to
xAI's Responses API and appends a paragraph labelled "AI summary". The tables are
always printed as computed. The key comes from XAI_API_KEY or ~/.xai-keys/api_key
and is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

PROJECT = "ai-weather-operational"
COMMON_BUCKET = f"monsoon-dev-common-{PROJECT}"
INDIA_BUCKET = f"monsoon-dev-india-{PROJECT}"
BILLING_TABLE = f"{PROJECT}.all_billing_data.gcp_billing_export_v1_01BC0F_9DA59B_09C0FA"
GCLOUD = str(Path.home() / "google-cloud-sdk/bin/gcloud")
BQ = str(Path.home() / "google-cloud-sdk/bin/bq")
OUT_DIR = Path.home() / "monsoon-scratch/digest"

# Ladder steps: (step, job-id suffix, region).
ENSEMBLE_JOBS = (
    ("primary", "", "us-central1"),
    ("fb1", "-fb1", "us-east1"),
    ("fb2", "-fb2", "us-central1"),
)
DRYWETCAST_CONFIGS = ("gefs_reduced", "gefs_full", "ncmrwf_reduced", "ncmrwf_full")
TARGET_UTC = (8, 30)  # 14:00 IST
IST = timezone(timedelta(hours=5, minutes=30))
# The common bucket deletes intermediate/ objects after this many days.
MARKER_RETENTION_DAYS = 30
STOCKOUT_CODE = "CODE_GCE_ZONE_RESOURCE_POOL_EXHAUSTED"

XAI_ENDPOINT = "https://api.x.ai/v1/responses"
XAI_MODEL = "grok-4.7"
XAI_KEY_FILE = Path.home() / ".xai-keys/api_key"
SUMMARY_INSTRUCTIONS = (
    "You write a short status note for the people who run a daily weather-forecast "
    "pipeline. The JSON you are given is the day's digest. Write 2-4 short, plain "
    "English sentences for a person, with no lists, headings or markdown. "
    "Sentence 1 is the overall verdict: was the forecast delivered on time against "
    "the 14:00 IST target, and if not, how late, or did anything fail. The target "
    "applies only to the ensemble and the two GEFS DryWetCast configs (the ones "
    "with an early/late value); the NCMRWF configs have no target and arrive later "
    "every day, so their time alone is never late or unusual. Call the products "
    "'the ensemble' and 'DryWetCast'. Then mention "
    "only what was unusual: VM stockouts, a fallback job used instead of the primary, "
    "failed or unavailable configs, missing subdistrict products, failed Drive "
    "syncs. If nothing was unusual, say 'Nothing unusual.' "
    "Describe things in words: never quote field names, JSON keys, file names or "
    "raw values like 'minutes_vs_0830_utc' or 'not found'. Use only facts in the "
    "data: do not add, compute, round or estimate any number, time, duration or "
    "count that is not written in it, and say a value is missing rather than "
    "guessing why."
)


# ---------------------------------------------------------------------------
# Reads (gcloud / bq). Each returns data or raises ReadError.
# ---------------------------------------------------------------------------


class ReadError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # "not_found", "denied" or "error"


def _classify(stderr: str) -> str:
    text = stderr.lower()
    if any(
        s in text
        for s in ("was not found", "matched no objects", "not found: 404", "not_found")
    ):
        return "not_found"
    if any(
        s in text
        for s in ("permission_denied", "access denied", "403", "does not have")
    ):
        return "denied"
    return "error"


def run_cli(args: list[str]) -> str:
    """Run a read-only gcloud/bq command and return stdout."""
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=180, check=False
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        last = stderr.splitlines()[0] if stderr else f"exit {result.returncode}"
        raise ReadError(_classify(stderr), last[:300])
    return result.stdout


def describe_job(job_id: str, region: str) -> dict:
    out = run_cli(
        [
            GCLOUD,
            "batch",
            "jobs",
            "describe",
            job_id,
            f"--location={region}",
            f"--project={PROJECT}",
            "--format=json",
        ]
    )
    return json.loads(out)


def read_object(bucket: str, path: str) -> str:
    return run_cli(
        [GCLOUD, "storage", "cat", f"gs://{bucket}/{path}", f"--project={PROJECT}"]
    )


def list_objects(bucket: str, pattern: str) -> dict[str, str]:
    """{object name: creation time (ISO)} for objects matching the wildcard."""
    try:
        out = run_cli(
            [
                GCLOUD,
                "storage",
                "objects",
                "list",
                f"gs://{bucket}/{pattern}",
                f"--project={PROJECT}",
                "--format=json(name,creation_time)",
            ]
        )
    except ReadError as exc:
        if exc.kind == "not_found":
            return {}
        raise
    return {
        item["name"]: item.get("creation_time", "") for item in json.loads(out or "[]")
    }


def bq_query(sql: str) -> list[dict]:
    out = run_cli(
        [
            BQ,
            f"--project_id={PROJECT}",
            "query",
            "--use_legacy_sql=false",
            "--format=json",
            "--quiet",
            sql,
        ]
    )
    return json.loads(out or "[]")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Batch times carry nanoseconds; fromisoformat takes at most microseconds.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)  # +0000 -> +00:00
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    return (
        value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None
    )


def hm_utc(value: str | None) -> str:
    parsed = parse_time(value)
    return parsed.strftime("%H:%M:%S") if parsed else "missing"


def hm_ist(value: str | None) -> str:
    parsed = parse_time(value)
    return parsed.astimezone(IST).strftime("%H:%M:%S") if parsed else "missing"


def target_for(day: str) -> datetime:
    base = datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc)
    return base.replace(hour=TARGET_UTC[0], minute=TARGET_UTC[1])


def minutes_vs_target(day: str, value: str | None) -> float | None:
    """Minutes after 08:30 UTC (negative = early)."""
    parsed = parse_time(value)
    if not parsed:
        return None
    return round((parsed - target_for(day)).total_seconds() / 60, 1)


def early_late(minutes: float | None) -> str:
    if minutes is None:
        return "missing"
    if minutes <= 0:
        return f"{abs(minutes):.1f} min early"
    return f"{minutes:.1f} min late"


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


def _error_fact(exc: ReadError) -> dict:
    return {
        "found": False,
        "status": "not found" if exc.kind == "not_found" else exc.kind,
        "error": None if exc.kind == "not_found" else str(exc),
    }


def job_facts(job: dict) -> dict:
    """Times and events from a Batch job description."""
    # Batch doesn't list events in time order, and it updates its stockout event
    # in place (latest time, "error count is N", one zone from the latest message).
    events = sorted(
        job.get("status", {}).get("statusEvents", []) or [],
        key=lambda e: (
            parse_time(e.get("eventTime")) or datetime.min.replace(tzinfo=timezone.utc)
        ),
    )
    first_running = succeeded = ended = None
    stockouts, requeues = [], 0
    for event in events:
        description = event.get("description", "")
        when = event.get("eventTime")
        if first_running is None and re.search(r"to RUNNING\b", description):
            first_running = when
        if re.search(r"to SUCCEEDED\b", description):
            succeeded = when
        if re.search(r"to (FAILED|CANCELLED)\b", description):
            ended = when
        if re.search(r"to SCHEDULED_PENDING_QUEUED\b", description):
            requeues += 1
        if STOCKOUT_CODE in description:
            zone = re.search(r"zones/([a-z0-9-]+)", description)
            count = re.search(r"error count is (\d+)", description)
            stockouts.append(
                {
                    "time": iso(parse_time(when)),
                    "zone": zone.group(1) if zone else None,
                    "error_count": int(count.group(1)) if count else None,
                }
            )
    return {
        "found": True,
        "status": job.get("status", {}).get("state", "missing"),
        "attempt": job.get("labels", {}).get("attempt"),
        "created": iso(parse_time(job.get("createTime"))),
        "first_running": iso(parse_time(first_running)),
        "succeeded": iso(parse_time(succeeded)),
        "failed_or_cancelled": iso(parse_time(ended)),
        "stockouts": stockouts,
        "requeues": requeues,
    }


def gather_ensemble(day: str) -> dict:
    jobs = {}
    for step, suffix, region in ENSEMBLE_JOBS:
        job_id = f"aifs-ens-v2-{day}-00{suffix}"
        try:
            facts = job_facts(describe_job(job_id, region))
        except ReadError as exc:
            facts = _error_fact(exc)
        jobs[step] = {"job_id": job_id, "region": region, **facts}
    try:
        claim = json.loads(
            read_object(COMMON_BUCKET, f"batch-state/AIFS_ENS_v2/{day}T00/winner")
        )
        winner_id = str(claim.get("job_id", ""))
        winner = {
            "status": "found",
            "job_id": winner_id or "missing",
            "step": next(
                (s for s, j in jobs.items() if j["job_id"] == winner_id), "missing"
            ),
            "claimed_utc": iso(parse_time(claim.get("claimed_utc"))),
            "forced": bool(claim.get("forced", False)),
        }
    except ReadError as exc:
        winner = {
            "status": "not found" if exc.kind == "not_found" else exc.kind,
            "error": None if exc.kind == "not_found" else str(exc),
        }
    except json.JSONDecodeError:
        winner = {"status": "error", "error": "claim file is not JSON"}
    for job in jobs.values():
        job["holds_claim"] = (
            job["job_id"] == winner["job_id"] if winner["status"] == "found" else None
        )
    return {"jobs": jobs, "winner": winner}


def gather_markers(day: str) -> dict:
    date = f"{day}T00"
    try:
        found = list_objects(COMMON_BUCKET, f"intermediate/*{date}_done")
        error = None
    except ReadError as exc:
        found, error = {}, str(exc)
    names = {"ensemble": f"intermediate/AIFS_ENS_v2_india_{date}_done"}
    names.update(
        {
            c: f"intermediate/drywetcast_india_{c}_{date}_done"
            for c in DRYWETCAST_CONFIGS
        }
    )
    names["drywetcast_date"] = f"intermediate/drywetcast_india_{date}_done"
    markers = {}
    for key, name in names.items():
        created = iso(parse_time(found.get(name))) if name in found else None
        entry = {
            "utc": created,
            "status": "found" if created else ("error" if error else "not found"),
        }
        if key in ("ensemble", "gefs_reduced", "gefs_full"):
            entry["minutes_vs_0830_utc"] = minutes_vs_target(day, created)
        markers[key] = entry
    age_days = (datetime.now(timezone.utc) - target_for(day)).days
    return {
        "markers": markers,
        "error": error,
        "older_than_marker_retention": age_days > MARKER_RETENTION_DAYS,
    }


def _read_json(bucket: str, path: str) -> tuple[dict | None, str]:
    try:
        payload = json.loads(read_object(bucket, path))
        return (payload if isinstance(payload, dict) else None), "found"
    except ReadError as exc:
        return None, "not found" if exc.kind == "not_found" else f"{exc.kind}: {exc}"
    except json.JSONDecodeError:
        return None, "error: not JSON"


def subdistrict_products(day: str) -> dict[str, dict]:
    """Per config: are its uploaded subdistrict .pkl and .png both there?

    State is done (both), partial (one; `present` says which) or missing (neither).
    """
    date = f"{day}T00"
    try:
        found = list_objects(
            INDIA_BUCKET, f"output/drywetcast/{date}/*/*_subdistrict.*"
        )
        error = None
    except ReadError as exc:
        found, error = {}, str(exc)
    result = {}
    for config in DRYWETCAST_CONFIGS:
        stem = f"output/drywetcast/{date}/{config}/{day}_00_subdistrict"
        present = [ext for ext in ("pkl", "png") if f"{stem}.{ext}" in found]
        if error:
            state = "error"
        elif len(present) == 2:
            state = "done"
        elif present:
            state = "partial"
        else:
            state = "missing"
        result[config] = {"state": state, "present": present}
        if error:
            result[config]["error"] = error
    return result


def subdistrict_cell(entry: dict) -> str:
    if entry["state"] == "partial":
        missing = next(ext for ext in ("pkl", "png") if ext not in entry["present"])
        return f"partial (only .{entry['present'][0]}; .{missing} missing)"
    if entry["state"] == "error":
        return f"error: {entry.get('error')}"
    return entry["state"]


def gather_drywetcast(day: str) -> dict:
    date = f"{day}T00"
    status, status_state = _read_json(
        INDIA_BUCKET, f"drywetcast-state/{date}/status.json"
    )
    manifest, manifest_state = _read_json(
        INDIA_BUCKET, f"output/drywetcast/{date}/manifest.json"
    )
    configs = {}
    status_configs = (status or {}).get("configs", {})
    subdistrict = subdistrict_products(day)
    for config in DRYWETCAST_CONFIGS:
        entry = status_configs.get(config)
        configs[config] = {
            "state": entry.get("state", "missing") if entry else "missing",
            "attempts": entry.get("attempts") if entry else None,
            "updated_utc": iso(parse_time(entry.get("updated_utc"))) if entry else None,
            "message": (entry.get("message") or None) if entry else None,
            "subdistrict": subdistrict[config],
        }
    bundle = (manifest or {}).get("subdistrict_bundle") or {}
    errors = [
        f"{c}: {v['message']}"
        for c, v in configs.items()
        if v["message"] and v["state"] != "done"
    ]
    if bundle.get("message"):
        errors.append(f"subdistrict bundle: {bundle['message']}")
    return {
        "status_file": status_state,
        "manifest_file": manifest_state,
        "configs": configs,
        "subdistrict_bundle": bundle.get("state") or ("missing" if manifest else None),
        "drywetcast_commit": ((manifest or {}).get("drywetcast_commit") or "")[:7]
        or None,
        "manifest_written_utc": iso(parse_time((manifest or {}).get("written_utc"))),
        "errors": errors,
    }


def gather_sync(day: str) -> dict:
    try:
        found = list_objects(INDIA_BUCKET, f"sync-items/{day}T00/drywetcast/*")
        error = None
    except ReadError as exc:
        found, error = {}, str(exc)
    items = {}
    for config in DRYWETCAST_CONFIGS:
        prefix = f"sync-items/{day}T00/drywetcast/{config}"
        if f"{prefix}.done.json" in found:
            items[config] = {
                "state": "done",
                "utc": iso(parse_time(found[f"{prefix}.done.json"])),
            }
        elif f"{prefix}.failed.json" in found:
            items[config] = {
                "state": "failed",
                "utc": iso(parse_time(found[f"{prefix}.failed.json"])),
            }
        else:
            items[config] = {"state": "error" if error else "not found", "utc": None}
    return {"items": items, "error": error}


def gather_cost(day: str) -> dict:
    """Net cost (cost + credits) for the UTC day before `day`, by usage_start_time."""
    previous = (target_for(day) - timedelta(days=1)).strftime("%Y-%m-%d")
    sql = f"""
SELECT
  CASE
    WHEN REGEXP_CONTAINS(LOWER(sku.description), r'gpu|a100|a2 instance') THEN 'gpu'
    WHEN service.description = 'Cloud Storage' THEN 'storage'
    ELSE 'other'
  END AS category,
  ROUND(SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 2) AS net,
  ANY_VALUE(currency) AS currency
FROM `{BILLING_TABLE}`
WHERE DATE(usage_start_time) = '{previous}' AND project.id = '{PROJECT}'
GROUP BY category
"""
    try:
        rows = bq_query(sql)
    except ReadError as exc:
        return {
            "day_utc": previous,
            "status": "access denied" if exc.kind == "denied" else "error",
            "error": str(exc),
        }
    split = {row["category"]: float(row["net"]) for row in rows}
    if not split:
        return {
            "day_utc": previous,
            "status": "missing",
            "error": "no billing rows for that day yet",
        }
    return {
        "day_utc": previous,
        "status": "found",
        "currency": rows[0].get("currency"),
        "gpu": split.get("gpu", 0.0),
        "storage": split.get("storage", 0.0),
        "other": split.get("other", 0.0),
        "total": round(sum(split.values()), 2),
    }


def gather_day(day: str) -> dict:
    with ThreadPoolExecutor(max_workers=4) as pool:
        ensemble = pool.submit(gather_ensemble, day)
        delivery = pool.submit(gather_markers, day)
        drywetcast = pool.submit(gather_drywetcast, day)
        sync = pool.submit(gather_sync, day)
        return {
            "date": day,
            "ensemble": ensemble.result(),
            "delivery": delivery.result(),
            "drywetcast": drywetcast.result(),
            "drive_sync": sync.result(),
        }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _v(value) -> str:
    return "missing" if value in (None, "") else str(value)


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _event(value: str | None) -> str:
    """A job that was found but has no such event: 'none' (not a missing read)."""
    return hm_utc(value) if value else "none"


def claim_cell(job: dict) -> str:
    """A SUCCEEDED loser exits at its claim check without running the forecast."""
    holds = job.get("holds_claim")
    if holds is None:
        return "claim missing"
    return "won" if holds else "lost / not claimed"


def render_day(facts: dict) -> list[str]:
    day = facts["date"]
    lines = [f"## {day[:4]}-{day[4:6]}-{day[6:]} (00z)", "", "### Ensemble ladder", ""]
    lines += [
        "| Step | Job | Region | State | Claim | Created | First RUNNING | SUCCEEDED | Stockouts | Requeues |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for step, job in facts["ensemble"]["jobs"].items():
        if not job["found"]:
            state = (
                "not found (never queued)"
                if job["status"] == "not found"
                else f"{job['status']}: {job.get('error')}"
            )
            lines.append(
                f"| {step} | {job['job_id']} | {job['region']} | {_cell(state)} | | | | | | |"
            )
            continue
        stockouts = (
            ", ".join(
                f"{s['zone'] or 'zone missing'}"
                + (f" ×{s['error_count']}" if s["error_count"] else "")
                + f" (latest {hm_utc(s['time'])})"
                for s in job["stockouts"]
            )
            or "none"
        )
        state = job["status"] + (
            f" (attempt {job['attempt']})" if job.get("attempt") else ""
        )
        if job["failed_or_cancelled"]:
            state += f" at {hm_utc(job['failed_or_cancelled'])}"
        lines.append(
            f"| {step} | {job['job_id']} | {job['region']} | {state} | {claim_cell(job)} | "
            f"{hm_utc(job['created'])} | "
            f"{_event(job['first_running'])} | {_event(job['succeeded'])} | {stockouts} | {job['requeues']} |"
        )
    winner = facts["ensemble"]["winner"]
    if winner["status"] == "found":
        forced = " (forced)" if winner["forced"] else ""
        lines += [
            "",
            f"Claim: **{winner['step']}** (`{winner['job_id']}`){forced}, claimed {hm_utc(winner['claimed_utc'])} UTC.",
        ]
    else:
        lines += [
            "",
            f"Claim: {winner['status']}"
            + (f" ({winner['error']})" if winner.get("error") else "")
            + ".",
        ]

    delivery = facts["delivery"]
    lines += [
        "",
        "### Delivery (done markers; target 14:00 IST = 08:30 UTC)",
        "",
        "| Marker | UTC | IST | vs 08:30 UTC |",
        "|---|---|---|---|",
    ]
    labels = {
        "ensemble": "Ensemble (India)",
        "drywetcast_date": "DryWetCast (all configs)",
    }
    for key, marker in delivery["markers"].items():
        when = marker["utc"]
        utc_text = hm_utc(when) if when else marker["status"]
        ist_text = hm_ist(when) if when else marker["status"]
        vs = (
            early_late(marker["minutes_vs_0830_utc"])
            if "minutes_vs_0830_utc" in marker
            else ""
        )
        lines.append(
            f"| {labels.get(key, 'DryWetCast ' + key)} | {utc_text} | {ist_text} | {vs} |"
        )
    if delivery["error"]:
        lines.append(f"\nMarker listing failed: {delivery['error']}")
    if delivery["older_than_marker_retention"]:
        lines.append(
            f"\nNote: markers are deleted after {MARKER_RETENTION_DAYS} days, so 'not found' here doesn't mean undelivered."
        )

    dwc = facts["drywetcast"]
    lines += [
        "",
        "### DryWetCast",
        "",
        (
            f"status.json: {dwc['status_file']}; manifest.json: {dwc['manifest_file']}; "
            f"commit {_v(dwc['drywetcast_commit'])}."
        ),
        "",
        f"Subdistrict bundle (manifest): {_v(dwc['subdistrict_bundle'])}.",
        "",
        "| Config | State | Failed attempts | Updated (UTC) | Subdistrict products | Drive sync |",
        "|---|---|---|---|---|---|",
    ]
    sync = facts["drive_sync"]["items"]
    for config, entry in dwc["configs"].items():
        drive = sync[config]["state"] + (
            f" {hm_utc(sync[config]['utc'])}" if sync[config]["utc"] else ""
        )
        lines.append(
            f"| {config} | {entry['state']} | {_v(entry['attempts'])} | {hm_utc(entry['updated_utc'])} | "
            f"{_cell(subdistrict_cell(entry['subdistrict']))} | {drive} |"
        )
    if facts["drive_sync"]["error"]:
        lines.append(f"\nDrive sync listing failed: {facts['drive_sync']['error']}")
    lines += [""] + (
        [f"Errors: {_cell('; '.join(dwc['errors']))}"]
        if dwc["errors"]
        else ["Errors: none"]
    )
    return lines


def render_cost(cost: dict) -> list[str]:
    lines = ["", f"### Cost ({cost['day_utc']} UTC, net of credits)", ""]
    if cost["status"] != "found":
        return lines + [
            f"Cost: {cost['status']}"
            + (f" ({cost['error']})" if cost.get("error") else "")
            + "."
        ]
    unit = cost.get("currency") or ""
    return lines + [
        "| GPU | Storage | Other | Total |",
        "|---|---|---|---|",
        f"| {cost['gpu']:.2f} {unit} | {cost['storage']:.2f} {unit} | {cost['other']:.2f} {unit} | {cost['total']:.2f} {unit} |",
    ]


def summary_row(facts: dict) -> str:
    day = facts["date"]
    jobs = facts["ensemble"]["jobs"]
    winner = facts["ensemble"]["winner"]
    winner_text = (
        winner.get("step", "missing")
        if winner["status"] == "found"
        else winner["status"]
    )
    stockouts = sum(len(j.get("stockouts", [])) for j in jobs.values() if j["found"])
    queued = "/".join(step for step, j in jobs.items() if j["found"]) or "none"
    markers = facts["delivery"]["markers"]
    ens = markers["ensemble"]
    gefs = [markers[c]["utc"] for c in ("gefs_reduced", "gefs_full")]
    gefs_last = max(gefs) if all(gefs) else None
    ncmrwf = [markers[c]["utc"] for c in ("ncmrwf_reduced", "ncmrwf_full")]
    ncmrwf_last = max(ncmrwf) if all(ncmrwf) else None
    drive = sum(
        1 for item in facts["drive_sync"]["items"].values() if item["state"] == "done"
    )
    return (
        f"| {day} | {queued} | {winner_text} | {stockouts} | {hm_utc(ens['utc']) if ens['utc'] else ens['status']} | "
        f"{early_late(ens.get('minutes_vs_0830_utc'))} | "
        f"{hm_utc(gefs_last) if gefs_last else 'missing'} | {early_late(minutes_vs_target(day, gefs_last))} | "
        f"{hm_utc(ncmrwf_last) if ncmrwf_last else 'missing'} | {drive}/{len(DRYWETCAST_CONFIGS)} |"
    )


def render_table(days: list[dict]) -> list[str]:
    lines = [
        "",
        f"## Last {len(days)} days",
        "",
        "| Date | Jobs queued | Winner | Stockout events | Ensemble (UTC) | vs 08:30 | GEFS last (UTC) | vs 08:30 | NCMRWF last (UTC) | Drive done |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    return lines + [summary_row(facts) for facts in days]


# ---------------------------------------------------------------------------
# AI summary (xAI)
# ---------------------------------------------------------------------------


_PATHISH = re.compile(
    r"(gs://\S+|projects/\S+|\S+@\S+|/(?:tmp|mnt|opt|app|home|Users)/\S+|monsoon-dev-[a-z]+-ai-weather-operational)"
)


def sanitize(value):
    """Drop bucket names, gs:// URIs and file paths from free text before sending."""
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return _PATHISH.sub("<path>", value)
    return value


def read_xai_key() -> str | None:
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key and XAI_KEY_FILE.exists():
        key = XAI_KEY_FILE.read_text().strip()
    return key or None


def _response_text(body: dict) -> str:
    if isinstance(body.get("output_text"), str) and body["output_text"].strip():
        return body["output_text"].strip()
    parts = [
        part.get("text", "")
        for item in body.get("output", []) or []
        if item.get("type") == "message"
        for part in item.get("content", []) or []
        if part.get("type") in ("output_text", "text")
    ]
    return "\n".join(p for p in parts if p).strip()


def summarize(facts: dict, endpoint: str, model: str, timeout: float = 90) -> str:
    key = read_xai_key()
    if not key:
        raise RuntimeError("no xAI key (set XAI_API_KEY or ~/.xai-keys/api_key)")
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": SUMMARY_INSTRUCTIONS},
            {"role": "user", "content": json.dumps(sanitize(facts), sort_keys=True)},
        ],
        "store": False,
        "temperature": 0.2,
        "max_output_tokens": 2000,
    }
    try:
        response = requests.post(
            endpoint,
            json=payload,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"request failed ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    try:
        text = _response_text(response.json())
    except ValueError:
        raise RuntimeError("response was not JSON") from None
    if not text:
        raise RuntimeError("response had no text")
    return text


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--date",
        default=datetime.now(timezone.utc).strftime("%Y%m%d"),
        help="forecast date YYYYMMDD (default: today, UTC)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help="also a one-row-per-day table for the N days ending at --date",
    )
    parser.add_argument(
        "--cost",
        action="store_true",
        help="add the previous UTC day's billed cost (BigQuery billing export)",
    )
    parser.add_argument(
        "--summarize",
        action="store_true",
        help="append an AI summary from xAI (sends the facts JSON only)",
    )
    parser.add_argument("--xai-endpoint", default=XAI_ENDPOINT)
    parser.add_argument("--xai-model", default=XAI_MODEL)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{8}", args.date):
        parser.error("--date must be YYYYMMDD")
    if args.days < 0:
        parser.error("--days must be >= 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    day = args.date
    dates = [day]
    if args.days > 1:
        base = target_for(day)
        dates = [
            (base - timedelta(days=n)).strftime("%Y%m%d") for n in range(args.days)
        ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        gathered = dict(zip(dates, pool.map(gather_day, dates)))

    facts = {"digest_date": day, "day": gathered[day]}
    lines = [
        f"# Pipeline digest: {day[:4]}-{day[4:6]}-{day[6:]}",
        "",
        f"Generated {iso(datetime.now(timezone.utc))}. Times are UTC unless marked IST.",
        "",
    ]
    lines += render_day(gathered[day])
    if args.cost:
        facts["cost"] = gather_cost(day)
        lines += render_cost(facts["cost"])
    if args.days > 1:
        days = [gathered[d] for d in dates]
        facts["last_days"] = [
            {
                "date": f["date"],
                "ensemble": f["ensemble"],
                "delivery": f["delivery"]["markers"],
                "drywetcast_states": {
                    c: v["state"] for c, v in f["drywetcast"]["configs"].items()
                },
                "drive_sync": {
                    c: v["state"] for c, v in f["drive_sync"]["items"].items()
                },
            }
            for f in days
        ]
        lines += render_table(days)

    if args.summarize:
        try:
            text = summarize(facts, args.xai_endpoint, args.xai_model)
            lines += [
                "",
                f"### AI summary ({args.xai_model}; generated from the data above, may contain errors)",
                "",
                text,
            ]
        except Exception as exc:  # noqa: BLE001  (the digest must still print)
            lines += ["", f"_AI summary unavailable: {exc}._"]

    output = "\n".join(lines) + "\n"
    print(output, end="")
    try:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / f"{day}.md").write_text(output)
    except OSError as exc:
        print(
            f"(could not write {args.out_dir / (day + '.md')}: {exc})", file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
