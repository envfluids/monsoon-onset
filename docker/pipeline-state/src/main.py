"""
Monsoon Pipeline-State Service

Single-call HTTP endpoint that returns the full state of the pipeline for a
given region and (optional) date:

  GET /state?region=india                       discover latest 00z IC per source
  GET /state?region=india&date=20260430T00      use the supplied date directly
  GET /state?region=india&lookback_days=7       widen the discovery window
  GET /healthz                                  liveness probe

Response shape:

  {
    "region": "india",
    "models": {
      "aifs":      { "ic_date": "...", "ic_in_gcs": bool, "forecast_complete": bool, "missing": [...] },
      "neuralgcm": { "ic_date": "...", "ic_in_gcs": bool, "forecast_complete": bool, "missing": [...] }
    },
    "blend": { "date": "...", "needs_run": bool },
    "sync":  { "date": "...", "needs_run": bool }
  }

GCS layout assumed:
  <region>/raw/ecmwf/<date>/input_state_<date>.pkl
  <region>/raw/ncep/<date>/gdas_<date>.pgrb2
  <region>/output/<aifs|neuralgcm>/<date>/<sji|tcw|tp>/<sji|tcw|tp>_<date>.nc
  <region>/output/blend/<date>/blend_output_summary.csv
  <region>/latest.txt                 (last successfully synced date)
"""

import json
import logging
import os
import sys
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
FORECAST_FILES = ("sji", "tcw", "tp")

GCS_BUCKET = os.environ.get("GCS_BUCKET", "")


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
    """Walk back from `today` up to `lookback_days` and return the first available 00z date string, or ''."""
    url_for = ecmwf_url if source == "ecmwf" else ncep_url
    cursor = today.replace(hour=0, minute=0, second=0, microsecond=0)
    for days_back in range(lookback_days):
        candidate = cursor - timedelta(days=days_back)
        date_str = candidate.strftime("%Y%m%dT%H")
        status = head_status(url_for(candidate))
        logger.info("external_probe source=%s date=%s status=%s", source, date_str, status)
        if status == 200:
            return date_str
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


def read_gcs_text(bucket: str, path: str) -> str:
    try:
        return gcs_client().bucket(bucket).blob(path).download_as_text().strip()
    except NotFound:
        return ""


# ---------------------------------------------------------------------------
# Per-stage state probes
# ---------------------------------------------------------------------------

def ic_path(source: str, region: str, date: str) -> str:
    if source == "ecmwf":
        return f"{region}/raw/ecmwf/{date}/input_state_{date}.pkl"
    return f"{region}/raw/ncep/{date}/gdas_{date}.pgrb2"


def forecast_paths(model: str, region: str, date: str) -> list[str]:
    return [f"{region}/output/{model}/{date}/{kind}/{kind}_{date}.nc" for kind in FORECAST_FILES]


def blend_prefix(region: str, date: str) -> str:
    return f"{region}/output/blend/{date}/"


def blend_complete(bucket: str, region: str, date: str) -> bool:
    blobs = gcs_client().list_blobs(bucket, prefix=blend_prefix(region, date), max_results=1)
    return next(iter(blobs), None) is not None


def model_state(bucket: str, region: str, source: str, model: str, date: str) -> dict:
    if not date:
        return {"ic_date": "", "ic_in_gcs": False, "forecast_complete": False, "missing": []}

    ic_in_gcs = gcs_object_exists(bucket, ic_path(source, region, date))

    missing: list[str] = []
    for path in forecast_paths(model, region, date):
        if not gcs_object_exists(bucket, path):
            missing.append(path)

    return {
        "ic_date": date,
        "ic_in_gcs": ic_in_gcs,
        "forecast_complete": len(missing) == 0,
        "missing": missing,
    }


def latest_blendable_date(bucket: str, region: str, lookback_days: int, today: datetime) -> str:
    """Find the latest date where both forecasts are complete in GCS and the blend output is absent."""
    cursor = today.replace(hour=0, minute=0, second=0, microsecond=0)
    for days_back in range(lookback_days):
        candidate = cursor - timedelta(days=days_back)
        date_str = candidate.strftime("%Y%m%dT%H")
        aifs_complete = all(gcs_object_exists(bucket, p) for p in forecast_paths("aifs", region, date_str))
        ncep_complete = all(gcs_object_exists(bucket, p) for p in forecast_paths("neuralgcm", region, date_str))
        if not (aifs_complete and ncep_complete):
            continue
        if blend_complete(bucket, region, date_str):
            continue
        return date_str
    return ""


def latest_synced_date(bucket: str, region: str) -> str:
    return read_gcs_text(bucket, f"{region}/latest.txt")


def latest_blended_date(bucket: str, region: str, lookback_days: int, today: datetime) -> str:
    cursor = today.replace(hour=0, minute=0, second=0, microsecond=0)
    for days_back in range(lookback_days):
        candidate = cursor - timedelta(days=days_back)
        date_str = candidate.strftime("%Y%m%dT%H")
        if blend_complete(bucket, region, date_str):
            return date_str
    return ""


# ---------------------------------------------------------------------------
# Top-level state computation
# ---------------------------------------------------------------------------

def compute_state(region: str, date: str, lookback_days: int, today: datetime) -> dict:
    if not GCS_BUCKET:
        raise RuntimeError("GCS_BUCKET environment variable is not set")

    if date:
        ecmwf_date = date
        ncep_date = date
    else:
        ecmwf_date = latest_external_00z("ecmwf", lookback_days, today)
        ncep_date = latest_external_00z("ncep", lookback_days, today)

    aifs = model_state(GCS_BUCKET, region, "ecmwf", "aifs", ecmwf_date)
    neuralgcm = model_state(GCS_BUCKET, region, "ncep", "neuralgcm", ncep_date)

    blend_date = latest_blendable_date(GCS_BUCKET, region, lookback_days, today)
    if blend_date:
        blend = {"date": blend_date, "needs_run": True}
    else:
        blend = {"date": "", "needs_run": False}

    blended = latest_blended_date(GCS_BUCKET, region, lookback_days, today)
    synced = latest_synced_date(GCS_BUCKET, region)
    if blended and blended != synced:
        sync = {"date": blended, "needs_run": True}
    else:
        sync = {"date": "", "needs_run": False}

    return {
        "region": region,
        "models": {"aifs": aifs, "neuralgcm": neuralgcm},
        "blend": blend,
        "sync": sync,
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


def parse_region(query: dict[str, list[str]]) -> str:
    return query.get("region", ["india"])[0]


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
            region = parse_region(query)
            lookback_days = parse_lookback(query)
            date = parse_date(query)
            today = parse_today(query)
            state = compute_state(region, date, lookback_days, today)
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
