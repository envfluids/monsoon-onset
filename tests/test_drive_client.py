import importlib.util
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "sync/utils/drive.py"


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeFilesResource:
    def __init__(self, existing_files=None):
        self.existing_files = existing_files or []
        self.created = []

    def list(self, **_kwargs):
        return FakeRequest({"files": list(self.existing_files)})

    def create(self, **kwargs):
        self.created.append(kwargs)
        return FakeRequest(
            {
                "id": "created-id",
                "name": kwargs["body"]["name"],
                "size": "1",
                "modifiedTime": "2026-06-08T00:00:00Z",
                "md5Checksum": "created-md5",
            }
        )


class FakeDriveService:
    def __init__(self, existing_files=None):
        self.files_resource = FakeFilesResource(existing_files)

    def files(self):
        return self.files_resource


class FakeMediaFileUpload:
    def __init__(self, filename, mimetype=None, resumable=False):
        self.filename = filename
        self.mimetype = mimetype
        self.resumable = resumable


class FakeMediaInMemoryUpload:
    def __init__(self, body, mimetype=None, resumable=False):
        self.body = body
        self.mimetype = mimetype
        self.resumable = resumable


def install_import_stubs():
    google_module = sys.modules.setdefault("google", types.ModuleType("google"))
    auth_module = sys.modules.setdefault("google.auth", types.ModuleType("google.auth"))
    transport_module = sys.modules.setdefault(
        "google.auth.transport", types.ModuleType("google.auth.transport")
    )
    requests_module = sys.modules.setdefault(
        "google.auth.transport.requests", types.ModuleType("google.auth.transport.requests")
    )
    oauth2_module = sys.modules.setdefault("google.oauth2", types.ModuleType("google.oauth2"))
    credentials_module = sys.modules.setdefault(
        "google.oauth2.credentials", types.ModuleType("google.oauth2.credentials")
    )
    oauthlib_module = sys.modules.setdefault(
        "google_auth_oauthlib", types.ModuleType("google_auth_oauthlib")
    )
    flow_module = sys.modules.setdefault(
        "google_auth_oauthlib.flow", types.ModuleType("google_auth_oauthlib.flow")
    )
    discovery_module = sys.modules.setdefault(
        "googleapiclient.discovery", types.ModuleType("googleapiclient.discovery")
    )
    errors_module = sys.modules.setdefault(
        "googleapiclient.errors", types.ModuleType("googleapiclient.errors")
    )
    http_module = sys.modules.setdefault(
        "googleapiclient.http", types.ModuleType("googleapiclient.http")
    )

    requests_module.Request = object
    credentials_module.Credentials = object
    flow_module.InstalledAppFlow = object
    discovery_module.build = lambda *args, **kwargs: None
    errors_module.HttpError = Exception
    http_module.MediaFileUpload = FakeMediaFileUpload
    http_module.MediaInMemoryUpload = FakeMediaInMemoryUpload

    google_module.auth = auth_module
    auth_module.transport = transport_module
    transport_module.requests = requests_module
    google_module.oauth2 = oauth2_module
    oauth2_module.credentials = credentials_module
    oauthlib_module.flow = flow_module


def load_drive_module():
    install_import_stubs()
    module_name = "drive_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class GoogleDriveClientTest(unittest.TestCase):
    def setUp(self):
        self.module = load_drive_module()

    def test_upload_file_skips_existing_drive_file(self):
        existing = {
            "id": "existing-id",
            "name": "forecast.nc",
            "size": "10",
            "modifiedTime": "2026-06-08T00:00:00Z",
            "md5Checksum": "existing-md5",
        }
        service = FakeDriveService(existing_files=[existing])
        client = self.module.GoogleDriveClient(service)
        client._folder_id_cache["target"] = "folder-id"

        with tempfile.TemporaryDirectory() as tmp:
            local_file = Path(tmp) / "forecast.nc"
            local_file.write_text("x", encoding="utf-8")
            result = client.upload_file(local_file, "target")

        self.assertEqual(result, existing)
        self.assertEqual(service.files_resource.created, [])

    def test_upload_file_creates_missing_drive_file(self):
        service = FakeDriveService(existing_files=[])
        client = self.module.GoogleDriveClient(service)
        client._folder_id_cache["target"] = "folder-id"

        with tempfile.TemporaryDirectory() as tmp:
            local_file = Path(tmp) / "forecast.nc"
            local_file.write_text("x", encoding="utf-8")
            result = client.upload_file(local_file, "target")

        self.assertEqual(result["id"], "created-id")
        self.assertEqual(len(service.files_resource.created), 1)



class RecordingFiles:
    """files() resource that records list queries and answers get/delete/update."""

    def __init__(self, list_results=None, get_result=None, delete_error=None):
        self.list_calls = []
        self.list_results = list(list_results or [])
        self.get_result = get_result or {}
        self.delete_error = delete_error
        self.updated = []
        self.deleted = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return FakeRequest(
            {"files": self.list_results.pop(0) if self.list_results else []}
        )

    def get(self, **kwargs):
        return FakeRequest(self.get_result)

    def create(self, **kwargs):
        return FakeRequest({"id": "new-folder", "name": kwargs["body"]["name"]})

    def delete(self, **kwargs):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(kwargs["fileId"])
        return FakeRequest({})

    def update(self, **kwargs):
        self.updated.append(kwargs)
        return FakeRequest({})


