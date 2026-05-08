import argparse
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)

SCOPES = ["https://www.googleapis.com/auth/drive"]
AUTH_DIR = Path(__file__).resolve().parent.parent / ".auth"
CREDENTIALS_FILE = AUTH_DIR / "credentials.json"
TOKEN_FILE = AUTH_DIR / "token.json"
NUM_API_RETRIES = 3

AIFS_DRIVE_PATH = "/AIFS_forecast/tp"
AIFS_LOCAL_DIR = Path("AIFS") / "output" / "india" / "tp"


def authenticate():
    """Create an authenticated Google Drive API service."""
    creds = None

    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Credentials file '{CREDENTIALS_FILE}' not found. "
            "Please download it from Google Cloud Console."
        )

    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception as e:
            logging.error(f"Error loading token file '{TOKEN_FILE}': {e}")

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logging.info("Token refreshed successfully.")
        except Exception as e:
            logging.error(f"Error refreshing token: {e}")
            try:
                TOKEN_FILE.unlink(missing_ok=True)
            except OSError as unlink_err:
                logging.error(f"Error deleting token file '{TOKEN_FILE}': {unlink_err}")
            creds = None

    if not creds or not creds.valid:
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0, open_browser=False)
        except Exception as e:
            logging.error(f"Error during authentication flow: {e}")
            return None

        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(creds.to_json())
            logging.info(f"Credentials saved to '{TOKEN_FILE}'")
        except IOError as e:
            logging.error(f"Error saving token file '{TOKEN_FILE}': {e}")

    if not creds or not creds.valid:
        logging.error("Authentication failed. Could not obtain valid credentials.")
        return None

    try:
        return build("drive", "v3", credentials=creds, num_retries=NUM_API_RETRIES)
    except HttpError as error:
        logging.error(f"Error building Drive service: {error}")
    except Exception as e:
        logging.error(f"Unexpected error building Drive service: {e}")

    return None


def drive_query_value(value):
    """Escape a string for use in a simple Drive API query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def get_or_create_folder_id(service, folder_name, parent_id="root"):
    """Find a Drive folder by name under a parent, creating it if needed."""
    safe_folder_name = drive_query_value(folder_name)
    query = (
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{safe_folder_name}' and "
        f"'{parent_id}' in parents and "
        "trashed=false"
    )

    try:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        folders = response.get("files", [])
        if folders:
            return folders[0]["id"]

        folder = (
            service.files()
            .create(
                body={
                    "name": folder_name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                },
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        folder_id = folder.get("id")
        logging.info(f"Created Drive folder '{folder_name}' with ID: {folder_id}")
        return folder_id
    except HttpError as error:
        logging.error(f"Error resolving Drive folder '{folder_name}': {error}")
    except Exception as e:
        logging.error(f"Unexpected error resolving Drive folder '{folder_name}': {e}")

    return None


def get_folder_id_by_path(service, path_string):
    """Resolve a slash-separated Drive path from root, creating folders as needed."""
    parent_id = "root"
    for part in [part for part in path_string.strip("/").split("/") if part]:
        parent_id = get_or_create_folder_id(service, part, parent_id)
        if parent_id is None:
            return None
    return parent_id


def get_drive_file(service, file_name, drive_folder_id):
    """Return the first matching non-folder Drive file in a folder."""
    safe_file_name = drive_query_value(file_name)
    query = (
        f"name='{safe_file_name}' and "
        f"'{drive_folder_id}' in parents and "
        "trashed=false and "
        "mimeType!='application/vnd.google-apps.folder'"
    )

    try:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = response.get("files", [])
        return files[0] if files else None
    except HttpError as error:
        logging.error(f"Error checking Drive file '{file_name}': {error}")
    except Exception as e:
        logging.error(f"Unexpected error checking Drive file '{file_name}': {e}")

    return None


def rename_drive_file(service, drive_file_id, current_name, new_name):
    """Rename an existing Drive file."""
    if current_name == new_name:
        return True

    try:
        renamed_file = (
            service.files()
            .update(
                fileId=drive_file_id,
                body={"name": new_name},
                fields="id, name",
                supportsAllDrives=True,
            )
            .execute()
        )
        logging.info(
            f"Renamed Drive file '{current_name}' to '{renamed_file.get('name')}'"
        )
        return True
    except HttpError as error:
        logging.error(
            f"Error renaming Drive file '{current_name}' to '{new_name}': {error}"
        )
    except Exception as e:
        logging.error(
            f"Unexpected error renaming Drive file '{current_name}' to '{new_name}': {e}"
        )

    return False


def upload_aifs_file(service, local_file_path, drive_folder_id, remote_file_name):
    """Upload one AIFS file, then rename the Drive object to the public name."""
    local_file_path = Path(local_file_path)
    source_file_name = local_file_path.name

    if not local_file_path.is_file():
        logging.error(f"Local AIFS file not found: {local_file_path}")
        return False

    if get_drive_file(service, remote_file_name, drive_folder_id):
        logging.info(f"Drive file '{remote_file_name}' already exists. Skipping upload.")
        return True

    existing_source_file = get_drive_file(service, source_file_name, drive_folder_id)
    if existing_source_file:
        logging.info(
            f"Drive file '{source_file_name}' already exists. Renaming to '{remote_file_name}'."
        )
        return rename_drive_file(
            service,
            existing_source_file["id"],
            source_file_name,
            remote_file_name,
        )

    media = MediaFileUpload(
        str(local_file_path),
        mimetype="application/octet-stream",
        resumable=True,
    )

    try:
        uploaded_file = (
            service.files()
            .create(
                body={"name": source_file_name, "parents": [drive_folder_id]},
                media_body=media,
                fields="id, name",
                supportsAllDrives=True,
            )
            .execute()
        )
        file_id = uploaded_file["id"]
        logging.info(f"Uploaded '{source_file_name}' to Drive with ID: {file_id}")

        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()
        logging.info(f"Set public read permission on file ID: {file_id}")

        return rename_drive_file(service, file_id, source_file_name, remote_file_name)
    except HttpError as error:
        logging.error(f"Error uploading AIFS file '{source_file_name}': {error}")
    except Exception as e:
        logging.error(f"Unexpected error uploading AIFS file '{source_file_name}': {e}")

    return False


def drive_sync(date, cluster=None):
    """Sync one AIFS tp file to Google Drive."""
    del cluster

    base = Path(__file__).resolve().parent.parent.parent
    local_file = base / AIFS_LOCAL_DIR / f"tp_0p25_{date}.nc"
    remote_file_name = f"tp_{date}.nc"

    logging.info(f"Syncing AIFS file: {local_file}")
    logging.info(f"Target Drive path: {AIFS_DRIVE_PATH}/{remote_file_name}")

    drive_service = authenticate()
    if not drive_service:
        logging.error("Could not authenticate with Google Drive.")
        return False

    drive_folder_id = get_folder_id_by_path(drive_service, AIFS_DRIVE_PATH)
    if not drive_folder_id:
        logging.error(f"Could not create or find Drive path '{AIFS_DRIVE_PATH}'.")
        return False

    return upload_aifs_file(
        drive_service,
        local_file,
        drive_folder_id,
        remote_file_name,
    )


def main():
    parser = argparse.ArgumentParser(description="Sync AIFS tp files to Google Drive.")
    parser.add_argument(
        "--date",
        required=True,
        nargs="+",
        help="Date(s) to upload in YYYYMMDDTHH format, for example 20260508T00.",
    )
    args = parser.parse_args()

    success = True
    for sync_date in args.date:
        if not drive_sync(sync_date):
            success = False
            break

    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
