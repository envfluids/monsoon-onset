import os
import io
from pathlib import Path
import threading
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload
import logging
import argparse

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)

# --- Configuration Constants ---
# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/drive"]
# Ensure these files are in the same directory as the script, or provide full paths

auth_dir = Path(__file__).resolve().parent.parent / ".auth"

CREDENTIALS_FILE = auth_dir / "credentials.json"
TOKEN_FILE = auth_dir / "token.json"
NUM_API_RETRIES = 3

# --- Google Drive Helper Functions ---


def authenticate():
    """Handles authentication and returns the Drive API service object."""
    creds = None
    token_path = Path(TOKEN_FILE)
    credentials_path = Path(CREDENTIALS_FILE)

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Credentials file '{credentials_path}' not found. Please download it from Google Cloud Console."
        )
        return None

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as e:
            logging.error(f"Error loading token file '{token_path}': {e}")
            creds = None  # Ensure creds is None if loading fails

    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logging.warning("Token expired. Attempting to refresh token...")
            try:
                creds.refresh(Request())
                logging.info("Token refreshed successfully.")
            except Exception as e:
                logging.error(
                    f"Error refreshing token: {e}. Deleting invalid token file."
                )
                # If refresh fails, delete token and trigger re-authentication
                try:
                    token_path.unlink(missing_ok=True)
                except OSError as unlink_err:
                    logging.error(
                        f"Error deleting token file '{token_path}': {unlink_err}"
                    )
                creds = None  # Force re-authentication
        # --- This block now correctly handles the case where creds were invalid ---
        # Run flow if creds are None (initial run, failed load, failed refresh)
        if not creds:
            logging.warning(
                "No valid credentials found. Starting authentication flow..."
            )
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path), SCOPES
                )
                # Specify redirect_uri for desktop apps
                creds = flow.run_local_server(port=0)
                logging.info("Authentication flow completed successfully.")
            except FileNotFoundError:
                logging.error(f"Credentials file '{credentials_path}' not found.")
                return None
            except Exception as e:
                logging.error(f"Error during authentication flow: {e}")
                return None

        # Save the (newly obtained or refreshed) credentials for the next run
        if creds and creds.valid:  # Only save if we have valid credentials
            try:
                with token_path.open("w") as token:
                    token.write(creds.to_json())
                logging.info(f"Credentials saved to '{token_path}'")
            except IOError as e:
                logging.error(f"Error saving token file '{token_path}': {e}")

    if not creds or not creds.valid:
        logging.error("Authentication failed. Could not obtain valid credentials.")
        return None

    try:
        service = build(
            "drive",
            "v3",
            credentials=creds,
            num_retries=NUM_API_RETRIES,
            cache_discovery=False,
        )
        logging.info(
            f"Authentication successful. Drive service created. API calls will be retried up to {NUM_API_RETRIES} times on transient server errors (5xx)."
        )
        return service
    except HttpError as error:
        logging.error(f"Error building Drive service: {error}")
        return None
    except Exception as e:
        logging.error(f"An unexpected error occurred during service build: {e}")
        return None


def get_or_create_folder_id(service, folder_name, parent_id="root"):
    """Finds a folder by name within a parent, creates it if not found."""
    try:
        # Escape single quotes in folder name for the query
        safe_folder_name = folder_name.replace("'", "\\'")

        query = (
            f"mimeType='application/vnd.google-apps.folder' and "
            f"name='{safe_folder_name}' and "
            f"'{parent_id}' in parents and "
            f"trashed=false"
        )

        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                # Increase pageSize if many folders share name (unlikely here)
                pageSize=10,
            )
            .execute()
        )
        folders = response.get("files", [])

        if folders:
            # Found existing folder
            folder_id = folders[0].get("id")
            # print(f"Found folder '{folder_name}' with ID: {folder_id}") # Less verbose
            return folder_id
        else:
            # Folder not found, create it
            logging.info(
                f"Folder '{folder_name}' not found in parent '{parent_id}', creating..."
            )
            file_metadata = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            # Use supportsAllDrives=True if working with Shared Drives
            folder = (
                service.files()
                .create(body=file_metadata, fields="id", supportsAllDrives=True)
                .execute()
            )
            folder_id = folder.get("id")
            logging.info(f"Created folder '{folder_name}' with ID: {folder_id}")
            return folder_id
    except HttpError as error:
        # Specifically handle permission errors if helpful
        if error.resp.status == 403:
            logging.error(
                f"Permission error accessing/creating folder '{folder_name}' in parent '{parent_id}'. Check Drive permissions."
            )
        elif error.resp.status == 404:
            logging.error(
                f"Parent folder ID '{parent_id}' not found when searching for/creating '{folder_name}'."
            )
        else:
            logging.error(
                f"An HTTP error occurred finding/creating folder '{folder_name}': {error}"
            )
        return None
    except Exception as e:
        # Catch other potential errors (network issues, etc.)
        logging.error(f"An unexpected error occurred with folder '{folder_name}': {e}")
        return None


