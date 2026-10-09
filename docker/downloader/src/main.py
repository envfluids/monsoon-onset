"""
Monsoon Downloader — GCS Shim Wrapper

Downloads ECMWF or NCEP initial conditions using the original science scripts
(unmodified) and uploads the results to the COMMON bucket (shared across regions).

The science scripts use relative paths from their utils/ directory, so this
wrapper sets up the expected directory structure and cwd before invoking them.

Environment Variables:
    SOURCE             : 'ecmwf' | 'ncep' | 'both' for get_latest_date
    ACTION             : 'download' | 'get_latest_date'  (default: download)
    DATE               : 00z forecast date YYYYMMDDTHH
    GCS_COMMON_BUCKET  : Common bucket for ICs, GenCast SST, intermediate markers
    REGION_MODELS      : JSON {region: [models]} from Terraform; ECMWF inputs (and
                         GenCast SST) are fetched only for these models. Falls back
                         to REGIONS, then to every cloud model when neither is set.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import requests
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

IC_UTILS      = Path("/app/IC/utils")
IC_ECMWF_DIR  = Path("/app/IC/output/ecmwf")
IC_NCEP_DIR   = Path("/app/IC/output/ncep")
NGCM_UTILS    = IC_UTILS
GENCAST_UTILS = Path("/app/gencast/utils")
ECMWF_OPEN_DATA_BUCKET = "ecmwf-open-data"
ECMWF_MIRROR_BASE_URL = "https://storage.googleapis.com/ecmwf-open-data/"
ECMWF_ORIGIN_BASE_URL = "https://data.ecmwf.int/forecasts/"
# Downloader Cloud Run job timeout (900 s) plus a margin: an older claim belongs
# to a crashed execution and is taken over.
DOWNLOAD_CLAIM_MAX_AGE_SECONDS = int(
    os.environ.get("DOWNLOAD_CLAIM_MAX_AGE_SECONDS", "1200")
)
MODEL_CONFIG_PATH = Path("/app/config/models.json")


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _client():
    return storage.Client()


_PUBLIC_CLIENT = None


def _public_client():
    global _PUBLIC_CLIENT
    if _PUBLIC_CLIENT is None:
        _PUBLIC_CLIENT = storage.Client.create_anonymous_client()
    return _PUBLIC_CLIENT


def blob_exists(bucket_name: str, gcs_path: str) -> bool:
    return _client().bucket(bucket_name).blob(gcs_path).exists()


def upload_file(bucket_name: str, local_path: Path, gcs_path: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_filename(str(local_path))
    logger.info(f"Uploaded {local_path} → gs://{bucket_name}/{gcs_path}")


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info(f"Wrote to gs://{bucket_name}/{gcs_path}: '{content}'")


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _client().bucket(bucket_name).blob(gcs_path).download_to_filename(str(local_path))
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


def download_public_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_name(local_path.name + ".tmp")
    blob = _public_client().bucket(bucket_name).blob(gcs_path)
    try:
        blob.download_to_filename(str(tmp_path))
        tmp_path.replace(local_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--source", envvar="SOURCE", required=True,
              type=click.Choice(["ecmwf", "ncep", "both"]))
@click.option("--action", envvar="ACTION", default="download",
              type=click.Choice(["download", "get_latest_date"]))
@click.option("--date", envvar="DATE", default=None)
@click.option("--bucket", envvar="GCS_COMMON_BUCKET", required=True)
def main(source, action, date, bucket):
    if action == "get_latest_date":
        _get_latest_date(source, bucket)
    else:
        _download(source, date, bucket)


# ---------------------------------------------------------------------------
# get_latest_date: probe the data source without downloading the full file
# ---------------------------------------------------------------------------

def _get_latest_date(source: str, bucket: str) -> None:
    if source == "ecmwf":
        date_str = _latest_ecmwf_00z()
    elif source == "ncep":
        date_str = _latest_ncep_00z()
        if not date_str:
            logger.warning("No NCEP 00z data available — writing empty latest_date.txt")
            write_gcs_text(bucket, "intermediate/latest_date.txt", "")
            return
    else:
        ecmwf_date = _latest_ecmwf_00z()
        ncep_date = _latest_ncep_00z()
        if not ncep_date:
            logger.warning("No NCEP 00z data available — writing empty latest_date.txt")
            write_gcs_text(bucket, "intermediate/latest_date.txt", "")
            return
        date_str = min(ecmwf_date, ncep_date)
        logger.info(
            "Latest common 00z date: %s (ECMWF=%s, NCEP=%s)",
            date_str, ecmwf_date, ncep_date,
        )

    write_gcs_text(bucket, "intermediate/latest_date.txt", date_str)
    logger.info(f"Latest {source} date: {date_str}")


def _latest_ecmwf_00z() -> str:
    """Return the latest ECMWF 00z cycle at or before ECMWF open-data latest."""
    from ecmwf.opendata import Client as OpendataClient

    latest = OpendataClient(source="google").latest()
    latest_00z = latest.replace(hour=0, minute=0, second=0, microsecond=0)
    return latest_00z.strftime("%Y%m%dT%H")


def _latest_ncep_00z(max_days_back: int = 7) -> str | None:
    """Return the latest available NCEP/GDAS 00z cycle using HEAD requests only."""
    sys.path.insert(0, str(NGCM_UTILS))
    os.chdir(NGCM_UTILS)
    import download_ncep

    today_00z = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for days_back in range(max_days_back):
        candidate = today_00z - timedelta(days=days_back)
        date_str = candidate.strftime("%Y%m%dT%H")
        url, _, _ = download_ncep.get_cycle_for_date(download_ncep.BASE_URL_PATTERN, date_str)
        logger.info("Checking for NCEP 00z cycle: %s", url)
        try:
            response = requests.head(url, timeout=download_ncep.REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("Error checking %s: %s", url, exc)
            continue
        if response.status_code == 200:
            return date_str
        if response.status_code != 404:
            logger.warning("Received status code %s for %s", response.status_code, url)
    return None


# ---------------------------------------------------------------------------
# download: fetch the IC file and push to GCS
# ---------------------------------------------------------------------------

def _download(source: str, date: str, bucket: str) -> None:
    if not date:
        date = _latest_ecmwf_00z() if source == "ecmwf" else _latest_ncep_00z()
    _require_00z(date)

    if source == "ecmwf":
        _download_ecmwf(date, bucket)
    elif source == "ncep":
        _download_ncep(date, bucket)
    else:
        raise click.ClickException("SOURCE=both is only valid with ACTION=get_latest_date")


def _require_00z(date: str) -> None:
    if not date or not date.endswith("T00"):
        raise click.ClickException(f"Only 00z initial conditions are supported; got DATE={date!r}")


def _download_ecmwf(date: str, bucket: str) -> None:
    """Run IC download_ecmwf.get_data() and upload ECMWF IC + GenCast SST artifacts to GCS.

    Only inputs for enabled models are fetched. A GenCast SST failure doesn't
    stop the GRIB upload or latest_ecmwf_date.txt; the job then exits non-zero.
    """
    enabled = _enabled_models()
    needs_sst = _needs_sst(enabled)
    logger.info(
        "ECMWF inputs for models: %s; GenCast SST needed: %s",
        sorted(enabled) if enabled is not None else "all (no REGION_MODELS/REGIONS)",
        needs_sst,
    )
    grib_prefix = f"ic/ecmwf/{date}/grib"
    expected_sst = f"ic/gencast_sst/{date}/sst_{date}.nc"
    if _ecmwf_gribs_exist(bucket, grib_prefix, date, enabled) and (
        not needs_sst or blob_exists(bucket, expected_sst)
    ):
        logger.info("ECMWF GRIB and GenCast SST artifacts already exist for %s; skipping download.", date)
        write_gcs_text(bucket, "intermediate/latest_ecmwf_date.txt", date)
        return

    # Not published yet (e.g. a pass just after 00z oper appears, or a manual run
    # with an explicit date): nothing to do, and no marker, so a later pass retries.
    missing = _unpublished_ecmwf_files(date, enabled)
    if missing:
        logger.info(
            "ECMWF %s not fully published yet (missing: %s); nothing to do",
            date,
            ", ".join(missing),
        )
        return

    # Overlapping passes can start two executions for the same date; one downloads.
    if not _acquire_download_claim(bucket, date):
        return
    try:
        _download_and_upload_ecmwf(date, bucket, enabled, needs_sst)
    finally:
        _release_download_claim(bucket, date)


def _download_and_upload_ecmwf(
    date: str, bucket: str, enabled: set[str] | None, needs_sst: bool
) -> None:
    IC_ECMWF_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(IC_UTILS))
    os.chdir(IC_UTILS)
    import download_ecmwf

    date_str = (
        download_ecmwf.get_data(date, enabled_models=enabled, sst_fatal=False) or date
    )

    _upload_ecmwf_gribs(bucket, date_str, enabled)
    sst_error = None
    if needs_sst:
        try:
            _download_and_upload_gencast_sst(bucket, date_str)
        except Exception as exc:
            sst_error = exc
            logger.exception(
                "GenCast SST unavailable for %s; ECMWF GRIBs were uploaded, "
                "but GenCast cannot run for this date",
                date_str,
            )

    write_gcs_text(bucket, "intermediate/latest_ecmwf_date.txt", date_str)
    if sst_error is not None:
        raise click.ClickException(
            f"GenCast SST download failed for {date_str} after uploading the ECMWF GRIBs: "
            f"{sst_error}"
        )


def _head_ok(url: str) -> bool:
    try:
        return requests.head(url, timeout=30, allow_redirects=True).status_code == 200
    except requests.RequestException as exc:
        logger.warning("HEAD %s failed: %s", url, exc)
        return False


def _unpublished_ecmwf_files(date_str: str, enabled: set[str] | None) -> list[str]:
    """Expected files that neither the public mirror nor the ECMWF origin serves yet."""
    missing = []
    for filename in _expected_ecmwf_grib_names(date_str, enabled):
        path = _ecmwf_open_data_path(filename)
        if not any(
            _head_ok(base + path)
            for base in (ECMWF_MIRROR_BASE_URL, ECMWF_ORIGIN_BASE_URL)
        ):
            missing.append(filename)
    return missing


def _download_claim_path(date: str) -> str:
    return f"ic/ecmwf/{date}/download-claim.json"


def _acquire_download_claim(bucket: str, date: str) -> bool:
    """Create-if-absent claim; take over our own (Cloud Run retry) or a stale one."""
    execution = os.environ.get("CLOUD_RUN_EXECUTION", f"local-{os.getpid()}")
    now = datetime.now(timezone.utc)
    payload = json.dumps(
        {"execution": execution, "started_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    )
    path = _download_claim_path(date)
    try:
        _client().bucket(bucket).blob(path).upload_from_string(
            payload, content_type="application/json", if_generation_match=0
        )
        return True
    except PreconditionFailed:
        pass

    existing = _client().bucket(bucket).get_blob(path)
    if existing is None:
        return _acquire_download_claim(bucket, date)
    try:
        claim = json.loads(existing.download_as_text())
    except (NotFound, ValueError):
        claim = {}
    try:
        started = datetime.fromisoformat(
            str(claim.get("started_utc", "")).replace("Z", "+00:00")
        )
    except ValueError:
        started = None
    stale = (
        started is None
        or (now - started).total_seconds() > DOWNLOAD_CLAIM_MAX_AGE_SECONDS
    )
    if claim.get("execution") != execution and not stale:
        logger.info(
            "ECMWF download for %s already claimed by %s (started %s); exiting",
            date,
            claim.get("execution"),
            claim.get("started_utc"),
        )
        return False
    try:
        _client().bucket(bucket).blob(path).upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=existing.generation,
        )
    except PreconditionFailed:
        logger.info("Lost a race for the ECMWF download claim for %s; exiting", date)
        return False
    logger.info(
        "Took over ECMWF download claim for %s from %s", date, claim.get("execution")
    )
    return True


def _release_download_claim(bucket: str, date: str) -> None:
    try:
        _client().bucket(bucket).blob(_download_claim_path(date)).delete()
    except NotFound:
        pass

def _expected_ecmwf_grib_names(
    date_str: str, enabled: set[str] | None = None
) -> list[str]:
    date = datetime.strptime(date_str, "%Y%m%dT%H")
    names = []
    for stream, deltas in _ecmwf_stream_deltas(enabled).items():
        for delta in sorted(deltas, reverse=True):
            target = date - timedelta(hours=delta)
            names.append(target.strftime(f"%Y%m%d%H0000-0h-{stream}-fc.grib2"))
    return names


def _model_config() -> dict:
    with open(MODEL_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _enabled_models() -> set[str] | None:
    """Models switched on in any region, from REGION_MODELS (or REGIONS).

    Returns None when neither is set or parseable, which keeps the behaviour of
    serving every cloud model.
    """
    for name in ("REGION_MODELS", "REGIONS"):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            models = set()
            for value in parsed.values():
                models.update(value["models"] if name == "REGIONS" else value)
            return {str(model) for model in models}
        except (ValueError, TypeError, AttributeError, KeyError):
            logger.warning(
                "Could not parse %s; fetching inputs for all cloud models", name
            )
            return None
    return None


def _is_enabled(model: str, model_config: dict, enabled: set[str] | None) -> bool:
    if enabled is None:
        return model_config.get("CLOUD", {}).get("run") == "true"
    return model.lower() in {name.lower() for name in enabled}


def _needs_sst(enabled: set[str] | None) -> bool:
    if enabled is None:
        return True
    return any(
        model_config.get("PARAM_MARS")
        for model, model_config in _model_config().items()
        if _is_enabled(model, model_config, enabled)
    )


def _ecmwf_stream_deltas(enabled: set[str] | None = None) -> dict[str, set[int]]:
    streams: dict[str, set[int]] = {}
    for model, model_config in _model_config().items():
        if model_config.get("ic_source") != "ecmwf" or not _is_enabled(
            model, model_config, enabled
        ):
            continue
        for stream in model_config.get("ic_streams", []):
            streams.setdefault(stream, {0}).add(int(model_config.get("ic_timedelta", 0)))
    return streams


def _ecmwf_open_data_path(filename: str) -> str:
    ymd = filename[:8]
    hh = filename[8:10]
    stream = filename.split("-0h-", 1)[1].split("-fc.", 1)[0]
    return f"{ymd}/{hh}z/ifs/0p25/{stream}/{filename}"


def _local_ecmwf_gribs_exist(date_str: str) -> bool:
    return all((IC_ECMWF_DIR / filename).exists() for filename in _expected_ecmwf_grib_names(date_str))


def _download_ecmwf_gribs_from_public_gcs(date_str: str) -> bool:
    IC_ECMWF_DIR.mkdir(parents=True, exist_ok=True)

    if _local_ecmwf_gribs_exist(date_str):
        logger.info("All expected ECMWF GRIBs already exist locally for %s.", date_str)
        return True

    try:
        for filename in _expected_ecmwf_grib_names(date_str):
            local_path = IC_ECMWF_DIR / filename
            if local_path.exists():
                logger.info("ECMWF GRIB %s already exists locally; skipping.", local_path)
                continue
            download_public_gcs_file(
                ECMWF_OPEN_DATA_BUCKET,
                _ecmwf_open_data_path(filename),
                local_path,
            )
    except Exception as exc:
        logger.warning(
            "Could not download ECMWF GRIBs for %s directly from gs://%s: %s",
            date_str,
            ECMWF_OPEN_DATA_BUCKET,
            exc,
        )

    return _local_ecmwf_gribs_exist(date_str)


def _ecmwf_gribs_exist(
    bucket: str, grib_prefix: str, date_str: str, enabled: set[str] | None = None
) -> bool:
    return all(
        blob_exists(bucket, f"{grib_prefix}/{filename}")
        for filename in _expected_ecmwf_grib_names(date_str, enabled)
    )


def _upload_ecmwf_gribs(
    bucket: str, date_str: str, enabled: set[str] | None = None
) -> None:
    missing = []
    for filename in _expected_ecmwf_grib_names(date_str, enabled):
        local_path = IC_ECMWF_DIR / filename
        if not local_path.exists():
            missing.append(str(local_path))
            continue
        upload_file(bucket, local_path, f"ic/ecmwf/{date_str}/grib/{filename}")
    if missing:
        raise RuntimeError("Missing expected ECMWF GRIB files: " + ", ".join(missing))


def _download_and_upload_gencast_sst(bucket: str, date_str: str) -> None:
    local_sst = IC_ECMWF_DIR / f"sst_{date_str}.nc"
    if not local_sst.exists():
        logger.info("ECMWF downloader did not create %s; falling back to GenCast SST helper.", local_sst)
        (GENCAST_UTILS.parent / "raw" / "sst_ic").mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(GENCAST_UTILS))
        os.chdir(GENCAST_UTILS)
        import download_sst

        download_sst.get_sst(date_str)
        fallback_sst = GENCAST_UTILS.parent / "raw" / "sst_ic" / f"sst_{date_str}.nc"
        if not fallback_sst.exists():
            raise RuntimeError(f"GenCast SST download did not create {fallback_sst}")
        local_sst = fallback_sst
    upload_file(bucket, local_sst, f"ic/gencast_sst/{date_str}/sst_{date_str}.nc")


def _download_ncep(date: str, bucket: str) -> None:
    """Run download_ncep.get_data() and upload the GRIB2 file to GCS."""
    gcs_path = f"ic/ncep/{date}/gdas_{date}.pgrb2"
    if blob_exists(bucket, gcs_path):
        logger.info("NCEP IC already exists at gs://%s/%s; skipping download.", bucket, gcs_path)
        write_gcs_text(bucket, "intermediate/latest_ncep_date.txt", date)
        return

    IC_NCEP_DIR.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(NGCM_UTILS))
    os.chdir(NGCM_UTILS)
    import download_ncep

    date_str = download_ncep.get_data(date)

    if not date_str:
        raise RuntimeError("NCEP download_ncep.get_data() returned None — download failed")

    local_pgrb2 = IC_NCEP_DIR / f"gdas_{date_str}.pgrb2"
    upload_file(bucket, local_pgrb2, f"ic/ncep/{date_str}/gdas_{date_str}.pgrb2")
    write_gcs_text(bucket, "intermediate/latest_ncep_date.txt", date_str)


if __name__ == "__main__":
    main()
# Rebuild trigger: re-run image build after transient Artifact Registry push timeout (build 93ddfab1).
