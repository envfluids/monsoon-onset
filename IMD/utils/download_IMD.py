import requests
import re
from datetime import datetime
from pathlib import Path
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)


def get_data(out_dir):
    # URL of the web page containing the download link
    page_url = "https://mausam.imd.gov.in/responsive/all_india_forcast_bulletin.php"
    # Send a GET request to the web page
    response = requests.get(page_url)
    # Check if the request was successful
    if response.status_code == 200:
        # Get the content of the page
        page_content = response.text
        # Use a regular expression to find the download link
        match = re.search(r'<a id="default-block-btn".*?href="(.*?)".*?>', page_content)
        if match:
            # Extract the relative URL of the file
            relative_file_url = match.group(1)
            # Construct the full URL of the file
            file_url = "https://mausam.imd.gov.in/" + relative_file_url
            # Get the current date in YYYYMMDD format
            current_date = datetime.now().strftime("%Y%m%d")
            # Local path where the file will be saved with the current date appended
            local_filename = out_dir / f"AIWFB_{current_date}.pdf"

            if local_filename.exists():
                logging.info(f"File {local_filename} already exists. Exiting download.")
                return

            # Send a GET request to the file URL
            file_response = requests.get(file_url)
            # Check if the request was successful
            if file_response.status_code == 200:
                # Open a local file in binary write mode
                with open(local_filename, "wb") as f:
                    f.write(file_response.content)
                logging.info(
                    f"File downloaded successfully and saved as {local_filename}"
                )
            else:
                logging.error(
                    f"Failed to download the file. Status code: {file_response.status_code}"
                )
        else:
            logging.error("Download link not found on the page.")
    else:
        logging.error(
            f"Failed to retrieve the web page. Status code: {response.status_code}"
        )

    return current_date


def main():
    base = Path(__file__).resolve().parent.parent.parent
    out_dir = base / "IMD" / "output"
    drive_dir = base / "sync" / "utils"
    current_date = get_data(out_dir)
    if current_date:
        logging.info("Syncing IMD with Google Drive...")
        sys.path.append(str(drive_dir))
        from drive import drive_sync_IMD

        try:
            drive_sync_IMD(current_date)
        except Exception as e:
            logging.error(f"Error during Google Drive sync: {e}")


if __name__ == "__main__":
    main()