def get_folder_id_by_path(service, path_string):
    """Gets the ID of a folder specified by a '/' separated path from root."""
    logging.info(f"Resolving Drive path: '{path_string}'")
    # Normalize path: remove leading/trailing slashes and filter empty parts
    path_parts = [part for part in path_string.strip("/").split("/") if part]
    if not path_parts:
        logging.warning("Empty or root path provided.")
        return "root"  # Or handle as error if root isn't intended

    current_parent_id = "root"  # Always start from root for absolute paths
    current_resolved_path = "/"

    for part in path_parts:
        logging.info(
            f"Searching for folder '{part}' in parent '{current_parent_id}' (Path: '{current_resolved_path}')"
        )
        folder_id = get_or_create_folder_id(service, part, current_parent_id)
        if folder_id is None:
            logging.error(
                f"Failed to get or create folder part: '{part}' in path '{path_string}'"
            )
            return None  # Stop if any part fails
        current_parent_id = folder_id
        current_resolved_path += f"{part}/"

    logging.info(f"Resolved path '{path_string}' to folder ID: {current_parent_id}")
    return current_parent_id


def find_folder_id(service, folder_name, parent_id="root"):
    """Finds an existing folder by name within a parent without creating it."""
    try:
        safe_folder_name = folder_name.replace("'", "\\'")
        query = (
            f"mimeType='application/vnd.google-apps.folder' and "
            f"name='{safe_folder_name}' and "
            f"'{parent_id}' in parents and "
            f"trashed=false"
        )
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        folders = response.get("files", [])
        return folders[0].get("id") if folders else None
    except HttpError as error:
        logging.error(
            f"Error finding folder '{folder_name}' in parent '{parent_id}': {error}"
        )
        return None


def resolve_folder_path(service, path_string, create=False):
    """Resolve a Drive folder path. Optionally create missing folders."""
    path_parts = [part for part in path_string.strip("/").split("/") if part]
    if not path_parts:
        return "root"

    current_parent_id = "root"
    for part in path_parts:
        folder_id = (
            get_or_create_folder_id(service, part, current_parent_id)
            if create
            else find_folder_id(service, part, current_parent_id)
        )
        if folder_id is None:
            return None
        current_parent_id = folder_id
    return current_parent_id


