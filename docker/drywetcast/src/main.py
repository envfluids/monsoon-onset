"""
Monsoon DryWetCast-India job (Cloud Run)

Runs DryWetCast-India (pinned commit, unmodified, under DRYWETCAST_HOME) for one
forecast date after the India AIFS ensemble store is validated, and uploads the
products to the region bucket.

Flow for ACTION=run:
  1. Claim the date (create-if-absent object, so overlapping workflow passes
     can't start a second run).
  2. Download the AIFS store and GEFS once (GEFS retried; the scratch dir is
     fresh per execution).
  3. GEFS configs first; each is uploaded and gets its done marker without
     waiting on NCMRWF.
  4. NCMRWF configs: poll the portal for NCMRWF_WAIT_MINUTES. Not there yet ->
     left pending for a later pass. With DRYWETCAST_FINALIZE (after the daily
     cutoff) one last check, then marked unavailable and logged at ERROR.
  5. Manifest, overall done marker when all four configs are done, release the claim.

ACTION=probe only checks that NOAA GEFS and the NCMRWF portal are reachable.

Environment Variables:
    DATE                  : forecast date YYYYMMDDT00
    FORECAST_REGION       : region whose bucket holds the store and products (india)
    DRYWETCAST_CONFIGS    : JSON list of configs to run (from pipeline-state)
    DRYWETCAST_FINALIZE   : "true" after the NCMRWF cutoff
    GCS_COMMON_BUCKET     : bucket for intermediate/ done markers
    GCS_REGION_BUCKETS    : JSON {region: bucket}
    NCMRWF_API_KEY        : NCMRWF portal key (Secret Manager); read by DryWetCast
    NCMRWF_WAIT_MINUTES   : how long to poll for the NCMRWF file (default 30)
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import requests
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

# Shared with docker/pipeline-state/src/main.py.
MODEL = "AIFS_ENS_v2"
CONFIGS = ("gefs_reduced", "gefs_full", "ncmrwf_reduced", "ncmrwf_full")

DRYWETCAST_HOME = Path(os.environ.get("DRYWETCAST_HOME", "/opt/drywetcast"))
WORK_DIR = Path(os.environ.get("DRYWETCAST_WORK_DIR", "/tmp/drywetcast"))
CLAIM_MAX_AGE_SECONDS = int(
    os.environ.get("DRYWETCAST_CLAIM_MAX_AGE_SECONDS", str(75 * 60))
)
NCMRWF_WAIT_MINUTES = float(os.environ.get("NCMRWF_WAIT_MINUTES", "30"))
NCMRWF_POLL_MINUTES = float(os.environ.get("NCMRWF_POLL_MINUTES", "5"))
GEFS_DOWNLOAD_ATTEMPTS = int(os.environ.get("GEFS_DOWNLOAD_ATTEMPTS", "3"))
GEFS_PROBE_URL = "https://noaa-gefs-pds.s3.amazonaws.com/"


# ---------------------------------------------------------------------------
# Paths (shared conventions)
# ---------------------------------------------------------------------------


def store_path(date: str) -> str:
    return f"output/{MODEL}/{date}/{MODEL}/drywetcast/init_{date[:8]}T00.zarr"


def config_marker_path(region: str, config: str, date: str) -> str:
    return f"intermediate/drywetcast_{region}_{config}_{date}_done"


def date_marker_path(region: str, date: str) -> str:
    return f"intermediate/drywetcast_{region}_{date}_done"


def claim_path(date: str) -> str:
    return f"drywetcast-state/{date}/claim.json"


def status_path(date: str) -> str:
    return f"drywetcast-state/{date}/status.json"


def product_prefix(date: str, config: str) -> str:
    return f"output/drywetcast/{date}/{config}"


def archive_path(date: str, config: str) -> str:
    return f"output/drywetcast/archive/{config}/{date[:8]}_00.npz"


def manifest_path(date: str) -> str:
    return f"output/drywetcast/{date}/manifest.json"


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = storage.Client()
    return _CLIENT


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def blob_exists(bucket: str, path: str) -> bool:
    return _client().bucket(bucket).blob(path).exists()


def read_json(bucket: str, path: str) -> tuple[dict, int | None]:
    """Return (payload, generation); ({}, None) when the object doesn't exist."""
    blob = _client().bucket(bucket).get_blob(path)
    if blob is None:
        return {}, None
    try:
        text = blob.download_as_text(if_generation_match=blob.generation)
    except (NotFound, PreconditionFailed):
        return read_json(bucket, path)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {}
    return (payload if isinstance(payload, dict) else {}), blob.generation


