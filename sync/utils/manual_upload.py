from __future__ import annotations

import argparse
import logging
from pathlib import Path

try:
    from .drive import GoogleDriveClient
except ImportError:  # Supports python sync/utils/manual_upload.py
    from drive import GoogleDriveClient


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)
logger = logging.getLogger(__name__)


def upload_file(local_file: Path, drive_folder: str) -> dict:
    if not local_file.is_file():
        raise FileNotFoundError(f"Local file not found: {local_file}")

    drive_path = drive_folder.strip("/")
    client = GoogleDriveClient.authenticated()
    uploaded_file = client.upload_file(local_file, drive_path)
    logger.info(
        "Uploaded %s to Google Drive folder %s as %s",
        local_file,
        f"/{drive_path}" if drive_path else "/",
        uploaded_file.get("name"),
    )
    return uploaded_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manually upload a single local file to Google Drive."
    )
    parser.add_argument(
        "file",
        type=Path,
        help="Local file to upload, e.g. AIFS/raw/ifs_ic/input_state_20260513T00.pkl",
    )
    parser.add_argument(
        "drive_folder",
        nargs="?",
        default="",
        help="Optional Google Drive folder path. Defaults to Drive root.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    upload_file(args.file, args.drive_folder)


if __name__ == "__main__":
    main()