class GoogleDriveClient:
    """Small path-oriented wrapper used by the configurable sync engine."""

    def __init__(self, service):
        self.service = service
        self._folder_id_cache = {"": "root"}
        self._folder_id_cache_lock = threading.RLock()

    @classmethod
    def authenticated(cls):
        service = authenticate()
        if not service:
            raise RuntimeError("Could not authenticate with Google Drive")
        return cls(service)

    def new_worker_client(self):
        return type(self).authenticated()

    def _resolve_folder_path(self, drive_path, create=False):
        path_parts = [part for part in drive_path.strip("/").split("/") if part]
        if not path_parts:
            return "root"

        current_parent_id = "root"
        current_path_parts = []
        for part in path_parts:
            current_path_parts.append(part)
            cache_key = "/".join(current_path_parts)
            with self._folder_id_cache_lock:
                cached_folder_id = self._folder_id_cache.get(cache_key)
            if cached_folder_id:
                current_parent_id = cached_folder_id
                continue

            folder_id = (
                get_or_create_folder_id(self.service, part, current_parent_id)
                if create
                else find_folder_id(self.service, part, current_parent_id)
            )
            if folder_id is None:
                return None

            with self._folder_id_cache_lock:
                self._folder_id_cache[cache_key] = folder_id
            current_parent_id = folder_id
        return current_parent_id

    def list_files(self, drive_path, create=False):
        folder_id = self._resolve_folder_path(drive_path, create=create)
        if folder_id is None:
            return {}

        files = {}
        page_token = None
        while True:
            response = (
                self.service.files()
                .list(
                    q=(
                        f"'{folder_id}' in parents and trashed=false and "
                        "mimeType!='application/vnd.google-apps.folder'"
                    ),
                    spaces="drive",
                    fields="nextPageToken, files(id, name, size, modifiedTime, md5Checksum)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for file_data in response.get("files", []):
                files[file_data["name"]] = file_data
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return files

    def upload_file(self, local_file_path, drive_path):
        folder_id = self._resolve_folder_path(drive_path, create=True)
        if folder_id is None:
            raise RuntimeError(f"Could not resolve Drive path: {drive_path}")

        file_path = Path(local_file_path)
        media = MediaFileUpload(
            str(file_path),
            mimetype="application/octet-stream",
            resumable=True,
        )
        metadata = {"name": file_path.name, "parents": [folder_id]}
        return (
            self.service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id, name, size, modifiedTime, md5Checksum",
                supportsAllDrives=True,
            )
            .execute()
        )

    def list_tree(self, drive_path):
        folder_id = self._resolve_folder_path(drive_path, create=False)
        if folder_id is None:
            return []
        rows = []
        self._list_tree(folder_id, drive_path.rstrip("/"), rows)
        return rows

    def _list_tree(self, folder_id, current_path, rows):
        page_token = None
        while True:
            response = (
                self.service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    spaces="drive",
                    fields=(
                        "nextPageToken, files(id, name, mimeType, size, "
                        "modifiedTime, md5Checksum)"
                    ),
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for item in response.get("files", []):
                item_path = f"{current_path}/{item['name']}"
                row = {**item, "path": item_path}
                rows.append(row)
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    self._list_tree(item["id"], item_path, rows)
            page_token = response.get("nextPageToken")
            if not page_token:
                break


def check_file_exists(service, file_name, drive_folder_id):
    """Checks if a file with the given name exists in the Drive folder."""
    try:
        # Escape single quotes in file name for the query
        safe_file_name = file_name.replace("'", "\\'")

        query = (
            f"name='{safe_file_name}' and "
            f"'{drive_folder_id}' in parents and "
            f"trashed=false and "
            f"mimeType!='application/vnd.google-apps.folder'"
        )  # Ensure it's not a folder

        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id)",
                pageSize=1,  # We only need to know if at least one exists
            )
            .execute()
        )
        return bool(response.get("files", []))  # True if list is not empty
    except HttpError as error:
        logging.error(
            f"Error checking existence for file '{file_name}' in folder '{drive_folder_id}': {error}"
        )
        return False  # Assume not found on error to potentially allow upload attempt
    except Exception as e:
        logging.error(
            f"Unexpected error checking file existence for '{file_name}': {e}"
        )
        return False


def upload_file(service, local_file_path, drive_folder_id):
    """Uploads a single file *only if* it doesn't already exist in the target Google Drive folder."""
    file_path = Path(local_file_path)
    if not file_path.is_file():
        logging.error(f"Skipping: Local file not found: {file_path}")
        return None

    file_name = file_path.name

    # --- Check if file already exists in Drive ---
    logging.info(
        f"Checking if '{file_name}' exists in Drive folder ID '{drive_folder_id}'..."
    )
    if check_file_exists(service, file_name, drive_folder_id):
        logging.info(
            f"File '{file_name}' already exists in Drive folder '{drive_folder_id}'. Skipping upload."
        )
        return None  # Indicate skipped
    # --- End check ---

    logging.info(f"Uploading '{file_name}' to folder ID '{drive_folder_id}'...")
    file_metadata = {"name": file_name, "parents": [drive_folder_id]}
    # Use application/octet-stream as a generic binary type, or let Drive guess
    # For netCDF, 'application/x-netcdf' might be more specific if needed.
    media = MediaFileUpload(
        str(file_path),
        mimetype="application/octet-stream",  # Adjust if needed
        resumable=True,
    )
    try:
        # Use supportsAllDrives=True if working with Shared Drives
        file = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name",
                supportsAllDrives=True,
            )
            .execute()
        )
        logging.info(
            f"Uploaded '{file_name}' to Drive folder ID '{drive_folder_id}' with ID: {file.get('id')}"
        )
        return file.get("id")
    except HttpError as error:
        logging.error(
            f"Error uploading file '{file_name}' to Drive folder '{drive_folder_id}': {error}"
        )
        # Consider retries for transient errors (e.g., 5xx)
        return None
    except Exception as e:
        logging.error(f"Unexpected error uploading file '{file_name}': {e}")
        return None