def write_json(
    bucket: str, path: str, payload: dict, if_generation_match: int | None = None
) -> None:
    blob = _client().bucket(bucket).blob(path)
    kwargs = (
        {}
        if if_generation_match is None
        else {"if_generation_match": if_generation_match}
    )
    blob.upload_from_string(
        json.dumps(payload, indent=1, sort_keys=True), "application/json", **kwargs
    )


def write_text(bucket: str, path: str, content: str) -> None:
    _client().bucket(bucket).blob(path).upload_from_string(content)
    logger.info("Wrote gs://%s/%s", bucket, path)


def upload_file(bucket: str, local_path: Path, path: str) -> None:
    _client().bucket(bucket).blob(path).upload_from_filename(str(local_path))
    logger.info("Uploaded %s -> gs://%s/%s", local_path, bucket, path)


def delete_blob(bucket: str, path: str) -> None:
    try:
        _client().bucket(bucket).blob(path).delete()
    except NotFound:
        pass


def download_prefix(bucket: str, prefix: str, dest: Path) -> int:
    count = 0
    for blob in _client().list_blobs(bucket, prefix=prefix.rstrip("/") + "/"):
        relative = blob.name[len(prefix.rstrip("/")) + 1 :]
        if not relative or relative.endswith("/"):
            continue
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(target))
        count += 1
    return count


# ---------------------------------------------------------------------------
# Claim and status
# ---------------------------------------------------------------------------


def acquire_claim(bucket: str, date: str, execution: str, configs: list[str]) -> bool:
    """Create the date's claim, or take over one that is ours or stale."""
    payload = {
        "execution": execution,
        "configs": configs,
        "started_utc": _timestamp(utc_now()),
    }
    try:
        write_json(bucket, claim_path(date), payload, if_generation_match=0)
        return True
    except PreconditionFailed:
        pass

    existing, generation = read_json(bucket, claim_path(date))
    if generation is None:
        return acquire_claim(bucket, date, execution, configs)
    started = _parse_timestamp(existing.get("started_utc"))
    stale = (
        started is None or (utc_now() - started).total_seconds() > CLAIM_MAX_AGE_SECONDS
    )
    if existing.get("execution") != execution and not stale:
        logger.info(
            "DryWetCast for %s is already claimed by %s (started %s); exiting",
            date,
            existing.get("execution"),
            existing.get("started_utc"),
        )
        return False
    try:
        write_json(bucket, claim_path(date), payload, if_generation_match=generation)
    except PreconditionFailed:
        logger.info("Lost a race for the DryWetCast claim for %s; exiting", date)
        return False
    logger.info(
        "Took over DryWetCast claim for %s from %s", date, existing.get("execution")
    )
    return True


def _parse_timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def update_status(
    bucket: str, date: str, config: str, state: str, message: str = ""
) -> dict:
    status, _ = read_json(bucket, status_path(date))
    configs = status.setdefault("configs", {})
    entry = configs.get(config, {})
    attempts = int(entry.get("attempts", 0)) + (1 if state == "failed" else 0)
    configs[config] = {
        "state": state,
        "attempts": attempts,
        "updated_utc": _timestamp(utc_now()),
        "message": message,
    }
    status["date"] = date
    write_json(bucket, status_path(date), status)
    return configs[config]