class RecordingService:
    def __init__(self, files_resource):
        self.files_resource = files_resource

    def files(self):
        return self.files_resource


class DriveFolderIdAndHeadlessTest(unittest.TestCase):
    def setUp(self):
        self.module = load_drive_module()

    def test_paths_resolve_under_the_root_folder_id(self):
        files = RecordingFiles(list_results=[[{"id": "dwc"}], [{"id": "day"}]])
        client = self.module.GoogleDriveClient(RecordingService(files), "FOLDER")
        self.assertEqual(client._resolve_folder_path("//drywetcast/20261013T00"), "day")
        self.assertIn("'FOLDER' in parents", files.list_calls[0]["q"])
        self.assertIn("'dwc' in parents", files.list_calls[1]["q"])
        self.assertEqual(client._resolve_folder_path("/"), "FOLDER")

    def test_default_root_is_my_drive(self):
        client = self.module.GoogleDriveClient(RecordingService(RecordingFiles()))
        self.assertEqual(client.root_folder_id, "root")

    def test_authenticated_reads_the_folder_id_and_workers_keep_it(self):
        service = RecordingService(RecordingFiles())
        self.module.authenticate = lambda: service
        os_env = {"MONSOON_DRIVE_FOLDER_ID": "FOLDER"}
        with unittest.mock.patch.dict(self.module.os.environ, os_env):
            client = self.module.GoogleDriveClient.authenticated()
        self.assertEqual(client.root_folder_id, "FOLDER")
        self.assertEqual(client.new_worker_client().root_folder_id, "FOLDER")

    def test_get_or_create_folder_searches_shared_drives(self):
        files = RecordingFiles(list_results=[[{"id": "existing"}]])
        folder = self.module.get_or_create_folder_id(
            RecordingService(files), "drywetcast", "parent"
        )
        self.assertEqual(folder, "existing")
        self.assertTrue(files.list_calls[0]["supportsAllDrives"])
        self.assertTrue(files.list_calls[0]["includeItemsFromAllDrives"])

    def test_folder_info_requires_a_writable_folder(self):
        folder = self.module.FOLDER_MIME_TYPE
        cases = {
            "not a folder": {"mimeType": "text/plain", "capabilities": {"canAddChildren": True}},
            "read only": {"mimeType": folder, "capabilities": {"canAddChildren": False}},
        }
        for label, info in cases.items():
            with self.subTest(label):
                client = self.module.GoogleDriveClient(
                    RecordingService(RecordingFiles(get_result=info)), "FOLDER"
                )
                with self.assertRaises(RuntimeError):
                    client.folder_info()
        ok = {"id": "FOLDER", "mimeType": folder, "driveId": "SD", "capabilities": {"canAddChildren": True}}
        client = self.module.GoogleDriveClient(
            RecordingService(RecordingFiles(get_result=ok)), "FOLDER"
        )
        self.assertEqual(client.folder_info()["driveId"], "SD")

    def test_remove_file_falls_back_to_trash(self):
        files = RecordingFiles()
        client = self.module.GoogleDriveClient(RecordingService(files), "FOLDER")
        self.assertEqual(client.remove_file("f1"), "deleted")
        files.delete_error = self.module.HttpError("403 insufficientFilePermissions")
        self.assertEqual(client.remove_file("f2"), "trashed")
        self.assertEqual(files.updated[0]["body"], {"trashed": True})

    def test_headless_auth_fails_instead_of_starting_a_browser_login(self):
        class ExpiredCredentials:
            valid = False
            expired = True
            refresh_token = "refresh"

            @classmethod
            def from_authorized_user_file(cls, *args):
                return cls()

            def refresh(self, request):
                raise RuntimeError("invalid_grant")

        class NoBrowser:
            @staticmethod
            def from_client_secrets_file(*args):
                raise AssertionError("browser login must not start when headless")

        with tempfile.TemporaryDirectory() as tmp:
            credentials = Path(tmp) / "credentials.json"
            token = Path(tmp) / "token.json"
            credentials.write_text("{}")
            token.write_text("{}")
            with (
                unittest.mock.patch.object(self.module, "CREDENTIALS_FILE", credentials),
                unittest.mock.patch.object(self.module, "TOKEN_FILE", token),
                unittest.mock.patch.object(self.module, "Credentials", ExpiredCredentials),
                unittest.mock.patch.object(self.module, "InstalledAppFlow", NoBrowser),
                unittest.mock.patch.object(self.module, "Request", object),
                unittest.mock.patch.dict(self.module.os.environ, {"MONSOON_DRIVE_HEADLESS": "1"}),
            ):
                self.assertIsNone(self.module.authenticate())
                with self.assertRaises(RuntimeError):
                    self.module.GoogleDriveClient.authenticated()


if __name__ == "__main__":
    unittest.main()