def upload_directory_recursive(service, local_dir_path, drive_parent_folder_id):
    """Recursively uploads a local directory's contents to a Drive folder, skipping existing files."""
    local_path = Path(local_dir_path)
    if not local_path.is_dir():
        logging.error(
            f"Error: Local directory not found or is not a directory: {local_path}"
        )
        return

    logging.info(
        f"Processing directory '{local_path}' for upload to Drive folder ID '{drive_parent_folder_id}'"
    )

    # Using os.walk is generally robust for traversing directories
    for root, dirs, files in os.walk(str(local_path)):
        current_local_root = Path(root)
        # Calculate the relative path from the starting local directory
        relative_path = current_local_root.relative_to(local_path)

        # Determine the corresponding Drive folder ID for the current level
        current_drive_folder_id = drive_parent_folder_id
        if relative_path != Path("."):  # If not the top-level dir passed initially
            # Need to create/find the folder structure in Drive
            parent_drive_id_for_current = drive_parent_folder_id
            for part in relative_path.parts:
                logging.info(
                    f"Creating/finding Drive subfolder '{part}' for path '{relative_path}'"
                )
                folder_id = get_or_create_folder_id(
                    service, part, parent_drive_id_for_current
                )
                if folder_id is None:
                    logging.error(
                        f"Failed to create/find Drive subfolder '{part}' for path '{relative_path}'. Skipping contents."
                    )
                    # Skip processing dirs and files within this failed path part
                    dirs[
                        :
                    ] = []  # Stop os.walk from descending further into this branch
                    files[:] = []
                    current_drive_folder_id = None  # Mark as invalid
                    break  # Stop processing parts for this relative path
                parent_drive_id_for_current = (
                    folder_id  # Update parent for the next part
                )
            current_drive_folder_id = parent_drive_id_for_current

        if current_drive_folder_id is None:
            continue  # Skip to the next item from os.walk if folder creation failed

        # Process files in the current directory
        for filename in files:
            local_file = current_local_root / filename
            # upload_file now contains the existence check
            upload_file(service, local_file, current_drive_folder_id)

        # Note: os.walk handles iterating into 'dirs'. We just need to ensure
        # the corresponding Drive folders are created, which is done above
        # when processing the 'root'.


