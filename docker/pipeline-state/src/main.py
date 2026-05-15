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
      "aifs":     {"complete": true,  "regions": {"india":{"present":true},"ethiopia":{"present":true}}},
      "aifs_ens": {"complete": false, "regions": {"ethiopia":{"present":false}}},
      "neuralgcm":{"complete": true,  "regions": {"india":{"present":true}}}
    },
    "per_region": {
      "india":    {"blend": {"present":true,"date":"20260513T00"},
                   "sync":  {"present":true,"latest":"20260513T00","needs_run":false}},
      "ethiopia": {"sync":  {"present":true,"latest":"20260513T00","needs_run":false}}
    },
    "actions": {
      "ic_to_download": [{"source":"ecmwf","date":"20260513T00"}],
      "models_to_run": [{"model":"aifs","date":"20260513T00","regions":["india"]}],
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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from google.api_core.exceptions import NotFound
from google.cloud import storage


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

GCS_COMMON_BUCKET = os.environ.get("GCS_COMMON_BUCKET", "")
REGION_BUCKETS = json.loads(os.environ.get("GCS_REGION_BUCKETS", "{}"))
REGIONS = json.loads(os.environ.get("REGIONS", "{}"))
REGION_MODELS = json.loads(os.environ.get("REGION_MODELS", "{}"))

# Which IC source each model consumes
MODEL_IC_SOURCE = {
    "aifs":      "ecmwf",
    "aifs_ens":  "ecmwf",
    "gencast":   "ecmwf",
    "neuralgcm": "ncep",
}


# ---------------------------------------------------------------------------
# External IC probing
# ---------------------------------------------------------------------------

def ecmwf_url(date: datetime) -> str:
    ymd = date.strftime("%Y%m%d")
    stamp = date.strftime("%Y%m%d%H0000")
    return f"https://data.ecmwf.int/forecasts/{ymd}/00z/ifs/0p25/oper/{stamp}-0h-oper-fc.grib2"


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


def latest_external_00z(source: str, lookback_days: int, today: datetime) -> str:
    url_for = ecmwf_url if source == "ecmwf" else ncep_url
    cursor = today.replace(hour=0, minute=0, second=0, microsecond=0)
    n_retries = 6
    backoff = 1
    for days_back in range(lookback_days):
        candidate = cursor - timedelta(days=days_back)
        date_str = candidate.strftime("%Y%m%dT%H")
        status = head_status(url_for(candidate))
        logger.info("external_probe source=%s date=%s status=%s", source, date_str, status)
        if status == 200:
            return date_str
        if status == 429 and n_retries > 0:
            logger.warning(
                "external_probe_rate_limited source=%s date=%s backoff=%ss retries_left=%s",
                source, date_str, backoff, n_retries,
            )
            time.sleep(backoff)
            n_retries -= 1
            backoff *= 2
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


# ---------------------------------------------------------------------------
# Per-stage state probes
# ---------------------------------------------------------------------------

def ic_ecmwf_paths(date: str) -> list[str]:
    base = datetime.strptime(date, "%Y%m%dT%H")
    dates = [base - timedelta(hours=12), base - timedelta(hours=6), base]
    filenames = [d.strftime("%Y%m%d%H0000-0h-oper-fc.grib2") for d in dates]
    return [f"ic/ecmwf/{date}/grib/{f}" for f in filenames]


def ic_ncep_paths(date: str) -> list[str]:
    return [f"ic/ncep/{date}/gdas_{date}.pgrb2"]


def ic_present(source: str, date: str) -> bool:
    paths = ic_ecmwf_paths(date) if source == "ecmwf" else ic_ncep_paths(date)
    return all(gcs_object_exists(GCS_COMMON_BUCKET, p) for p in paths)


def model_marker_path(model: str, region: str, date: str) -> str:
    return f"intermediate/{model}_{region}_{date}_done"


def model_region_outputs_present(model: str, region: str, date: str) -> bool:
    bucket = REGION_BUCKETS.get(region)
    if not bucket:
        return False

    if model == "aifs" and region == "india":
        return gcs_object_exists(bucket, f"output/aifs/{date}/tp/tp_2p0_{date}.nc")
    if model == "aifs" and region == "ethiopia":
        return gcs_prefix_has_objects(bucket, f"output/aifs/{date}/AIFS/")
    if model == "aifs_ens" and region == "ethiopia":
        return gcs_prefix_has_objects(bucket, f"output/aifs_ens/{date}/AIFS_ENS/")
    if model == "neuralgcm" and region == "india":
        return gcs_object_exists(bucket, f"output/neuralgcm/{date}/tp/tp_2p0_{date}.nc")
    if model == "gencast":
        return gcs_object_exists(bucket, f"output/gencast/{date}/init_{date}.nc")

    return gcs_prefix_has_objects(bucket, f"output/{model}/{date}/")


def model_region_done(model: str, region: str, date: str) -> bool:
    return (
        gcs_object_exists(GCS_COMMON_BUCKET, model_marker_path(model, region, date))
        and model_region_outputs_present(model, region, date)
    )


def blend_present(region: str, date: str) -> bool:
    bucket = REGION_BUCKETS.get(region)
    if not bucket:
        return False
    return gcs_prefix_has_objects(bucket, f"output/blend/{date}/")


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
            "regions": region_results,
        }

    per_region = {}
    for region, cfg in REGIONS.items():
        block: dict = {}
        stages = cfg.get("stages", [])
        if "blend" in stages:
            blend_date_for_region = _blend_date_for_region(region, models_state)
            block["blend"] = {
                "date": blend_date_for_region,
                "present": bool(blend_date_for_region) and blend_present(region, blend_date_for_region),
            }
        if "sync" in stages:
            sync_target = _sync_date_for_region(region, cfg, models_state, per_region_block=block)
            latest = latest_synced(region)
            block["sync"] = {
                "date": sync_target,
                "latest": latest,
                "needs_run": bool(sync_target) and sync_target != latest,
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
    if source == "ecmwf":
        paths.append(f"ic/gencast_sst/{date}/sst_{date}.nc")
    return [p for p in paths if not gcs_object_exists(GCS_COMMON_BUCKET, p)]


def _blend_date_for_region(region: str, models_state: dict) -> str:
    """Blend is ready when ALL of the region's models have a per-region marker
    for the same date. Today only india uses blend with aifs + neuralgcm at the
    same calendar date."""
    region_models = REGIONS[region].get("models", [])
    candidate_dates: list[str] = []
    for model in region_models:
        date_for_model = models_state.get(model, {}).get("date", "")
        if not date_for_model:
            return ""
        candidate_dates.append(date_for_model)
    # All models must align on the same date for the blend to be valid.
    if len(set(candidate_dates)) != 1:
        return ""
    date = candidate_dates[0]
    for model in region_models:
        if not model_region_done(model, region, date):
            return ""
    return date


def _sync_date_for_region(
    region: str,
    cfg: dict,
    models_state: dict,
    per_region_block: dict,
) -> str:
    """Date to sync for the region — depends on the region's date_kind:
    - 'date': use the latest date where the region's blend output is present.
              (Today: india.)
    - 'aifs_date': use the AIFS date where AIFS markers for this region exist.
              (Today: ethiopia.)
    The region's own model set determines which marker(s) are required.
    """
    date_kind = cfg.get("sync", {}).get("date_kind", "date")
    if "blend" in cfg.get("stages", []) and "blend" in per_region_block:
        candidate = per_region_block["blend"].get("date", "")
        if candidate and per_region_block["blend"].get("present"):
            return candidate
        return ""
    # No blend stage — pick the latest date where every model for the region
    # has its completion marker.
    region_models = cfg.get("models", [])
    dates = {models_state.get(m, {}).get("date", "") for m in region_models}
    if "" in dates or len(dates) != 1:
        return ""
    date = next(iter(dates))
    if all(model_region_done(m, region, date) for m in region_models):
        return date
    _ = date_kind  # retained for future use when dates diverge
    return ""


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
        if pending_regions:
            action = {"model": model, "date": m["date"], "regions": pending_regions}
            models_to_run.append(action)
        models_to_run_by_model[model] = action

    regions_to_blend = []
    regions_to_blend_by_region = {}
    for region, block in per_region.items():
        action = {"region": region, "date": ""}
        if "blend" in block and block["blend"].get("date") and not block["blend"].get("present"):
            action = {"region": region, "date": block["blend"]["date"]}
            regions_to_blend.append(action)
        regions_to_blend_by_region[region] = action

    regions_to_sync = []
    regions_to_sync_by_region = {}
    for region, block in per_region.items():
        action = {"region": region, "date": ""}
        if "sync" in block and block["sync"].get("needs_run"):
            action = {"region": region, "date": block["sync"]["date"]}
            regions_to_sync.append(action)
        regions_to_sync_by_region[region] = action

    return {
        "ic_to_download": ic_to_download,
        "ic_to_download_by_source": ic_to_download_by_source,
        "models_to_run": models_to_run,
        "models_to_run_by_model": models_to_run_by_model,
        "regions_to_blend": regions_to_blend,
        "regions_to_blend_by_region": regions_to_blend_by_region,
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
