import importlib.util
import sys
import tempfile
import types
import unittest
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


if __name__ == "__main__":
    unittest.main()