def drive_sync(date, cluster):  # Added cluster parameter with default
    """Main function to perform the sync operation for a given date and cluster."""

    # Define the base Google Drive folder path using date and cluster
    # Example: /MO Forecast Benchmarking/operational_data_2026/{cluster}
    DRIVE_CLUSTER_BASE_PATH = f"/MO_operational_data_2026/{cluster}"

    # Define local paths
    try:
        # Assumes script is like project_root/scripts/sync_script.py
        # Adjust if your structure is different
        script_dir = Path(__file__).resolve().parent
        base = script_dir.parent.parent  # project_root
    except NameError:
        # Fallback for interactive sessions or environments where __file__ isn't set
        logging.warning("Using current working directory's parent as project base.")
        # This assumes you run interactively from the 'scripts' dir
        base = Path.cwd().parent
        if not (base / "AIFS").exists():  # Basic sanity check
            logging.warning(
                "Base path might be incorrect. Expected AIFS folder not found."
            )

    logging.info(f"Using project base path: {base}")
    logging.info(f"Syncing for date: {date}, cluster: {cluster}")
    logging.info(f"Target Drive path: {DRIVE_CLUSTER_BASE_PATH}/{date}")

    # --- Define Local File/Directory Sources ---
    AIFS_output_path = base / "AIFS" / "output"
    AIFS_FILES_TO_UPLOAD = {
        # Using Path objects directly is generally better
        "AIFS": [
            AIFS_output_path / "tp" / f"tp_{date}.nc",
            AIFS_output_path / "sji" / f"sji_{date}.nc",
            AIFS_output_path / "tcw" / f"tcw_{date}.nc",
        ]
    }

    NGCM_output_path = base / "NeuralGCM" / "output"
    NGCM_FILES_TO_UPLOAD = {
        "NeuralGCM": [
            NGCM_output_path / "tp" / f"tp_{date}.nc",
            NGCM_output_path / "sji" / f"sji_{date}.nc",
            NGCM_output_path / "tcw" / f"tcw_{date}.nc",
        ]
    }

    blend_output_path = base / "blend" / "output"
    blend_date_dir_local_path = blend_output_path / date
    BLEND_DIR_TO_UPLOAD = {
        # Key is the target folder name in Drive under the date folder
        "blend": blend_date_dir_local_path
    }

    blend_google_output_path = base / "blend" / "output_google"
    blend_google_date_dir_local_path = blend_google_output_path / date
    BLEND_GOOGLE_DIR_TO_UPLOAD = {
        # Key is the target folder name in Drive under the date folder
        "blend_google": blend_google_date_dir_local_path
    }

    logging.info("Starting Google Drive authentication process...")
    drive_service = authenticate()

    if drive_service:
        logging.info("Google Drive authentication successful.")
        logging.info(f"Starting Google Drive sync process...")
        # 1. Get the ID for the cluster base path (e.g., .../operational_data/midway)
        cluster_base_drive_folder_id = get_folder_id_by_path(
            drive_service, DRIVE_CLUSTER_BASE_PATH
        )

        if cluster_base_drive_folder_id:
            # 2. Create or get the date-specific folder inside the cluster base path
            logging.info(
                f"Ensuring date folder '{date}' exists under '{DRIVE_CLUSTER_BASE_PATH}'..."
            )
            date_folder_id = get_or_create_folder_id(
                drive_service, date, cluster_base_drive_folder_id
            )

            if date_folder_id:
                # --- Sync Operations ---

                # 3. Upload AIFS files
                logging.info("Processing AIFS files...")
                aifs_target_folder_name = list(AIFS_FILES_TO_UPLOAD.keys())[0]
                logging.info(
                    f"Ensuring Drive folder '{aifs_target_folder_name}' exists..."
                )
                aifs_drive_folder_id = get_or_create_folder_id(
                    drive_service, aifs_target_folder_name, date_folder_id
                )
                if aifs_drive_folder_id:
                    logging.info(
                        f"Uploading AIFS files to folder ID: {aifs_drive_folder_id}"
                    )
                    for file_path in AIFS_FILES_TO_UPLOAD[aifs_target_folder_name]:
                        upload_file(drive_service, file_path, aifs_drive_folder_id)
                else:
                    logging.error(
                        f"ERROR: Could not create/find folder '{aifs_target_folder_name}', skipping AIFS uploads."
                    )

                # 4. Upload NeuralGCM files
                logging.info("Processing NeuralGCM files...")
                ngcm_target_folder_name = list(NGCM_FILES_TO_UPLOAD.keys())[0]
                logging.info(
                    f"Ensuring Drive folder '{ngcm_target_folder_name}' exists..."
                )
                ngcm_drive_folder_id = get_or_create_folder_id(
                    drive_service, ngcm_target_folder_name, date_folder_id
                )
                if ngcm_drive_folder_id:
                    logging.info(
                        f"Uploading NeuralGCM files to folder ID: {ngcm_drive_folder_id}"
                    )
                    for file_path in NGCM_FILES_TO_UPLOAD[ngcm_target_folder_name]:
                        upload_file(drive_service, file_path, ngcm_drive_folder_id)
                else:
                    logging.error(
                        f"ERROR: Could not create/find folder '{ngcm_target_folder_name}', skipping NeuralGCM uploads."
                    )

                # 5. Upload blend directory contents
                logging.info("Processing blend directory...")
                blend_target_folder_name = list(BLEND_DIR_TO_UPLOAD.keys())[0]
                blend_local_source_dir = BLEND_DIR_TO_UPLOAD[blend_target_folder_name]
                logging.info(
                    f"Ensuring Drive folder '{blend_target_folder_name}' exists..."
                )
                blend_drive_folder_id = get_or_create_folder_id(
                    drive_service, blend_target_folder_name, date_folder_id
                )

                if blend_drive_folder_id:
                    if blend_local_source_dir.is_dir():
                        logging.info(
                            f"Uploading contents of '{blend_local_source_dir}' to Drive folder ID: {blend_drive_folder_id}"
                        )
                        # Pass the starting local path and the target Drive folder ID
                        upload_directory_recursive(
                            drive_service, blend_local_source_dir, blend_drive_folder_id
                        )
                    else:
                        logging.error(
                            f"ERROR: Local blend directory '{blend_local_source_dir}' not found or is not a directory. Skipping blend upload."
                        )
                else:
                    logging.error(
                        f"ERROR: Could not create/find folder '{blend_target_folder_name}', skipping blend upload."
                    )
                
                # 6. Upload blend-google directory contents
                logging.info("Processing blend-google directory...")
                blend_google_target_folder_name = list(BLEND_GOOGLE_DIR_TO_UPLOAD.keys())[0]
                blend_google_local_source_dir = BLEND_GOOGLE_DIR_TO_UPLOAD[blend_google_target_folder_name]
                logging.info(
                    f"Ensuring Drive folder '{blend_google_target_folder_name}' exists..."
                )
                blend_google_drive_folder_id = get_or_create_folder_id(
                    drive_service, blend_google_target_folder_name, date_folder_id
                )

                if blend_google_drive_folder_id:
                    if blend_google_local_source_dir.is_dir():
                        logging.info(
                            f"Uploading contents of '{blend_google_local_source_dir}' to Drive folder ID: {blend_google_drive_folder_id}"
                        )
                        # Pass the starting local path and the target Drive folder ID
                        upload_directory_recursive(
                            drive_service, blend_google_local_source_dir, blend_google_drive_folder_id
                        )
                    else:
                        logging.error(
                            f"ERROR: Local blend directory '{blend_google_local_source_dir}' not found or is not a directory. Skipping blend upload."
                        )
                else:
                    logging.error(
                        f"ERROR: Could not create/find folder '{blend_google_target_folder_name}', skipping blend upload."
                    )

                logging.info("Sync process finished.")
            else:
                logging.error(
                    f"CRITICAL: Could not create or find the main date folder '{date}' under '{DRIVE_CLUSTER_BASE_PATH}'. Aborting."
                )
        else:
            logging.error(
                f"CRITICAL: Could not create or find the cluster base path '{DRIVE_CLUSTER_BASE_PATH}'. Aborting."
            )
    else:
        logging.error("CRITICAL: Could not authenticate with Google Drive. Aborting.")


