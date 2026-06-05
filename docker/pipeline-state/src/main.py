"""
Monsoon Pipeline-State Service

Single-call HTTP endpoint that returns the full multi-region pipeline state for
a forecast date (or latest 00z if not supplied):

  GET /state                                     discover latest 00z per IC source
  GET /state?date=20260430T00                    use the supplied date directly
  GET /state?lookback_days=7                     widen the discovery window
  GET /healthz                                   liveness probe

Response shape (sketch — actual contents depend on the configured regions):

  {
    "date": "20260513T00",
    "ic": {
      "ecmwf": {"date": "20260513T00", "present": true},
      "ncep":  {"date": "20260513T00", "present": true}
    },
    "models": {
      "AIFS_single_v2": {"complete": true, "regions": {"india":{"present":true},"ethiopia":{"present":true}}},
      "AIFS_ENS_v2":    {"complete": false, "regions": {"ethiopia":{"present":false}}},
      "neuralgcm":{"complete": true,  "regions": {"india":{"present":true}}}
    },
    "per_region": {
      "india":    {"blend": {"present":true,"date":"20260513T00"},
                   "sync":  {"present":true,"latest":"20260513T00","needs_run":false}},
      "ethiopia": {"sync":  {"present":true,"latest":"20260513T00","needs_run":false}}
    },
    "actions": {
      "ic_to_download": [{"source":"ecmwf","date":"20260513T00"}],
      "models_to_run": [{"model":"AIFS_single_v2","date":"20260513T00","regions":["india"]}],
      "regions_to_blend": [{"region":"india","date":"20260513T00"}],
      "regions_to_sync": [{"region":"india","date":"20260513T00"}],
      "blocked": [...]
    }
  }

GCS layout assumed (matches docker shims):
  common bucket:
    ic/ecmwf/<date>/grib/<filename>
    ic/ncep/<date>/gdas_<date>.pgrb2
    ic/gencast_sst/<date>/sst_<date>.nc
    full_field/<model>/<date>/...           (optional, gated by upload flag for NGCM)
    intermediate/{model}_{region}_{date}_done
  region bucket:
    output/<model>/<date>/...               post-processed forecast products
    output/blend/<date>/...                 blend output
    latest.txt                              last successfully synced date
"""

import json
import logging
import os
import sys
import time
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from google.api_core.exceptions import NotFound
from google.cloud import storage

from blend.utils.main import BLENDS, BlendConfig, ForecastInput


class CloudLoggingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "source": f"{record.pathname}:{record.lineno}",
        })


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(CloudLoggingFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 7
REQUEST_TIMEOUT_SECONDS = 20
EXTERNAL_PROBE_MAX_RETRIES = int(os.environ.get("EXTERNAL_PROBE_MAX_RETRIES", "6"))
EXTERNAL_PROBE_BACKOFF_FACTOR_SECONDS = float(
    os.environ.get("EXTERNAL_PROBE_BACKOFF_FACTOR_SECONDS", "2")
)
RETRYABLE_EXTERNAL_PROBE_STATUS = {429, 500, 502, 503, 504, "timeout"}
INACTIVE_DISPATCH_STATES = {"CLEANING_UP", "SUCCEEDED", "FAILED"}
ACTIVE_DISPATCH_MAX_AGE_SECONDS = 30 * 60 * 60

GCS_COMMON_BUCKET = os.environ.get("GCS_COMMON_BUCKET", "")
REGION_BUCKETS = json.loads(os.environ.get("GCS_REGION_BUCKETS", "{}"))
REGIONS = json.loads(os.environ.get("REGIONS", "{}"))
REGION_MODELS = json.loads(os.environ.get("REGION_MODELS", "{}"))

# Which IC source each model consumes
MODEL_IC_SOURCE = {
    "AIFS_single_v2": "ecmwf",
    "AIFS_ENS_v2":    "ecmwf",
    "gencast":   "ecmwf",
    "neuralgcm": "ncep",
}

BLEND_MODEL_TO_PIPELINE_MODEL = {
    "AIFS_SINGLE_V2": "AIFS_single_v2",
    "AIFS_ENS_V2": "AIFS_ENS_v2",
    "GENCAST": "gencast",
    "NCUM": "ncum",
    "NGCM": "neuralgcm",
    "NEURALGCM": "neuralgcm",
}


# ---------------------------------------------------------------------------
# External IC probing
# ---------------------------------------------------------------------------

def ecmwf_url(date: datetime) -> str:
    ymd = date.strftime("%Y%m%d")
    stamp = date.strftime("%Y%m%d%H0000")
    return f"https://data.ecmwf.int/forecasts/{ymd}/00z/ifs/0p25/oper/{stamp}-0h-oper-fc.grib2"


def ecmwf_google_url(date: datetime) -> str:
    ymd = date.strftime("%Y%m%d")
    stamp = date.strftime("%Y%m%d%H0000")
    return f"https://storage.googleapis.com/ecmwf-open-data/{ymd}/00z/ifs/0p25/oper/{stamp}-0h-oper-fc.grib2"


def ecmwf_probe_urls(date: datetime) -> list[tuple[str, str]]:
    return [
        ("google", ecmwf_google_url(date)),
        ("ecmwf", ecmwf_url(date)),
    ]


def ncep_url(date: datetime) -> str:
    ymd = date.strftime("%Y%m%d")
    return (
        "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
        f"gdas.{ymd}/00/atmos/gdas.t00z.pgrb2.0p25.f000"
    )


def head_status(url: str) -> int | str:
    request = Request(url, method="HEAD", headers={"User-Agent": "monsoon-pipeline-state/1.0"})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except URLError as exc:
        return f"url_error:{exc.reason}"
    except TimeoutError:
        return "timeout"


def _is_retryable_probe_status(status: int | str) -> bool:
    if status in RETRYABLE_EXTERNAL_PROBE_STATUS:
        return True
    return isinstance(status, str) and status.startswith("url_error:")


def _head_status_with_backoff(
    source: str,
    date_str: str,
    provider: str,
    url: str,
) -> int | str:
    for attempt in range(EXTERNAL_PROBE_MAX_RETRIES + 1):
        status = head_status(url)
        logger.info(
            "external_probe source=%s provider=%s date=%s status=%s attempt=%s",
            source,
            provider,
            date_str,
            status,
            attempt + 1,
        )
        if status == 200:
            return status
        if not _is_retryable_probe_status(status) or attempt >= EXTERNAL_PROBE_MAX_RETRIES:
            return status

        sleep_seconds = EXTERNAL_PROBE_BACKOFF_FACTOR_SECONDS * (2**attempt)
        logger.warning(
            "external_probe_retry source=%s provider=%s date=%s status=%s backoff=%ss attempt=%s max_attempts=%s",
            source,
            provider,
            date_str,
            status,
            sleep_seconds,
            attempt + 1,
            EXTERNAL_PROBE_MAX_RETRIES + 1,
        )
        time.sleep(sleep_seconds)
    return status


def latest_external_00z(source: str, lookback_days: int, today: datetime) -> str:
    cursor = today.replace(hour=0, minute=0, second=0, microsecond=0)
    for days_back in range(lookback_days):
        candidate = cursor - timedelta(days=days_back)
        date_str = candidate.strftime("%Y%m%dT%H")
        if ic_present(source, date_str):
            logger.info(
                "external_probe source=%s date=%s status=present_in_common_bucket",
                source,
                date_str,
            )
            return date_str

        probe_urls = (
            ecmwf_probe_urls(candidate)
            if source == "ecmwf"
            else [("ncep", ncep_url(candidate))]
        )
        for provider, url in probe_urls:
            status = _head_status_with_backoff(source, date_str, provider, url)
            if status == 200:
                return date_str
            logger.info(
                "external_probe_unavailable source=%s provider=%s date=%s final_status=%s",
                source,
                provider,
                date_str,
                status,
            )
    return ""


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

_storage_client: storage.Client | None = None


def gcs_client() -> storage.Client:
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client()
    return _storage_client


def gcs_object_exists(bucket: str, path: str) -> bool:
    return gcs_client().bucket(bucket).blob(path).exists()


def gcs_prefix_has_objects(bucket: str, prefix: str) -> bool:
    blobs = gcs_client().list_blobs(bucket, prefix=prefix, max_results=1)
    return next(iter(blobs), None) is not None


def read_gcs_text(bucket: str, path: str) -> str:
    try:
        return gcs_client().bucket(bucket).blob(path).download_as_text().strip()
    except NotFound:
        return ""


def read_gcs_json(bucket: str, path: str) -> dict:
    text = read_gcs_text(bucket, path)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid GCS JSON: gs://%s/%s", bucket, path)
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Per-stage state probes
# ---------------------------------------------------------------------------

def ic_ecmwf_paths(date: str) -> list[str]:
    base = datetime.strptime(date, "%Y%m%dT%H")
    requirements: dict[str, set[int]] = {}
    models = _models_in_use()
    if {"AIFS_single_v2", "AIFS_ENS_v2"} & models:
        requirements.setdefault("oper", set()).update({0, 6})
        requirements.setdefault("wave", set()).update({0, 6})
    if "gencast" in models:
        requirements.setdefault("oper", set()).update({0, 12})
    filenames = []
    for stream, deltas in requirements.items():
        for delta in sorted(deltas, reverse=True):
            target = base - timedelta(hours=delta)
            filenames.append(target.strftime(f"%Y%m%d%H0000-0h-{stream}-fc.grib2"))
    return [f"ic/ecmwf/{date}/grib/{f}" for f in filenames]


def ic_ncep_paths(date: str) -> list[str]:
    return [f"ic/ncep/{date}/gdas_{date}.pgrb2"]


def ic_present(source: str, date: str) -> bool:
    paths = ic_ecmwf_paths(date) if source == "ecmwf" else ic_ncep_paths(date)
    return all(gcs_object_exists(GCS_COMMON_BUCKET, p) for p in paths)


def model_marker_path(model: str, region: str, date: str) -> str:
    return f"intermediate/{model}_{region}_{date}_done"


def tpu_dispatch_status_path(workload: str, date: str) -> str:
    run_id = f"{workload}-{date.replace('T', '-')}"
    return f"intermediate/tpu-dispatch/{workload}/{date}/{run_id}/status.json"


def blend_marker_path(region: str, date: str) -> str:
    return f"intermediate/blend_{region}_{date}_done"


def diagnostics_marker_path(region: str, date: str) -> str:
    return f"intermediate/model_diagnostics_{region}_{date}_done"


def blend_config_marker_path(region: str, blend_name: str, date: str) -> str:
    return f"intermediate/blend_{region}_{blend_name}_{date}_done"


def diagnostics_config_marker_path(region: str, blend_name: str, date: str) -> str:
    return f"intermediate/model_diagnostics_{region}_{blend_name}_{date}_done"


def sync_state_path(date: str) -> str:
    return f"sync-state/{date}.json"


def model_region_outputs_present(model: str, region: str, date: str) -> bool:
    bucket = REGION_BUCKETS.get(region)
    if not bucket:
        return False

    if model == "AIFS_single_v2" and region == "india":
        return gcs_object_exists(bucket, f"output/{model}/{date}/{model}/tp/tp_2p0_{date}.nc")
    if model == "AIFS_single_v2" and region == "ethiopia":
        return gcs_prefix_has_objects(bucket, f"output/{model}/{date}/{model}/")
    if model == "AIFS_ENS_v2" and region == "ethiopia":
        return gcs_prefix_has_objects(bucket, f"output/{model}/{date}/{model}/")
    if model == "neuralgcm" and region == "india":
        return gcs_object_exists(bucket, f"output/neuralgcm/{date}/tp/tp_2p0_{date}.nc")
    if model == "neuralgcm" and region == "ethiopia":
        return gcs_object_exists(bucket, f"output/neuralgcm/{date}/tp/tp_2p8_{date}.nc")
    if model == "gencast":
        return gcs_object_exists(bucket, f"output/gencast/{date}/tp_0p25_{date}.nc")

    return gcs_prefix_has_objects(bucket, f"output/{model}/{date}/")


def model_region_done(model: str, region: str, date: str) -> bool:
    return (
        gcs_object_exists(GCS_COMMON_BUCKET, model_marker_path(model, region, date))
        and model_region_outputs_present(model, region, date)
    )


def tpu_dispatch_status(model: str, date: str) -> dict:
    if model != "gencast" or not date:
        return {}
    return read_gcs_json(GCS_COMMON_BUCKET, tpu_dispatch_status_path(model, date))


def parse_status_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def tpu_dispatch_active(status: dict, now: datetime) -> bool:
    state = str(status.get("state", "")).upper()
    if not state or state in INACTIVE_DISPATCH_STATES:
        return False

    updated_at = (
        parse_status_timestamp(status.get("updated_at"))
        or parse_status_timestamp(status.get("started_at"))
    )
    if updated_at is None:
        return True

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_seconds = (now.astimezone(timezone.utc) - updated_at).total_seconds()
    if age_seconds > ACTIVE_DISPATCH_MAX_AGE_SECONDS:
        logger.warning(
            "Ignoring stale TPU dispatch status: run_id=%s state=%s age_seconds=%s max_age_seconds=%s",
            status.get("run_id", ""),
            state,
            int(age_seconds),
            ACTIVE_DISPATCH_MAX_AGE_SECONDS,
        )
        return False
    return True


def blend_present(region: str, date: str) -> bool:
    bucket = REGION_BUCKETS.get(region)
    if not bucket:
        return False
    return (
        gcs_object_exists(GCS_COMMON_BUCKET, blend_marker_path(region, date))
        and gcs_prefix_has_objects(bucket, f"output/blend/{date}/")
    )


def _blend_output_prefix(blend: BlendConfig, date: str) -> str:
    parts = PurePosixPath(blend.output_dir_template.format(date=date)).parts
    if len(parts) < 3 or parts[0:2] != ("blend", "output"):
        raise ValueError(
            f"Unsupported blend output path for {blend.name}: {blend.output_dir_template}"
        )
    return "/".join(("output", "blend", date, *parts[2:]))


def blend_config_present(region: str, date: str, blend: BlendConfig) -> bool:
    bucket = REGION_BUCKETS.get(region)
    if not bucket:
        return False
    return (
        gcs_object_exists(GCS_COMMON_BUCKET, blend_config_marker_path(region, blend.name, date))
        and gcs_prefix_has_objects(bucket, _blend_output_prefix(blend, date))
    )


def diagnostics_present(region: str, date: str, blends: list[BlendConfig]) -> bool:
    bucket = REGION_BUCKETS.get(region)
    if not bucket:
        return False
    return (
        gcs_object_exists(GCS_COMMON_BUCKET, diagnostics_marker_path(region, date))
        and all(
            gcs_prefix_has_objects(
                bucket,
                f"output/model_diagnostics/{date}/{region}/{date}/{blend.name}/",
            )
            for blend in blends
        )
    )


def diagnostics_config_present(region: str, date: str, blend: BlendConfig) -> bool:
    bucket = REGION_BUCKETS.get(region)
    if not bucket:
        return False
    return (
        gcs_object_exists(
            GCS_COMMON_BUCKET,
            diagnostics_config_marker_path(region, blend.name, date),
        )
        and gcs_prefix_has_objects(
            bucket,
            f"output/model_diagnostics/{date}/{region}/{date}/{blend.name}/",
        )
    )


def _pipeline_model_for_blend_input(input_: ForecastInput) -> str:
    return BLEND_MODEL_TO_PIPELINE_MODEL.get(input_.model.upper(), input_.model)


def _pipeline_models_for_blend(blend: BlendConfig) -> set[str]:
    return {
        BLEND_MODEL_TO_PIPELINE_MODEL.get(model.upper(), model)
        for model in blend.models()
    }


def _blend_input_bucket_path(region: str, input_: ForecastInput, date: str) -> str:
    """Translate a blend-local input path into the region-bucket object path.

    blend/utils/main.py is the source of truth for local paths. Cloud model
    shims upload the same repo-shaped products under output/{model}/{date}/,
    with the leading model/output[/region] segments removed.
    """
    model = _pipeline_model_for_blend_input(input_)
    if model == "gencast":
        return f"output/gencast/{date}/tp_0p25_{date}.nc"

    parts = PurePosixPath(input_.path_template.format(date=date)).parts
    if len(parts) < 3 or parts[1] != "output":
        raise ValueError(
            f"Unsupported blend input path for {input_.model}: {input_.path_template}"
        )

    if len(parts) >= 4 and parts[2] == region:
        relative_parts = parts[3:]
    else:
        relative_parts = parts[2:]

    return "/".join(("output", model, date, *relative_parts))


def _blend_input_state(
    region: str,
    blend: BlendConfig,
    date: str,
    models_state: dict,
) -> list[dict]:
    return _forecast_input_state(region, blend.inputs, date, models_state)


def _diagnostic_input_state(
    region: str,
    blend: BlendConfig,
    date: str,
    models_state: dict,
) -> list[dict]:
    return _forecast_input_state(
        region,
        blend.diagnostic_inputs or blend.inputs,
        date,
        models_state,
    )


def _forecast_input_state(
    region: str,
    inputs: tuple[ForecastInput, ...],
    date: str,
    models_state: dict,
) -> list[dict]:
    bucket = REGION_BUCKETS.get(region, "")
    states = []
    for input_ in inputs:
        model = _pipeline_model_for_blend_input(input_)
        path = _blend_input_bucket_path(region, input_, date)
        output_present = bool(bucket) and gcs_object_exists(bucket, path)
        marker_present = (
            model not in models_state
            or gcs_object_exists(GCS_COMMON_BUCKET, model_marker_path(model, region, date))
        )
        states.append({
            "model": model,
            "role": input_.role,
            "path": f"gs://{bucket}/{path}" if bucket else path,
            "output_present": output_present,
            "marker_present": marker_present,
            "present": output_present and marker_present,
        })
    return states


def latest_synced(region: str) -> str:
    bucket = REGION_BUCKETS.get(region)
    if not bucket:
        return ""
    return read_gcs_text(bucket, "latest.txt")


# ---------------------------------------------------------------------------
# Top-level state computation
# ---------------------------------------------------------------------------

def _ic_date_for_source(source: str, requested_date: str, lookback_days: int, today: datetime) -> str:
    if requested_date:
        return requested_date
    return latest_external_00z(source, lookback_days, today)


def _ic_source_in_use() -> set[str]:
    sources = set()
    for region_cfg in REGIONS.values():
        for model in region_cfg.get("models", []):
            sources.add(MODEL_IC_SOURCE[model])
    return sources


def _models_in_use() -> set[str]:
    models = set()
    for region_cfg in REGIONS.values():
        models.update(region_cfg.get("models", []))
    return models


def _regions_for_model(model: str) -> list[str]:
    return [
        r for r, cfg in REGIONS.items()
        if model in cfg.get("models", [])
    ]


def _regions_with_stage(stage: str) -> list[str]:
    return [
        r for r, cfg in REGIONS.items()
        if stage in cfg.get("stages", [])
    ]


def compute_state(requested_date: str, lookback_days: int, today: datetime) -> dict:
    if not GCS_COMMON_BUCKET:
        raise RuntimeError("GCS_COMMON_BUCKET environment variable is not set")
    if not REGIONS:
        raise RuntimeError("REGIONS environment variable is not set or is empty")

    sources = _ic_source_in_use()
    ic_state = {}
    ic_dates: dict[str, str] = {}
    for source in ("ecmwf", "ncep"):
        if source not in sources:
            continue
        date_for_source = _ic_date_for_source(source, requested_date, lookback_days, today)
        ic_dates[source] = date_for_source
        missing_paths = _missing_ic_paths(source, date_for_source) if date_for_source else []
        ic_state[source] = {
            "date": date_for_source,
            "present": bool(date_for_source) and not missing_paths,
            "missing": missing_paths,
        }

    primary_date = requested_date or _primary_date(ic_dates)

    models_state = {}
    for model in _models_in_use():
        source = MODEL_IC_SOURCE[model]
        model_date = ic_dates.get(source, "")
        model_ic_present = bool(model_date) and ic_state.get(source, {}).get("present", False)
        dispatch_status = tpu_dispatch_status(model, model_date)
        dispatch_active = tpu_dispatch_active(
            dispatch_status,
            datetime.now(timezone.utc),
        )
        region_results = {}
        for region in _regions_for_model(model):
            present = bool(model_date) and model_region_done(model, region, model_date)
            region_results[region] = {
                "present": present,
                "date": model_date,
                "output_present": bool(model_date) and model_region_outputs_present(model, region, model_date),
                "marker_present": bool(model_date) and gcs_object_exists(
                    GCS_COMMON_BUCKET,
                    model_marker_path(model, region, model_date),
                ),
            }
        complete = model_ic_present and all(r["present"] for r in region_results.values())
        models_state[model] = {
            "date": model_date,
            "ic_source": source,
            "ic_present": model_ic_present,
            "complete": complete,
            "dispatch_active": dispatch_active,
            "dispatch_status": dispatch_status,
            "regions": region_results,
        }

    per_region = {}
    for region, cfg in REGIONS.items():
        block: dict = {}
        stages = cfg.get("stages", [])
        if "blend" in stages:
            block["blend"] = _blend_state_for_region(region, models_state, primary_date)
        if "model_diagnostics" in stages:
            block["model_diagnostics"] = _diagnostics_state_for_region(region, models_state, primary_date)
        if "sync" in stages:
            sync_inventory = _sync_inventory_for_region(
                region,
                cfg,
                models_state,
                per_region_block=block,
            )
            sync_target = sync_inventory.get("date", "")
            latest = latest_synced(region)
            synced_fingerprint = (
                read_gcs_text(REGION_BUCKETS.get(region, ""), sync_state_path(sync_target))
                if sync_target
                else ""
            )
            block["sync"] = {
                "date": sync_target,
                "latest": latest,
                "fingerprint": sync_inventory.get("fingerprint", ""),
                "items": sync_inventory.get("items", []),
                "synced_fingerprint": synced_fingerprint,
                "needs_run": bool(sync_target)
                and bool(sync_inventory.get("fingerprint", ""))
                and sync_inventory.get("fingerprint", "") != synced_fingerprint,
            }
        per_region[region] = block

    actions = _derive_actions(ic_state, models_state, per_region)

    return {
        "date": primary_date,
        "ic": ic_state,
        "models": models_state,
        "per_region": per_region,
        "actions": actions,
    }


def _primary_date(ic_dates: dict[str, str]) -> str:
    """Most workflows treat a single date as 'the' forecast date.
    Prefer NCEP (NeuralGCM-paced) when present; fall back to ECMWF."""
    return ic_dates.get("ncep") or ic_dates.get("ecmwf") or ""


def _missing_ic_paths(source: str, date: str) -> list[str]:
    if not date:
        return []
    paths = ic_ecmwf_paths(date) if source == "ecmwf" else ic_ncep_paths(date)
    if source == "ecmwf" and "gencast" in _models_in_use():
        paths.append(f"ic/gencast_sst/{date}/sst_{date}.nc")
    return [p for p in paths if not gcs_object_exists(GCS_COMMON_BUCKET, p)]


def _blend_state_for_region(region: str, models_state: dict, fallback_date: str) -> dict:
    """Blend readiness follows the configured blend/utils/main.py inputs."""
    _ = fallback_date
    configured_models = set(REGIONS.get(region, {}).get("models", []))
    configured = [
        blend for blend in BLENDS
        if blend.region == region
        and blend.blend_implemented
        and _pipeline_models_for_blend(blend).issubset(configured_models)
    ]
    if not configured:
        return {
            "date": "",
            "present": False,
            "configured": False,
            "names": [],
            "ready": [],
            "complete": [],
            "inputs": [],
            "missing": [],
        }

    items = [
        _config_state_for_region(
            region,
            blend,
            tuple(blend.inputs),
            models_state,
            present_fn=blend_config_present,
        )
        for blend in configured
    ]
    ready = [item for item in items if item["ready"]]
    complete = [item for item in items if item["present"]]
    missing = [
        state
        for item in items
        if not item["present"] and not item["ready"]
        for state in item.get("missing", [])
    ]
    action_items = ready or [
        item
        for item in items
        if item["date"] and not item["present"] and not item["ready"]
    ][:1]
    action_date = _single_action_date(action_items)
    return {
        "date": action_date if ready else "",
        "present": len(complete) == len(items),
        "configured": True,
        "names": [blend.name for blend in configured],
        "ready": ready,
        "complete": complete,
        "items": items,
        "inputs": [state for item in items for state in item.get("inputs", [])],
        "missing": missing,
    }


def _diagnostics_state_for_region(region: str, models_state: dict, fallback_date: str) -> dict:
    """Diagnostics readiness follows blend/utils/main.py diagnostic definitions."""
    _ = fallback_date
    configured_models = set(REGIONS.get(region, {}).get("models", []))
    configured = [
        blend for blend in BLENDS
        if blend.region == region
        and blend.diagnostic_plots
        and _pipeline_models_for_blend(blend).issubset(configured_models)
    ]
    if not configured:
        return {
            "date": "",
            "present": False,
            "configured": False,
            "names": [],
            "ready": [],
            "complete": [],
            "inputs": [],
            "missing": [],
        }

    items = [
        _config_state_for_region(
            region,
            blend,
            blend.diagnostic_inputs or blend.inputs,
            models_state,
            present_fn=diagnostics_config_present,
        )
        for blend in configured
    ]
    ready = [item for item in items if item["ready"]]
    complete = [item for item in items if item["present"]]
    missing = [
        state
        for item in items
        if not item["present"] and not item["ready"]
        for state in item.get("missing", [])
    ]
    action_items = ready or [
        item
        for item in items
        if item["date"] and not item["present"] and not item["ready"]
    ][:1]
    action_date = _single_action_date(action_items)
    return {
        "date": action_date if ready else "",
        "present": len(complete) == len(items),
        "configured": True,
        "names": [blend.name for blend in configured],
        "ready": ready,
        "complete": complete,
        "items": items,
        "inputs": [state for item in items for state in item.get("inputs", [])],
        "missing": missing,
    }


def _config_state_for_region(
    region: str,
    blend: BlendConfig,
    inputs: tuple[ForecastInput, ...],
    models_state: dict,
    present_fn,
) -> dict:
    candidate = _candidate_date_for_inputs(inputs, models_state)
    input_states = _forecast_input_state(region, inputs, candidate, models_state) if candidate else []
    missing = [state for state in input_states if not state["present"]]
    present = bool(candidate) and present_fn(region, candidate, blend)
    ready = bool(candidate) and not present and not missing
    return {
        "name": blend.name,
        "date": candidate,
        "present": present,
        "ready": ready,
        "inputs": input_states,
        "missing": missing,
    }


def _candidate_date_for_inputs(
    inputs: tuple[ForecastInput, ...],
    models_state: dict,
) -> str:
    known_dates = []
    for input_ in inputs:
        model = _pipeline_model_for_blend_input(input_)
        model_date = models_state.get(model, {}).get("date", "")
        if not model_date:
            return ""
        known_dates.append(model_date)
    if len(set(known_dates)) != 1:
        return ""
    return known_dates[0] if known_dates else ""


def _single_action_date(items: list[dict]) -> str:
    dates = {item.get("date", "") for item in items}
    dates.discard("")
    return next(iter(dates)) if len(dates) == 1 else ""


def _ready_items_for_action(items: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        date = item.get("date", "")
        if date:
            grouped.setdefault(date, []).append(item)
    if not grouped:
        return []
    return grouped[sorted(grouped)[-1]]


def _job_suffix(items: list[dict]) -> str:
    names = sorted(item["name"] for item in items)
    return hashlib.sha1(",".join(names).encode("utf-8")).hexdigest()[:8]


def _sync_inventory_for_region(
    region: str,
    cfg: dict,
    models_state: dict,
    per_region_block: dict,
) -> dict:
    """Return the latest completed sync-relevant inventory for a region.

    Sync is intentionally incremental: any completed model, blend, or
    diagnostics item can advance the inventory fingerprint for a date and
    trigger another same-date sync pass.
    """
    if region not in REGION_BUCKETS:
        return {"date": "", "items": [], "fingerprint": ""}

    items_by_date: dict[str, list[dict]] = {}
    for model in cfg.get("models", []):
        model_date = models_state.get(model, {}).get("date", "")
        if model_date and model_region_done(model, region, model_date):
            items_by_date.setdefault(model_date, []).append({
                "type": "model",
                "name": model,
            })

    for stage_name, block_name in (
        ("blend", "blend"),
        ("model_diagnostics", "model_diagnostics"),
    ):
        block = per_region_block.get(block_name, {})
        for item in block.get("complete", []):
            item_date = item.get("date", "")
            item_name = item.get("name", "")
            if item_date and item_name:
                items_by_date.setdefault(item_date, []).append({
                    "type": stage_name,
                    "name": item_name,
                })

    if not items_by_date:
        return {"date": "", "items": [], "fingerprint": ""}

    date = sorted(items_by_date)[-1]
    items = sorted(items_by_date[date], key=lambda item: (item["type"], item["name"]))
    fingerprint = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return {"date": date, "items": items, "fingerprint": fingerprint}


def _derive_actions(ic_state: dict, models_state: dict, per_region: dict) -> dict:
    ic_to_download = []
    ic_to_download_by_source = {}
    blocked = []

    for source, state in ic_state.items():
        action = {"source": source, "date": "", "missing": []}
        if not state.get("date"):
            blocked.append({"type": "ic_unavailable", "source": source})
        elif not state.get("present"):
            action = {
                "source": source,
                "date": state["date"],
                "missing": state.get("missing", []),
            }
            ic_to_download.append(action)
        ic_to_download_by_source[source] = action

    models_to_run = []
    models_to_run_by_model = {}
    for model, m in models_state.items():
        action = {"model": model, "date": "", "regions": []}
        if not m["date"]:
            blocked.append({"type": "model_blocked", "model": model, "reason": "ic_unavailable"})
            models_to_run_by_model[model] = action
            continue
        if not m.get("ic_present"):
            blocked.append({
                "type": "model_blocked",
                "model": model,
                "date": m["date"],
                "reason": "ic_missing",
                "ic_source": m.get("ic_source", ""),
            })
            models_to_run_by_model[model] = action
            continue
        pending_regions = [r for r, info in m["regions"].items() if not info["present"]]
        if pending_regions and m.get("dispatch_active"):
            blocked.append({
                "type": "model_in_progress",
                "model": model,
                "date": m["date"],
                "regions": pending_regions,
                "run_id": m.get("dispatch_status", {}).get("run_id", ""),
                "state": m.get("dispatch_status", {}).get("state", ""),
            })
        elif pending_regions:
            action = {"model": model, "date": m["date"], "regions": pending_regions}
            models_to_run.append(action)
        models_to_run_by_model[model] = action

    regions_to_blend = []
    regions_to_blend_by_region = {}
    for region, block in per_region.items():
        action = {"region": region, "date": "", "blends": [], "job_suffix": ""}
        ready = _ready_items_for_action(block.get("blend", {}).get("ready", []))
        ready_date = _single_action_date(ready)
        if ready and ready_date:
            action = {
                "region": region,
                "date": ready_date,
                "blends": [item["name"] for item in ready],
                "job_suffix": _job_suffix(ready),
            }
            regions_to_blend.append(action)
        elif "blend" in block and block["blend"].get("configured") and block["blend"].get("missing"):
            blocked.append({
                "type": "blend_blocked",
                "region": region,
                "blends": block["blend"].get("names", []),
                "reason": "inputs_missing",
                "missing": block["blend"].get("missing", []),
            })
        regions_to_blend_by_region[region] = action

    regions_to_diagnose = []
    regions_to_diagnose_by_region = {}
    for region, block in per_region.items():
        action = {"region": region, "date": "", "blends": [], "job_suffix": ""}
        diagnostics = block.get("model_diagnostics")
        ready = _ready_items_for_action(diagnostics.get("ready", []) if diagnostics else [])
        ready_date = _single_action_date(ready)
        if ready and ready_date:
            action = {
                "region": region,
                "date": ready_date,
                "blends": [item["name"] for item in ready],
                "job_suffix": _job_suffix(ready),
            }
            regions_to_diagnose.append(action)
        elif diagnostics and diagnostics.get("configured") and diagnostics.get("missing"):
            blocked.append({
                "type": "model_diagnostics_blocked",
                "region": region,
                "diagnostics": diagnostics.get("names", []),
                "reason": "inputs_missing",
                "missing": diagnostics.get("missing", []),
            })
        regions_to_diagnose_by_region[region] = action

    regions_to_sync = []
    regions_to_sync_by_region = {}
    for region, block in per_region.items():
        action = {"region": region, "date": "", "fingerprint": ""}
        if "sync" in block and block["sync"].get("needs_run"):
            action = {
                "region": region,
                "date": block["sync"]["date"],
                "fingerprint": block["sync"].get("fingerprint", ""),
            }
            regions_to_sync.append(action)
        regions_to_sync_by_region[region] = action

    return {
        "ic_to_download": ic_to_download,
        "ic_to_download_by_source": ic_to_download_by_source,
        "models_to_run": models_to_run,
        "models_to_run_by_model": models_to_run_by_model,
        "regions_to_blend": regions_to_blend,
        "regions_to_blend_by_region": regions_to_blend_by_region,
        "regions_to_diagnose": regions_to_diagnose,
        "regions_to_diagnose_by_region": regions_to_diagnose_by_region,
        "regions_to_sync": regions_to_sync,
        "regions_to_sync_by_region": regions_to_sync_by_region,
        "blocked": blocked,
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def parse_lookback(query: dict[str, list[str]]) -> int:
    raw = query.get("lookback_days", [str(DEFAULT_LOOKBACK_DAYS)])[0]
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_LOOKBACK_DAYS
    return max(1, min(parsed, 30))


def parse_date(query: dict[str, list[str]]) -> str:
    raw = query.get("date", [""])[0].strip()
    if not raw:
        return ""
    datetime.strptime(raw, "%Y%m%dT%H")
    return raw


def parse_today(query: dict[str, list[str]]) -> datetime:
    raw = query.get("today", [""])[0]
    if not raw:
        return datetime.now(timezone.utc)
    return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.respond(200, {"status": "ok"})
            return
        if parsed.path != "/state":
            self.respond(404, {"error": "not_found"})
            return

        query = parse_qs(parsed.query)
        try:
            lookback_days = parse_lookback(query)
            date = parse_date(query)
            today = parse_today(query)
            state = compute_state(date, lookback_days, today)
        except ValueError as exc:
            self.respond(400, {"error": "bad_request", "detail": str(exc)})
            return
        except Exception as exc:
            logger.exception("state_computation_failed")
            self.respond(500, {"error": "internal_error", "detail": str(exc)})
            return

        self.respond(200, state)

    def log_message(self, fmt: str, *args: object) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    logger.info("Starting pipeline-state service on port %s", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
