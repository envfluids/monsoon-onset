"""
Monsoon IC Checker

Lightweight Cloud Run service for probing latest available 00z ECMWF and NCEP
initial-condition source files without starting the downloader job.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 7
REQUEST_TIMEOUT_SECONDS = 20


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
    request = Request(url, method="HEAD", headers={"User-Agent": "monsoon-ic-checker/1.0"})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except URLError as exc:
        return f"url_error:{exc.reason}"
    except TimeoutError:
        return "timeout"


def latest_available_00z(source: str, lookback_days: int, today: datetime | None = None) -> dict:
    if today is None:
        today = datetime.now(timezone.utc)
    cursor = today.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    url_for_source = ecmwf_url if source == "ecmwf" else ncep_url

    checked = []
    for days_back in range(lookback_days):
        candidate = cursor - timedelta(days=days_back)
        date_str = candidate.strftime("%Y%m%dT%H")
        url = url_for_source(candidate)
        status = head_status(url)
        checked.append({"date": date_str, "status": status, "url": url})
        logger.info("Checked %s %s: %s", source, date_str, status)
        if status == 200:
            return {"date": date_str, "url": url, "checked": checked}

    return {"date": "", "url": "", "checked": checked}


def parse_lookback(query: dict[str, list[str]]) -> int:
    raw = query.get("lookback_days", [str(DEFAULT_LOOKBACK_DAYS)])[0]
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_LOOKBACK_DAYS
    return max(1, min(parsed, 30))


def parse_today(query: dict[str, list[str]]) -> datetime | None:
    raw = query.get("today", [""])[0]
    if not raw:
        return None
    parsed = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)
    return parsed


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.respond(200, {"status": "ok"})
            return
        if parsed.path != "/check":
            self.respond(404, {"error": "not_found"})
            return

        query = parse_qs(parsed.query)
        lookback_days = parse_lookback(query)
        today = parse_today(query)

        ecmwf = latest_available_00z("ecmwf", lookback_days, today)
        ncep = latest_available_00z("ncep", lookback_days, today)

        self.respond(
            200,
            {
                "ecmwf_date": ecmwf["date"],
                "ecmwf_url": ecmwf["url"],
                "ncep_date": ncep["date"],
                "ncep_url": ncep["url"],
                "lookback_days": lookback_days,
                "checked": {
                    "ecmwf": ecmwf["checked"],
                    "ncep": ncep["checked"],
                },
            },
        )

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
    logger.info("Starting IC checker on port %s", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