def drive_sync_IMERG(date, cluster):  # Added cluster parameter with default
    """Main function to perform the sync operation for a given date and cluster."""

    # Define the base Google Drive folder path using date and cluster
    # Example: /MO Forecast Benchmarking/operational_data/midway
    DRIVE_CLUSTER_BASE_PATH = f"/MO Forecast Benchmarking/operational_data/{cluster}"

    # Define local paths
    try:
        # Assumes script is like project_root/scripts/sync_script.py
        # Adjust if your structure is different
        script_dir = Path(__file__).resolve().parent
        base = script_dir.parent.parent  # project_root
    except NameError:
        # Fallback for interactive sessions or environments where __file__ isn't set
        logging.warning("Using current working directory's parent as project base.")
        # This assumes you run interactively from the 'scripts' dir
        base = Path.cwd().parent
        if not (base / "IMERG").exists():  # Basic sanity check
            logging.warning(
                "Base path might be incorrect. Expected IMERG folder not found."
            )

    logging.info(f"Using project base path: {base}")
    logging.info(f"Syncing for date: {date}, cluster: {cluster}")
    logging.info(f"Target Drive path: {DRIVE_CLUSTER_BASE_PATH}/{date}")

    IMERG_output_path = base / "IMERG" / "output"
    IMERG_date_dir_local_path = IMERG_output_path / date
    IMERG_DIR_TO_UPLOAD = {
        # Key is the target folder name in Drive under the date folder
        "IMERG": IMERG_date_dir_local_path
    }

    logging.info("Starting Google Drive authentication process...")
    drive_service = authenticate()

    if drive_service:
        logging.info("Google Drive authentication successful.")
        logging.info(f"Starting Google Drive sync process...")
        # 1. Get the ID for the cluster base path (e.g., .../operational_data/midway)
        cluster_base_drive_folder_id = get_folder_id_by_path(
            drive_service, DRIVE_CLUSTER_BASE_PATH
        )

        if cluster_base_drive_folder_id:
            # 2. Create or get the date-specific folder inside the cluster base path
            logging.info(
                f"Ensuring date folder '{date}' exists under '{DRIVE_CLUSTER_BASE_PATH}'..."
            )
            date_folder_id = get_or_create_folder_id(
                drive_service, date, cluster_base_drive_folder_id
            )

            if date_folder_id:
                # 5. Upload IMERG directory contents
                logging.info("Processing IMERG directory...")
                IMERG_target_folder_name = list(IMERG_DIR_TO_UPLOAD.keys())[0]
                IMERG_local_source_dir = IMERG_DIR_TO_UPLOAD[IMERG_target_folder_name]
                logging.info(
                    f"Ensuring Drive folder '{IMERG_target_folder_name}' exists..."
                )
                IMERG_drive_folder_id = get_or_create_folder_id(
                    drive_service, IMERG_target_folder_name, date_folder_id
                )

                if IMERG_drive_folder_id:
                    if IMERG_local_source_dir.is_dir():
                        logging.info(
                            f"Uploading contents of '{IMERG_local_source_dir}' to Drive folder ID: {IMERG_drive_folder_id}"
                        )
                        # Pass the starting local path and the target Drive folder ID
                        upload_directory_recursive(
                            drive_service, IMERG_local_source_dir, IMERG_drive_folder_id
                        )
                    else:
                        logging.error(
                            f"ERROR: Local IMERG directory '{IMERG_local_source_dir}' not found or is not a directory. Skipping IMERG upload."
                        )
                else:
                    logging.error(
                        f"ERROR: Could not create/find folder '{IMERG_target_folder_name}', skipping IMERG upload."
                    )

                logging.info("Sync process finished.")
            else:
                logging.error(
                    f"CRITICAL: Could not create or find the main date folder '{date}' under '{DRIVE_CLUSTER_BASE_PATH}'. Aborting."
                )
        else:
            logging.error(
                f"CRITICAL: Could not create or find the cluster base path '{DRIVE_CLUSTER_BASE_PATH}'. Aborting."
            )
    else:
        logging.error("CRITICAL: Could not authenticate with Google Drive. Aborting.")


