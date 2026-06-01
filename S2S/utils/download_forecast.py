import logging
from pathlib import Path

from ecmwf.datastores import Client

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)


class S2SDataStore:
    def __init__(self, collection_id="s2s-forecasts"):
        self.client = Client()
        self.collection = self.client.get_collection(collection_id)

    def get_latest_date(self):
        self.latest_date = self.collection.end_datetime
        return self.latest_date

    def get_request(self, date, lead_times):
        request = {
            "origin": "ecmwf",
            "year": date.strftime("%Y"),
            "month": date.strftime("%m"),
            "day": date.strftime("%d"),
            "time": date.strftime("%H:%M"),
            "level_type": "single_level",
            "variable": ["total_precipitation"],
            "forecast_type": ["control_forecast", "perturbed_forecast"],
            "leadtime_hour": lead_times,
            "data_format": "grib",
        }
        return request

    def download_data(self, target_path: Path, date=None):
        lead_times = [str(val) for val in range(0, 1104 + 6, 6)]

        if date is None:
            date = self.latest_date

        request = self.get_request(date, lead_times)
        self.collection.submit(request).download(target=target_path)
        return True


def get_data(date=None):
    base_path = Path(__file__).resolve().parent.parent
    raw_data_path = base_path / "raw" / "grib"
    raw_data_path.mkdir(parents=True, exist_ok=True)

    data_store = S2SDataStore()
    latest_date = data_store.get_latest_date()
    latest_date_f = latest_date.strftime("%Y%m%dT%H")
    logging.info(f"Latest available date: {latest_date_f}")

    target_file = (
        raw_data_path / f"ifs_s2s_init_{latest_date.strftime('%Y%m%dT%H')}.grib"
    )
    if target_file.exists():
        logging.info(f"File {target_file.name} already exists. Skipping download.")
    else:
        logging.info(
            f"Downloading forecast data for date {latest_date_f} to {target_file.name}"
        )
        success = data_store.download_data(target_file, date=date)
        if success:
            logging.info(f"Successfully downloaded data for date {latest_date_f}")
            return latest_date_f
        else:
            logging.error(f"Failed to download data for date {latest_date_f}")
            return None


def main():
    logging.info("Starting ECMWF forecast data acquisition process...")
    # The return value is the date string (e.g., '20250615T00') or None
    latest_forecast_date = get_data()

    if latest_forecast_date:
        logging.info(
            f"Process complete. Latest forecast date processed: {latest_forecast_date}"
        )
    else:
        logging.info("Could not retrieve any new forecast data.")


if __name__ == "__main__":
    main()