# ---------------------------------------------------------------------------
# DryWetCast invocation
# ---------------------------------------------------------------------------


def run_drywetcast(args: list[str]) -> None:
    command = [sys.executable, str(DRYWETCAST_HOME / "run_forecast.py"), *args]
    logger.info("Running: %s", " ".join(command))
    subprocess.run(command, cwd=DRYWETCAST_HOME, check=True)


def ncmrwf_module():
    if str(DRYWETCAST_HOME) not in sys.path:
        sys.path.insert(0, str(DRYWETCAST_HOME))
    from pipeline import download_ncmrwf

    return download_ncmrwf


def ncmrwf_available(day: str) -> bool:
    """Folder listing only: is the NCMRWF file for `day` (YYYYMMDD) on the portal?"""
    try:
        ncmrwf_module().find_cycle(day)
        return True
    except FileNotFoundError:
        return False


def wait_for_ncmrwf(day: str, wait_minutes: float, poll_minutes: float) -> bool:
    deadline = utc_now() + timedelta(minutes=wait_minutes)
    while True:
        if ncmrwf_available(day):
            return True
        remaining = (deadline - utc_now()).total_seconds()
        if remaining <= 0:
            return False
        sleep_seconds = min(poll_minutes * 60, remaining)
        logger.info(
            "NCMRWF %s not on the portal yet; checking again in %.0f s",
            day,
            sleep_seconds,
        )
        time.sleep(sleep_seconds)