def drive_sync_S2S(date, cluster):  # Added cluster parameter with default
    """Main function to perform the sync operation for a given date and cluster."""

    # Define the base Google Drive folder path using date and cluster
    # Example: /MO Forecast Benchmarking/operational_data/midway
    DRIVE_CLUSTER_BASE_PATH = f"/MO Forecast Benchmarking/operational_data/{cluster}/S2S"

    # Define local paths
    try:
        # Assumes script is like project_root/scripts/sync_script.py
        # Adjust if your structure is different
        script_dir = Path(__file__).resolve().parent
        base = script_dir.parent.parent  # project_root
    except NameError:
        # Fallback for interactive sessions or environments where __file__ isn't set
        logging.warning("Using current working directory's parent as project base.")
        # This assumes you run interactively from the 'scripts' dir
        base = Path.cwd().parent
        if not (base / "S2S").exists():  # Basic sanity check
            logging.warning(
                "Base path might be incorrect. Expected S2S folder not found."
            )

    logging.info(f"Using project base path: {base}")
    logging.info(f"Syncing for date: {date}, cluster: {cluster}")
    logging.info(f"Target Drive path: {DRIVE_CLUSTER_BASE_PATH}/{date}")

    output_path = base / "S2S" / "output"
    date_dir_local_path = output_path / date
    DIR_TO_UPLOAD = {
        # Key is the target folder name in Drive under the date folder
        "S2S": date_dir_local_path
    }

    logging.info("Starting Google Drive authentication process...")
    drive_service = authenticate()

    if drive_service:
        logging.info("Google Drive authentication successful.")
        logging.info(f"Starting Google Drive sync process...")
        # 1. Get the ID for the cluster base path (e.g., .../operational_data/midway)
        cluster_base_drive_folder_id = get_folder_id_by_path(
            drive_service, DRIVE_CLUSTER_BASE_PATH
        )

        if cluster_base_drive_folder_id:
            # 2. Create or get the date-specific folder inside the cluster base path
            logging.info(
                f"Ensuring date folder '{date}' exists under '{DRIVE_CLUSTER_BASE_PATH}'..."
            )
            date_folder_id = get_or_create_folder_id(
                drive_service, date, cluster_base_drive_folder_id
            )

            if date_folder_id:
                if date_dir_local_path.is_dir():
                    logging.info(
                        f"Uploading contents of '{date_dir_local_path}' to Drive folder ID: {date_folder_id}"
                    )
                    # Pass the starting local path and the target Drive folder ID
                    upload_directory_recursive(
                        drive_service, date_dir_local_path, date_folder_id
                    )
                else:
                    logging.error(
                        f"ERROR: Local S2S directory '{date_dir_local_path}' not found or is not a directory. Skipping S2S upload."
                    )
            else:
                logging.error(
                    f"CRITICAL: Could not create or find the main date folder '{date}' under '{DRIVE_CLUSTER_BASE_PATH}'. Aborting."
                )
        else:
            logging.error(
                f"CRITICAL: Could not create or find the cluster base path '{DRIVE_CLUSTER_BASE_PATH}'. Aborting."
            )
    else:
        logging.error("CRITICAL: Could not authenticate with Google Drive. Aborting.")

def drive_sync_IMD(date):  # Added cluster parameter with default
    """Main function to perform the sync operation for a given date and cluster."""

    # Define the base Google Drive folder path using date and cluster
    # Example: /MO Forecast Benchmarking/operational_data/midway
    DRIVE_IMD_BASE_PATH = f"/MO Forecast Benchmarking/operational_data/imd-bulletins/IMD_AllIndiaWeatherBulletins"

    # Define local paths
    try:
        # Assumes script is like project_root/scripts/sync_script.py
        # Adjust if your structure is different
        script_dir = Path(__file__).resolve().parent
        base = script_dir.parent.parent  # project_root
    except NameError:
        # Fallback for interactive sessions or environments where __file__ isn't set
        logging.warning("Using current working directory's parent as project base.")
        # This assumes you run interactively from the 'scripts' dir
        base = Path.cwd().parent
        if not (base / "AIFS").exists():  # Basic sanity check
            logging.warning(
                "Base path might be incorrect. Expected AIFS folder not found."
            )

    logging.info(f"Using project base path: {base}")
    logging.info(f"Syncing for date: {date}")
    logging.info(f"Target Drive path: {DRIVE_IMD_BASE_PATH}")

    # --- Define Local File/Directory Sources ---
    IMD_output_path = base / "IMD" / "output"
    IMD_FILE_TO_UPLOAD = IMD_output_path / f"AIWFB_{date}.pdf"
    logging.info("Starting Google Drive authentication process...")
    drive_service = authenticate()

    if drive_service:
        logging.info("Google Drive authentication successful.")
        logging.info("Starting Google Drive sync process...")
        # 1. Get the ID for the cluster base path (e.g., .../operational_data/midway)
        IMD_base_drive_folder_id = get_folder_id_by_path(
            drive_service, DRIVE_IMD_BASE_PATH
        )

        if IMD_base_drive_folder_id:
            # --- Sync Operations ---
            # 3. Upload AIFS files
            logging.info("Processing IMD files...")
            logging.info(f"Uploading IMD file to folder ID: {IMD_base_drive_folder_id}")
            upload_file(drive_service, IMD_FILE_TO_UPLOAD, IMD_base_drive_folder_id)
            logging.info("Sync process finished.")

        else:
            logging.error(
                f"CRITICAL: Could not create or find the cluster base path '{DRIVE_IMD_BASE_PATH}'. Aborting."
            )
    else:
        logging.error("CRITICAL: Could not authenticate with Google Drive. Aborting.")


def _build_engines(args):
    try:
        from .sync_config import load_sync_configs
        from .sync_engine import SyncEngine
        from .sync_inventory import SyncInventory
    except ImportError:  # pragma: no cover - supports python drive.py
        from sync_config import load_sync_configs
        from sync_engine import SyncEngine
        from sync_inventory import SyncInventory

    configs = load_sync_configs(
        args.config,
        sync_root=args.sync_root,
        cluster=args.cluster,
        drive_root=args.drive_root,
        inventory_path=args.inventory,
        regions=args.region,
    )
    drive_client = GoogleDriveClient.authenticated()
    engines = []
    for config in configs:
        inventory = SyncInventory(config.inventory_path)
        engines.append((config, SyncEngine(config, drive_client, inventory), inventory))
    return engines


def main():
    parser = argparse.ArgumentParser(description="Configurable Google Drive sync")
    parser.add_argument(
        "action",
        nargs="?",
        choices=["sync", "reconcile", "ls-drive"],
        default="sync",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--sync-root", type=Path, default=None)
    parser.add_argument("--cluster", type=str, default=None)
    parser.add_argument("--region", type=str, nargs="+", default=None)
    parser.add_argument("--drive-root", type=str, default=None)
    parser.add_argument("--inventory", type=Path, default=None)
    parser.add_argument("--date", type=str, nargs="+", default=None)
    parser.add_argument("--rule", type=str, nargs="+", default=None)
    parser.add_argument(
        "--repair-mode",
        choices=["report", "upload-missing"],
        default="report",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dates = set(args.date) if args.date else None
    rules = set(args.rule) if args.rule else None

    engines = _build_engines(args)
    for config, engine, inventory in engines:
        try:
            logging.info(
                "Running %s for region=%s drive_root=%s",
                args.action,
                config.region,
                config.drive_root,
            )
            if args.action == "sync":
                summary = engine.sync(dates=dates, rule_names=rules, dry_run=args.dry_run)
                logging.info("Sync summary for region=%s: %s", config.region, summary)
            elif args.action == "reconcile":
                summary = engine.reconcile(
                    dates=dates,
                    rule_names=rules,
                    repair_mode=args.repair_mode,
                )
                logging.info("Reconcile summary for region=%s: %s", config.region, summary)
            else:
                for item in engine.list_drive(date=args.date[0] if args.date else None):
                    logging.info("%s", item.get("path"))
        finally:
            inventory.close()


if __name__ == "__main__":
    main()