def download_gefs(day: str, store: Path, scratch: Path) -> None:
    for attempt in range(1, GEFS_DOWNLOAD_ATTEMPTS + 1):
        try:
            run_drywetcast(
                [
                    "--download-only",
                    "--config",
                    "gefs_reduced",
                    "--date",
                    day,
                    "--cycle",
                    "00",
                    "--aifs-zarr",
                    str(store),
                    "--out-dir",
                    str(scratch),
                ]
            )
            return
        except subprocess.CalledProcessError:
            if attempt == GEFS_DOWNLOAD_ATTEMPTS:
                raise
            # A failed atmosphere download can leave a truncated file that a rerun
            # would skip; drop that folder so it is fetched again.
            for folder in scratch.glob("gefs_*/mean_spread"):
                shutil.rmtree(folder, ignore_errors=True)
            logger.warning("GEFS download attempt %d failed; retrying in 60 s", attempt)
            time.sleep(60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@click.command()
@click.option(
    "--action", envvar="ACTION", default="run", type=click.Choice(["run", "probe"])
)
@click.option("--date", envvar="DATE", default="")
@click.option("--region", envvar="FORECAST_REGION", default="india")
@click.option(
    "--configs", envvar="DRYWETCAST_CONFIGS", default=json.dumps(list(CONFIGS))
)
@click.option(
    "--finalize",
    envvar="DRYWETCAST_FINALIZE",
    type=lambda v: str(v).lower() == "true",
    default=False,
)
@click.option("--common-bucket", envvar="GCS_COMMON_BUCKET", default="")
@click.option("--region-buckets", envvar="GCS_REGION_BUCKETS", default="{}")
def main(action, date, region, configs, finalize, common_bucket, region_buckets):
    if action == "probe":
        sys.exit(probe())

    if not date.endswith("T00"):
        raise click.ClickException(f"DATE must be a 00z date YYYYMMDDT00; got {date!r}")
    requested = json.loads(configs)
    unknown = sorted(set(requested) - set(CONFIGS))
    if unknown:
        raise click.ClickException(f"Unknown DryWetCast configs: {unknown}")
    bucket = json.loads(region_buckets).get(region)
    if not bucket or not common_bucket:
        raise click.ClickException(
            f"No bucket configured for region {region!r} or common bucket"
        )

    execution = os.environ.get("CLOUD_RUN_EXECUTION", f"local-{os.getpid()}")
    if not acquire_claim(bucket, date, execution, requested):
        return
    try:
        errors = run(
            date,
            region,
            [c for c in CONFIGS if c in requested],
            finalize,
            common_bucket,
            bucket,
        )
    finally:
        delete_blob(bucket, claim_path(date))
    if errors:
        raise click.ClickException(
            "DryWetCast problems for " + date + ": " + "; ".join(errors)
        )


def run(
    date: str,
    region: str,
    configs: list[str],
    finalize: bool,
    common_bucket: str,
    bucket: str,
) -> list[str]:
    """Run the requested configs; return the error messages (empty on success)."""
    day = date[:8]
    work = WORK_DIR / date
    shutil.rmtree(work, ignore_errors=True)
    scratch, products, archive = work / "scratch", work / "out", work / "archive"
    store = work / "aifs" / f"init_{day}T00.zarr"
    errors: list[str] = []
    results: dict[str, dict] = {}

    todo = [
        c
        for c in configs
        if not blob_exists(common_bucket, config_marker_path(region, c, date))
    ]
    if todo != configs:
        logger.info("Already done, skipping: %s", sorted(set(configs) - set(todo)))

    def run_config(config: str) -> None:
        started = time.monotonic()
        try:
            fig = products / config / f"{day}_00.png"
            fig.parent.mkdir(parents=True, exist_ok=True)
            run_drywetcast(
                [
                    "--config",
                    config,
                    "--date",
                    day,
                    "--cycle",
                    "00",
                    "--skip-download",
                    "--aifs-zarr",
                    str(store),
                    "--out-dir",
                    str(scratch),
                    "--fig-out",
                    str(fig),
                    "--archive-dir",
                    str(archive),
                ]
            )
            for local in (fig, fig.with_suffix(".npz")):
                upload_file(
                    bucket, local, f"{product_prefix(date, config)}/{local.name}"
                )
            upload_file(
                bucket, archive / config / f"{day}_00.npz", archive_path(date, config)
            )
            write_text(common_bucket, config_marker_path(region, config, date), "done")
            update_status(bucket, date, config, "done")
            results[config] = {
                "state": "done",
                "seconds": round(time.monotonic() - started, 1),
            }
        except Exception as exc:
            logger.exception("DryWetCast %s failed for %s", config, date)
            entry = update_status(bucket, date, config, "failed", str(exc)[:500])
            results[config] = {
                "state": "failed",
                "attempts": entry["attempts"],
                "message": str(exc)[:500],
            }
            errors.append(f"{config} failed: {exc}")

    if todo:
        try:
            files = download_prefix(bucket, store_path(date), store)
            if not files:
                raise RuntimeError(
                    f"AIFS store gs://{bucket}/{store_path(date)} is empty or missing"
                )
            logger.info("Downloaded AIFS store (%d files) to %s", files, store)
            download_gefs(day, store, scratch)
        except Exception as exc:
            logger.exception("DryWetCast inputs unavailable for %s", date)
            for config in todo:
                entry = update_status(
                    bucket, date, config, "failed", f"inputs: {str(exc)[:450]}"
                )
                results[config] = {"state": "failed", "attempts": entry["attempts"]}
            errors.append(f"inputs failed: {exc}")
            todo = []

    for config in (c for c in todo if c.startswith("gefs_")):
        run_config(config)

    ncmrwf_configs = [c for c in todo if c.startswith("ncmrwf_")]
    if ncmrwf_configs:
        wait = 0 if finalize else NCMRWF_WAIT_MINUTES
        try:
            available = wait_for_ncmrwf(day, wait, NCMRWF_POLL_MINUTES)
        except Exception as exc:
            logger.exception("NCMRWF portal check failed for %s", day)
            for config in ncmrwf_configs:
                entry = update_status(
                    bucket, date, config, "failed", f"portal: {str(exc)[:450]}"
                )
                results[config] = {"state": "failed", "attempts": entry["attempts"]}
            errors.append(f"NCMRWF portal check failed: {exc}")
            ncmrwf_configs = []
            available = False

        if ncmrwf_configs and available:
            try:
                run_drywetcast(
                    [
                        "--download-only",
                        "--config",
                        "ncmrwf_reduced",
                        "--date",
                        day,
                        "--cycle",
                        "00",
                        "--aifs-zarr",
                        str(store),
                        "--out-dir",
                        str(scratch),
                    ]
                )
            except Exception as exc:
                logger.exception("NCMRWF download failed for %s", day)
                for config in ncmrwf_configs:
                    entry = update_status(
                        bucket, date, config, "failed", f"download: {str(exc)[:450]}"
                    )
                    results[config] = {"state": "failed", "attempts": entry["attempts"]}
                errors.append(f"NCMRWF download failed: {exc}")
            else:
                for config in ncmrwf_configs:
                    run_config(config)
        elif ncmrwf_configs and finalize:
            message = f"NCMRWF file for {day} not on the portal by the daily cutoff"
            logger.error(
                "DryWetCast NCMRWF products UNAVAILABLE for %s: %s; marking %s unavailable",
                date,
                message,
                ", ".join(ncmrwf_configs),
            )
            for config in ncmrwf_configs:
                update_status(bucket, date, config, "ncmrwf_unavailable", message)
                results[config] = {"state": "ncmrwf_unavailable"}
            errors.append(message)
        elif ncmrwf_configs:
            logger.warning(
                "NCMRWF file for %s not on the portal after %.0f min; %s left pending for a later pass",
                day,
                wait,
                ", ".join(ncmrwf_configs),
            )
            for config in ncmrwf_configs:
                update_status(
                    bucket,
                    date,
                    config,
                    "ncmrwf_pending",
                    "NCMRWF not on the portal yet",
                )
                results[config] = {"state": "ncmrwf_pending"}

    write_manifest(bucket, date, region, results, finalize, store, scratch)
    if all(
        blob_exists(common_bucket, config_marker_path(region, c, date)) for c in CONFIGS
    ):
        write_text(common_bucket, date_marker_path(region, date), "done")
    return errors


def write_manifest(bucket, date, region, results, finalize, store, scratch) -> None:
    status, _ = read_json(bucket, status_path(date))
    store_attrs = {}
    attrs_file = store / ".zattrs"
    if attrs_file.exists():
        store_attrs = json.loads(attrs_file.read_text())
    ncmrwf_file = scratch / f"ncmrwf_{date[:8]}" / f"{date[:8]}.nc"
    manifest = {
        "date": date,
        "region": region,
        "written_utc": _timestamp(utc_now()),
        "execution": os.environ.get("CLOUD_RUN_EXECUTION", ""),
        "job_code_version": os.environ.get("CODE_VERSION", "unknown"),
        "drywetcast_commit": os.environ.get("DRYWETCAST_COMMIT", "unknown"),
        "aifs_store": f"gs://{bucket}/{store_path(date)}",
        "aifs_store_code_version": store_attrs.get("code_version", ""),
        "aifs_store_members": store_attrs.get("members", ""),
        "ncmrwf_file_bytes": ncmrwf_file.stat().st_size
        if ncmrwf_file.exists()
        else None,
        "finalize": finalize,
        "this_run": results,
        "status": status.get("configs", {}),
    }
    write_json(bucket, manifest_path(date), manifest)


def probe() -> int:
    """Reachability check: NOAA GEFS bucket and the NCMRWF portal (key from the env)."""
    ok = True
    try:
        status = requests.head(GEFS_PROBE_URL, timeout=20).status_code
        logger.info("NOAA GEFS bucket: HTTP %s", status)
        ok = ok and status < 500
    except requests.RequestException as exc:
        logger.error("NOAA GEFS bucket unreachable: %s", exc)
        ok = False
    try:
        entries = ncmrwf_module().list_folder("")
        logger.info(
            "NCMRWF portal reachable with the key: top folder has %d entries",
            len(entries),
        )
    except Exception:
        logger.exception("NCMRWF portal check failed")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    main()
