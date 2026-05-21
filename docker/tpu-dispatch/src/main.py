"""Cloud Run controller for TPU queued-resource workloads."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from typing import Any

import google.auth
from google.api_core import exceptions as google_exceptions
from google.api_core import retry
from google.cloud import storage, tpu_v2alpha1

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

TPU_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
TERMINAL_QR_STATES = {"FAILED", "SUSPENDED", "PREEMPTED", "DELETING"}


class DispatchError(RuntimeError):
    """Base controller error."""


class ControllerInterrupted(DispatchError):
    """Raised when Cloud Run asks the controller to stop."""


class TpuNotFound(DispatchError):
    """Queued resource was not found."""


@dataclass(frozen=True)
class DispatchConfig:
    project_id: str
    workload_name: str
    run_id: str
    date: str
    workload_image: str
    forecast_regions: str
    common_bucket: str
    region_buckets: str
    tpu_zone: str
    tpu_accelerator_type: str = "v5p-64"
    tpu_runtime_version: str = "v2-alpha-tpuv5"
    tpu_spot: bool = True
    tpu_network: str = ""
    tpu_subnetwork: str = ""
    tpu_service_account: str = ""
    artifact_registry_host: str = ""
    max_attempts: int = 1
    poll_interval_seconds: int = 60
    queue_timeout_seconds: int = 14_400
    run_timeout_seconds: int = 72_000
    request_valid_duration: str = "14400s"
    retry_on_workload_failure: bool = False
    success_grace_seconds: int = 30
    expected_global_devices: str = "32"
    expected_local_devices: str = "4"
    expected_process_count: str = "8"
    ensemble_members: str = "32"

    @property
    def status_prefix(self) -> str:
        return f"intermediate/tpu-dispatch/{self.workload_name}/{self.date}/{self.run_id}"

    @property
    def status_path(self) -> str:
        return f"{self.status_prefix}/status.json"

    def attempt_status_path(self, attempt: int) -> str:
        return f"{self.status_prefix}/attempts/{attempt}/status.json"

    def attempt_node_status_path(self, attempt: int) -> str:
        return f"{self.status_prefix}/attempts/{attempt}/nodes/__HOSTNAME__.json"

    def attempt_node_workload_log_path(self, attempt: int) -> str:
        return f"{self.status_prefix}/attempts/{attempt}/logs/__HOSTNAME__.log"

    def attempt_node_startup_log_path(self, attempt: int) -> str:
        return f"{self.status_prefix}/attempts/{attempt}/logs/__HOSTNAME__-startup.log"

    @classmethod
    def from_env(cls) -> "DispatchConfig":
        project_id = os.getenv("PROJECT_ID") or default_project_id()
        workload_name = required_env("WORKLOAD_NAME")
        date = required_env("DATE")
        workload_image = required_env("WORKLOAD_IMAGE")
        run_id = os.getenv("RUN_ID") or f"{workload_name}-{date.replace('T', '-')}"
        return cls(
            project_id=project_id,
            workload_name=workload_name,
            run_id=sanitize_resource_id(run_id),
            date=date,
            workload_image=workload_image,
            forecast_regions=required_env("FORECAST_REGIONS"),
            common_bucket=required_env("GCS_COMMON_BUCKET"),
            region_buckets=required_env("GCS_REGION_BUCKETS"),
            tpu_zone=os.getenv("TPU_ZONE", "us-central1-a"),
            tpu_accelerator_type=os.getenv("TPU_ACCELERATOR_TYPE", "v5p-64"),
            tpu_runtime_version=os.getenv("TPU_RUNTIME_VERSION", "v2-alpha-tpuv5"),
            tpu_spot=env_bool("TPU_SPOT", True),
            tpu_network=os.getenv("TPU_NETWORK", ""),
            tpu_subnetwork=os.getenv("TPU_SUBNETWORK", ""),
            tpu_service_account=os.getenv("TPU_SERVICE_ACCOUNT", ""),
            artifact_registry_host=os.getenv("ARTIFACT_REGISTRY_HOST") or workload_image.split("/")[0],
            max_attempts=env_int("MAX_ATTEMPTS", 1),
            poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", 60),
            queue_timeout_seconds=env_int("QUEUE_TIMEOUT_SECONDS", 14_400),
            run_timeout_seconds=env_int("RUN_TIMEOUT_SECONDS", 72_000),
            request_valid_duration=os.getenv("REQUEST_VALID_DURATION", "14400s"),
            retry_on_workload_failure=env_bool("RETRY_ON_WORKLOAD_FAILURE", False),
            success_grace_seconds=env_int("SUCCESS_GRACE_SECONDS", 30),
            expected_global_devices=os.getenv("GENCAST_EXPECTED_GLOBAL_DEVICES", "32"),
            expected_local_devices=os.getenv("GENCAST_EXPECTED_LOCAL_DEVICES", "4"),
            expected_process_count=os.getenv("GENCAST_EXPECTED_PROCESS_COUNT", "8"),
            ensemble_members=os.getenv("GENCAST_ENSEMBLE_MEMBERS", "32"),
        )

    @property
    def forecast_region_names(self) -> list[str]:
        try:
            parsed = json.loads(self.forecast_regions)
        except json.JSONDecodeError as exc:
            raise DispatchError("FORECAST_REGIONS must be a JSON list") from exc
        if not isinstance(parsed, list) or not all(isinstance(region, str) for region in parsed):
            raise DispatchError("FORECAST_REGIONS must be a JSON list of strings")
        return parsed


@dataclass(frozen=True)
class AttemptResult:
    success: bool
    retryable: bool
    message: str


class StatusStore:
    def __init__(self) -> None:
        self._client = storage.Client()

    def read(self, bucket_name: str, path: str) -> dict[str, Any] | None:
        blob = self._client.bucket(bucket_name).blob(path)
        if not blob.exists():
            return None
        text = blob.download_as_text()
        if not text.strip():
            logger.warning("Ignoring empty GCS status marker: gs://%s/%s", bucket_name, path)
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(
                "Ignoring invalid GCS status marker: gs://%s/%s content_prefix=%r",
                bucket_name,
                path,
                text[:200],
            )
            return None

    def write(self, bucket_name: str, path: str, payload: dict[str, Any]) -> None:
        self._client.bucket(bucket_name).blob(path).upload_from_string(
            json.dumps(payload, sort_keys=True),
            content_type="application/json",
        )

    def exists(self, bucket_name: str, path: str) -> bool:
        return self._client.bucket(bucket_name).blob(path).exists()

    def list_json(self, bucket_name: str, prefix: str) -> dict[str, dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        for blob in self._client.list_blobs(bucket_name, prefix=prefix):
            text = blob.download_as_text()
            if not text.strip():
                logger.warning("Ignoring empty GCS status marker: gs://%s/%s", bucket_name, blob.name)
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                logger.warning(
                    "Ignoring invalid GCS status marker: gs://%s/%s content_prefix=%r",
                    bucket_name,
                    blob.name,
                    text[:200],
                )
                continue
            if isinstance(parsed, dict):
                values[blob.name] = parsed
        return values


class TpuClient:
    def __init__(self, config: DispatchConfig) -> None:
        self.config = config
        credentials, auth_project_id = google.auth.default(scopes=[TPU_SCOPE])
        logger.info(
            "Google auth context: %s",
            json.dumps(
                {
                    "auth_project_id": auth_project_id or "",
                    "credential_class": credentials.__class__.__name__,
                    "quota_project_id": getattr(credentials, "quota_project_id", "") or "",
                    "service_account_email": getattr(credentials, "service_account_email", "") or "",
                },
                sort_keys=True,
            ),
        )
        self._client = tpu_v2alpha1.TpuClient(credentials=credentials)
        self._parent = f"projects/{config.project_id}/locations/{config.tpu_zone}"

    def create(self, queued_resource_id: str, node_id: str, metadata: dict[str, str]) -> None:
        request = self._create_request(queued_resource_id, node_id, metadata)
        logger.info(
            "Creating TPU queued resource: %s",
            json.dumps(self._request_summary(queued_resource_id, node_id), sort_keys=True),
        )
        try:
            operation = self._client.create_queued_resource(request=request)
        except google_exceptions.AlreadyExists:
            logger.info("Queued resource %s already exists; monitoring it", queued_resource_id)
            return
        logger.info("Queued resource create operation started: %s", operation_name(operation))
        operation.result()
        logger.info("Queued resource create operation completed: %s", queued_resource_id)

    @retry.Retry(
        predicate=retry.if_transient_error,
        initial=1.0,
        maximum=10.0,
        timeout=60.0,
    )
    def state(self, queued_resource_id: str) -> str:
        try:
            resource = self._client.get_queued_resource(
                name=f"{self._parent}/queuedResources/{queued_resource_id}"
            )
        except google_exceptions.NotFound as exc:
            raise TpuNotFound(queued_resource_id) from exc
        return enum_name(resource.state.state)

    def delete(self, queued_resource_id: str, wait: bool = True) -> None:
        request = tpu_v2alpha1.DeleteQueuedResourceRequest(
            name=f"{self._parent}/queuedResources/{queued_resource_id}",
            force=True,
        )
        try:
            operation = self._client.delete_queued_resource(request=request)
        except google_exceptions.NotFound:
            logger.info("Queued resource %s already deleted", queued_resource_id)
            return
        logger.info("Queued resource delete operation started: %s", operation_name(operation))
        if wait:
            logger.info(
                "Not waiting for queued resource delete operation; TPU delete operations "
                "return google.protobuf.Empty in this API version."
            )

    def _create_request(
        self,
        queued_resource_id: str,
        node_id: str,
        metadata: dict[str, str],
    ) -> tpu_v2alpha1.CreateQueuedResourceRequest:
        node = tpu_v2alpha1.Node(
            accelerator_type=self.config.tpu_accelerator_type,
            runtime_version=self.config.tpu_runtime_version,
            metadata=metadata,
        )
        if self.config.tpu_network or self.config.tpu_subnetwork:
            node.network_config = tpu_v2alpha1.NetworkConfig(
                network=self.config.tpu_network,
                subnetwork=self.config.tpu_subnetwork,
                enable_external_ips=False,
            )
        if self.config.tpu_service_account:
            node.service_account = tpu_v2alpha1.ServiceAccount(
                email=self.config.tpu_service_account,
                scope=[TPU_SCOPE],
            )

        queued_resource = tpu_v2alpha1.QueuedResource(
            tpu=tpu_v2alpha1.QueuedResource.Tpu(
                node_spec=[
                    tpu_v2alpha1.QueuedResource.Tpu.NodeSpec(
                        parent=self._parent,
                        node_id=node_id,
                        node=node,
                    )
                ]
            )
        )
        if self.config.tpu_spot:
            queued_resource.spot = tpu_v2alpha1.QueuedResource.Spot()
        elif self.config.request_valid_duration:
            queued_resource.queueing_policy = tpu_v2alpha1.QueuedResource.QueueingPolicy(
                valid_until_duration=self.config.request_valid_duration,
            )

        return tpu_v2alpha1.CreateQueuedResourceRequest(
            parent=self._parent,
            queued_resource_id=queued_resource_id,
            queued_resource=queued_resource,
        )

    def _request_summary(self, queued_resource_id: str, node_id: str) -> dict[str, Any]:
        return {
            "parent": self._parent,
            "queued_resource_id": queued_resource_id,
            "node_id": node_id,
            "accelerator_type": self.config.tpu_accelerator_type,
            "runtime_version": self.config.tpu_runtime_version,
            "spot": self.config.tpu_spot,
            "queueing_policy": {}
            if self.config.tpu_spot
            else {"valid_until_duration": self.config.request_valid_duration},
            "network": self.config.tpu_network,
            "subnetwork": self.config.tpu_subnetwork,
            "service_account": self.config.tpu_service_account,
        }


class Controller:
    def __init__(
        self,
        config: DispatchConfig,
        tpu_client: TpuClient,
        status_store: StatusStore,
        sleep=time.sleep,
        now=lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.config = config
        self.tpu = tpu_client
        self.status = status_store
        self.sleep = sleep
        self.now = now

    def run(self) -> None:
        log_dispatch_context(self.config)
        self.write_status(0, "QUEUED", "Starting TPU dispatch")
        last_result = AttemptResult(False, True, "No attempts ran")
        last_attempt = 0
        for attempt in range(1, self.config.max_attempts + 1):
            last_attempt = attempt
            last_result = self.run_attempt(attempt)
            if last_result.success:
                self.write_status(attempt, "SUCCEEDED", last_result.message)
                return
            if not last_result.retryable:
                break
            logger.warning("Attempt %s will be retried: %s", attempt, last_result.message)
        self.write_status(last_attempt, "FAILED", last_result.message)
        raise DispatchError(last_result.message)

    def run_attempt(self, attempt: int) -> AttemptResult:
        queued_resource_id = self.queued_resource_id(attempt)
        node_id = queued_resource_id
        interrupted = False
        succeeded = False
        try:
            self.write_status(attempt, "QUEUED", "Creating TPU queued resource", queued_resource_id, node_id)
            self.tpu.create(queued_resource_id, node_id, self.startup_metadata(attempt, queued_resource_id, node_id))
            self.write_status(attempt, "WAITING_FOR_TPU", "Queued resource accepted", queued_resource_id, node_id)
            result = self.wait_for_attempt(attempt, queued_resource_id, node_id)
            succeeded = result.success
            return result
        except ControllerInterrupted:
            interrupted = True
            self.write_status(attempt, "FAILED", "Controller interrupted", queued_resource_id, node_id)
            raise
        except Exception as exc:  # noqa: BLE001 - controller must clean up.
            logger.exception("Attempt %s failed before terminal status", attempt)
            return AttemptResult(False, True, f"TPU attempt error: {exc}")
        finally:
            try:
                if succeeded and self.config.success_grace_seconds > 0:
                    logger.info(
                        "TPU workload succeeded; waiting %s seconds before cleanup",
                        self.config.success_grace_seconds,
                    )
                    self.sleep(self.config.success_grace_seconds)
                self.write_status(attempt, "CLEANING_UP", "Deleting TPU queued resource", queued_resource_id, node_id)
                self.tpu.delete(queued_resource_id, wait=not interrupted)
            except Exception:
                logger.exception("Failed to delete queued resource %s", queued_resource_id)

    def wait_for_attempt(self, attempt: int, queued_resource_id: str, node_id: str) -> AttemptResult:
        attempt_started_at = self.now()
        queue_deadline = attempt_started_at + dt.timedelta(seconds=self.config.queue_timeout_seconds)
        run_deadline: dt.datetime | None = None

        while True:
            if self.completion_markers_exist():
                return AttemptResult(True, False, "TPU workload succeeded")

            failed_node = self.node_failure_status(attempt, queued_resource_id, attempt_started_at)
            if failed_node:
                exit_code = failed_node.get("exit_code")
                message = f"TPU workload failed with exit_code={exit_code}"
                if failed_node.get("workload_log_uri"):
                    message = f"{message}; workload_log={failed_node['workload_log_uri']}"
                return AttemptResult(
                    False,
                    self.config.retry_on_workload_failure,
                    message,
                )

            if (
                self.node_running_status(attempt, queued_resource_id, attempt_started_at)
                and run_deadline is None
            ):
                run_deadline = self.now() + dt.timedelta(seconds=self.config.run_timeout_seconds)
                self.write_status(attempt, "RUNNING", "TPU workload reported RUNNING", queued_resource_id, node_id)

            try:
                qr_state = self.tpu.state(queued_resource_id)
            except TpuNotFound:
                qr_state = "NOT_FOUND"

            if qr_state in TERMINAL_QR_STATES or qr_state == "NOT_FOUND":
                return AttemptResult(False, True, f"Queued resource ended before workload success: {qr_state}")
            if run_deadline is None and self.now() > queue_deadline:
                return AttemptResult(False, True, "Timed out waiting for TPU allocation")
            if run_deadline is not None and self.now() > run_deadline:
                return AttemptResult(False, True, "Timed out waiting for TPU workload")
            self.sleep(self.config.poll_interval_seconds)

    def completion_markers_exist(self) -> bool:
        return all(
            self.status.exists(
                self.config.common_bucket,
                f"intermediate/{self.config.workload_name}_{region}_{self.config.date}_done",
            )
            for region in self.config.forecast_region_names
        )

    def node_failure_status(
        self,
        attempt: int,
        queued_resource_id: str = "",
        attempt_started_at: dt.datetime | None = None,
    ) -> dict[str, Any] | None:
        for status in self.node_statuses(attempt, queued_resource_id, attempt_started_at).values():
            if normalize_state(status.get("state")) == "FAILED":
                return status
        return None

    def node_running_status(
        self,
        attempt: int,
        queued_resource_id: str = "",
        attempt_started_at: dt.datetime | None = None,
    ) -> bool:
        return any(
            normalize_state(status.get("state")) == "RUNNING"
            for status in self.node_statuses(
                attempt, queued_resource_id, attempt_started_at
            ).values()
        )

    def node_statuses(
        self,
        attempt: int,
        queued_resource_id: str = "",
        attempt_started_at: dt.datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        prefix = f"{self.config.status_prefix}/attempts/{attempt}/nodes/"
        statuses = self.status.list_json(self.config.common_bucket, prefix)
        return {
            path: status
            for path, status in statuses.items()
            if self.is_current_node_status(status, attempt, queued_resource_id, attempt_started_at)
        }

    def is_current_node_status(
        self,
        status: dict[str, Any],
        attempt: int,
        queued_resource_id: str,
        attempt_started_at: dt.datetime | None,
    ) -> bool:
        if status.get("run_id") and status["run_id"] != self.config.run_id:
            return False
        if status.get("date") and status["date"] != self.config.date:
            return False
        if status.get("attempt"):
            try:
                status_attempt = int(status["attempt"])
            except (TypeError, ValueError):
                return False
            if status_attempt != attempt:
                return False
        if attempt_started_at is not None:
            status_time = parse_timestamp(status.get("updated_at")) or parse_timestamp(status.get("started_at"))
            if status_time is not None and status_time < attempt_started_at:
                return False
        return True

    def startup_metadata(
        self,
        attempt: int,
        queued_resource_id: str,
        node_id: str,
    ) -> dict[str, str]:
        return {
            "startup-script": build_startup_script(),
            "workload-name": self.config.workload_name,
            "run-id": self.config.run_id,
            "attempt": str(attempt),
            "date": self.config.date,
            "forecast-regions": self.config.forecast_regions,
            "common-bucket": self.config.common_bucket,
            "region-buckets": self.config.region_buckets,
            "workload-image": self.config.workload_image,
            "artifact-registry-host": self.config.artifact_registry_host,
            "node-status-path": self.config.attempt_node_status_path(attempt),
            "node-workload-log-path": self.config.attempt_node_workload_log_path(
                attempt
            ),
            "node-startup-log-path": self.config.attempt_node_startup_log_path(attempt),
            "dispatch-queued-resource-id": queued_resource_id,
            "node-id": node_id,
            "zone": self.config.tpu_zone,
            "expected-global-devices": self.config.expected_global_devices,
            "expected-local-devices": self.config.expected_local_devices,
            "expected-process-count": self.config.expected_process_count,
            "ensemble-members": self.config.ensemble_members,
        }

    def queued_resource_id(self, attempt: int) -> str:
        return sanitize_resource_id(f"{self.config.workload_name}-{self.config.date.replace('T', '-')}-a{attempt}")

    def write_status(
        self,
        attempt: int,
        state: str,
        message: str,
        queued_resource_id: str = "",
        node_id: str = "",
        exit_code: int | None = None,
    ) -> None:
        timestamp = self.now().isoformat()
        payload = {
            "run_id": self.config.run_id,
            "attempt": attempt,
            "workload": self.config.workload_name,
            "date": self.config.date,
            "state": state,
            "queued_resource_id": queued_resource_id,
            "node_id": node_id,
            "zone": self.config.tpu_zone,
            "started_at": timestamp,
            "updated_at": timestamp,
            "message": message,
            "exit_code": exit_code,
        }
        self.status.write(self.config.common_bucket, self.config.status_path, payload)
        if attempt > 0:
            self.status.write(self.config.common_bucket, self.config.attempt_status_path(attempt), payload)


def build_startup_script() -> str:
    return r'''#!/bin/bash
set -uo pipefail

LOG_FILE=/var/log/monsoon-tpu-dispatch-startup.log
WORKLOAD_LOG_FILE=/var/log/monsoon-tpu-workload.log

metadata() {
  curl -fsH "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"
}

metadata_instance() {
  curl -fsH "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/$1"
}

metadata_project() {
  curl -fsH "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/project/$1"
}

WORKLOAD_NAME=$(metadata workload-name)
RUN_ID=$(metadata run-id)
ATTEMPT=$(metadata attempt)
DATE=$(metadata date)
FORECAST_REGIONS=$(metadata forecast-regions)
COMMON_BUCKET=$(metadata common-bucket)
REGION_BUCKETS=$(metadata region-buckets)
WORKLOAD_IMAGE=$(metadata workload-image)
ARTIFACT_REGISTRY_HOST=$(metadata artifact-registry-host)
NODE_STATUS_PATH=$(metadata node-status-path)
NODE_STATUS_PATH="${NODE_STATUS_PATH/__HOSTNAME__/$(hostname)}"
NODE_WORKLOAD_LOG_PATH=$(metadata node-workload-log-path)
NODE_WORKLOAD_LOG_PATH="${NODE_WORKLOAD_LOG_PATH/__HOSTNAME__/$(hostname)}"
NODE_STARTUP_LOG_PATH=$(metadata node-startup-log-path)
NODE_STARTUP_LOG_PATH="${NODE_STARTUP_LOG_PATH/__HOSTNAME__/$(hostname)}"
QUEUED_RESOURCE_ID=$(metadata dispatch-queued-resource-id)
NODE_ID=$(metadata node-id)
ZONE=$(metadata zone)
EXPECTED_GLOBAL_DEVICES=$(metadata expected-global-devices)
EXPECTED_LOCAL_DEVICES=$(metadata expected-local-devices)
EXPECTED_PROCESS_COUNT=$(metadata expected-process-count)
ENSEMBLE_MEMBERS=$(metadata ensemble-members)
PROJECT_ID=$(metadata_project project-id)
INSTANCE_ID=$(metadata_instance id)
TPU_WORKER_HOSTNAME=$(hostname)

LOG_FORWARDER=/tmp/monsoon-cloud-logging-forwarder.py
cat > "$LOG_FORWARDER" <<'PY'
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

STREAM = sys.argv[1]
LOCAL_LOG_FILE = sys.argv[2]
PROJECT_ID = os.environ.get("PROJECT_ID", "")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "")
ZONE = os.environ.get("ZONE", "")
TOKEN = ""
TOKEN_EXPIRES_AT = 0.0
DISABLED_UNTIL = 0.0

COMMON_LABELS = {
    "workload": os.environ.get("WORKLOAD_NAME", ""),
    "run_id": os.environ.get("RUN_ID", ""),
    "attempt": os.environ.get("ATTEMPT", ""),
    "date": os.environ.get("DATE", ""),
    "queued_resource_id": os.environ.get("QUEUED_RESOURCE_ID", ""),
    "node_id": os.environ.get("NODE_ID", ""),
    "worker_hostname": os.environ.get("TPU_WORKER_HOSTNAME", ""),
    "stream": STREAM,
}
COMMON_LABELS = {key: value for key, value in COMMON_LABELS.items() if value}


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def severity_for(message):
    upper = message.upper()
    if "CRITICAL" in upper or "FATAL" in upper:
        return "CRITICAL"
    if "ERROR" in upper or "FAILED" in upper or "TRACEBACK" in upper:
        return "ERROR"
    if "WARNING" in upper or "WARN" in upper:
        return "WARNING"
    return "INFO"


def access_token():
    global TOKEN, TOKEN_EXPIRES_AT
    now = time.time()
    if TOKEN and now < TOKEN_EXPIRES_AT - 60:
        return TOKEN
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
    TOKEN = payload["access_token"]
    TOKEN_EXPIRES_AT = now + int(payload.get("expires_in", 300))
    return TOKEN


def write_entries(entries, local_log):
    global DISABLED_UNTIL
    if not PROJECT_ID or not INSTANCE_ID or not entries:
        return
    now = time.time()
    if now < DISABLED_UNTIL:
        return

    body = {
        "logName": f"projects/{PROJECT_ID}/logs/monsoon-tpu-vm",
        "resource": {
            "type": "gce_instance",
            "labels": {
                "project_id": PROJECT_ID,
                "instance_id": INSTANCE_ID,
                "zone": ZONE,
            },
        },
        "labels": COMMON_LABELS,
        "entries": entries,
    }
    try:
        request = urllib.request.Request(
            "https://logging.googleapis.com/v2/entries:write",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3):
            return
    except Exception as exc:
        DISABLED_UNTIL = time.time() + 60
        local_log.write(
            f"{timestamp()} cloud_logging_forwarder stream={STREAM} disabled_for=60s error={exc}\n"
        )


def main():
    batch = []
    last_flush = time.monotonic()
    with open(LOCAL_LOG_FILE, "a", buffering=1) as local_log:
        for line in sys.stdin:
            local_log.write(line)
            message = line.rstrip("\n")
            if message:
                batch.append(
                    {
                        "timestamp": timestamp(),
                        "severity": severity_for(message),
                        "jsonPayload": {
                            "message": message,
                            "stream": STREAM,
                            "worker_hostname": COMMON_LABELS.get("worker_hostname", ""),
                            "node_id": COMMON_LABELS.get("node_id", ""),
                            "queued_resource_id": COMMON_LABELS.get("queued_resource_id", ""),
                            "attempt": COMMON_LABELS.get("attempt", ""),
                        },
                    }
                )
            if len(batch) >= 50 or time.monotonic() - last_flush >= 5:
                write_entries(batch, local_log)
                batch = []
                last_flush = time.monotonic()
        write_entries(batch, local_log)


if __name__ == "__main__":
    main()
PY

forward_log_stream() {
  local stream="$1"
  local local_log_file="$2"
  python3 "$LOG_FORWARDER" "$stream" "$local_log_file" 2>> "$local_log_file"
}

export PROJECT_ID INSTANCE_ID TPU_WORKER_HOSTNAME
export WORKLOAD_NAME RUN_ID ATTEMPT DATE QUEUED_RESOURCE_ID NODE_ID ZONE
exec > >(forward_log_stream startup-stdout "$LOG_FILE") 2> >(forward_log_stream startup-stderr "$LOG_FILE")

write_status() {
  local state="$1"
  local exit_code="$2"
  local message="$3"
  local target_path="$4"
  python3 - "$state" "$exit_code" "$message" <<'PY' | gcloud storage cp - "gs://${COMMON_BUCKET}/${target_path}" --content-type=application/json
import datetime as dt
import json
import os
import sys

state, exit_code, message = sys.argv[1:4]
payload = {
    "run_id": os.environ["RUN_ID"],
    "attempt": int(os.environ["ATTEMPT"]),
    "workload": os.environ["WORKLOAD_NAME"],
    "date": os.environ["DATE"],
    "state": state,
    "queued_resource_id": os.environ["QUEUED_RESOURCE_ID"],
    "node_id": os.environ["NODE_ID"],
    "zone": os.environ["ZONE"],
    "started_at": os.environ.get("STARTED_AT", ""),
    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "message": message,
    "exit_code": None if exit_code == "" else int(exit_code),
    "workload_log_uri": os.environ["NODE_WORKLOAD_LOG_URI"],
    "startup_log_uri": os.environ["NODE_STARTUP_LOG_URI"],
}
print(json.dumps(payload, sort_keys=True))
PY
}

upload_log() {
  local log_file="$1"
  local target_path="$2"
  if [[ -s "$log_file" ]]; then
    gcloud storage cp "$log_file" "gs://${COMMON_BUCKET}/${target_path}" --content-type=text/plain || true
  fi
}

upload_logs() {
  upload_log "$WORKLOAD_LOG_FILE" "$NODE_WORKLOAD_LOG_PATH"
  upload_log "$LOG_FILE" "$NODE_STARTUP_LOG_PATH"
}

NODE_WORKLOAD_LOG_URI="gs://${COMMON_BUCKET}/${NODE_WORKLOAD_LOG_PATH}"
NODE_STARTUP_LOG_URI="gs://${COMMON_BUCKET}/${NODE_STARTUP_LOG_PATH}"
export NODE_WORKLOAD_LOG_URI NODE_STARTUP_LOG_URI
STARTED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
export STARTED_AT
COMMON_BUCKET_MOUNT=/mnt/disks/common
GENCAST_ZARR_MIRROR_TARGET="${COMMON_BUCKET_MOUNT}/full_field/gencast/${DATE}/init_${DATE}.zarr"
GENCAST_ZARR_MIRROR_WORKERS=16

on_interrupt() {
  status=$?
  write_status "FAILED" "$status" "TPU startup interrupted" "$NODE_STATUS_PATH" || true
  upload_logs || true
  exit "$status"
}
trap on_interrupt INT TERM

echo "Starting TPU workload ${WORKLOAD_NAME} date=${DATE} image=${WORKLOAD_IMAGE}"
echo "Workload log will be uploaded to ${NODE_WORKLOAD_LOG_URI}"
write_status "RUNNING" "" "TPU VM startup running on $(hostname)" "$NODE_STATUS_PATH"

ensure_gcsfuse() {
  if command -v gcsfuse >/dev/null 2>&1; then
    echo "Cloud Storage FUSE already installed: $(gcsfuse --version)"
    if gcsfuse --help 2>&1 | grep -q -- "--profile"; then
      return
    fi
    echo "Installed Cloud Storage FUSE does not support --profile; upgrading."
  fi

  echo "Installing Cloud Storage FUSE for GenCast full-field mirroring."
  apt-get update
  apt-get install -y curl lsb-release
  GCSFUSE_REPO="gcsfuse-$(lsb_release -c -s)"
  echo "deb [signed-by=/usr/share/keyrings/cloud.google.asc] https://packages.cloud.google.com/apt ${GCSFUSE_REPO} main" > /etc/apt/sources.list.d/gcsfuse.list
  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg > /usr/share/keyrings/cloud.google.asc
  apt-get update
  apt-get install -y gcsfuse
  echo "Installed Cloud Storage FUSE: $(gcsfuse --version)"
}

mount_common_bucket() {
  mkdir -p "$COMMON_BUCKET_MOUNT"
  if mountpoint -q "$COMMON_BUCKET_MOUNT"; then
    echo "Cloud Storage FUSE mount already active at ${COMMON_BUCKET_MOUNT}."
    return
  fi

  echo "Mounting gs://${COMMON_BUCKET} at ${COMMON_BUCKET_MOUNT} with Cloud Storage FUSE for GenCast full-field mirroring."
  gcsfuse --implicit-dirs --profile=aiml-checkpointing "$COMMON_BUCKET" "$COMMON_BUCKET_MOUNT"
  if ! mountpoint -q "$COMMON_BUCKET_MOUNT"; then
    echo "Cloud Storage FUSE mount failed at ${COMMON_BUCKET_MOUNT}."
    exit 1
  fi
  echo "Cloud Storage FUSE active: gs://${COMMON_BUCKET} -> ${COMMON_BUCKET_MOUNT}; mirror target ${GENCAST_ZARR_MIRROR_TARGET}"
}

ensure_gcsfuse
mount_common_bucket

systemctl start docker
gcloud auth configure-docker "$ARTIFACT_REGISTRY_HOST" --quiet
docker pull "$WORKLOAD_IMAGE"

echo "Starting workload container; capturing stdout/stderr to ${WORKLOAD_LOG_FILE}."
docker run --rm --privileged --net=host --name "monsoon-${WORKLOAD_NAME}-${DATE}" \
  -v "${COMMON_BUCKET_MOUNT}:${COMMON_BUCKET_MOUNT}" \
  -e DATE="$DATE" \
  -e FORECAST_REGIONS="$FORECAST_REGIONS" \
  -e GCS_COMMON_BUCKET="$COMMON_BUCKET" \
  -e GCS_REGION_BUCKETS="$REGION_BUCKETS" \
  -e UPLOAD_FULL_FIELD="true" \
  -e GENCAST_ZARR_MIRROR_TARGET="$GENCAST_ZARR_MIRROR_TARGET" \
  -e GENCAST_ZARR_MIRROR_WORKERS="$GENCAST_ZARR_MIRROR_WORKERS" \
  -e GENCAST_JAX_DISTRIBUTED="true" \
  -e GENCAST_EXPECTED_GLOBAL_DEVICES="$EXPECTED_GLOBAL_DEVICES" \
  -e GENCAST_EXPECTED_LOCAL_DEVICES="$EXPECTED_LOCAL_DEVICES" \
  -e GENCAST_EXPECTED_PROCESS_COUNT="$EXPECTED_PROCESS_COUNT" \
  -e GENCAST_ENSEMBLE_MEMBERS="$ENSEMBLE_MEMBERS" \
  -e TPU_DISPATCH_RUN_ID="$RUN_ID" \
  -e TPU_DISPATCH_ATTEMPT="$ATTEMPT" \
  -e TPU_DISPATCH_WORKLOAD="$WORKLOAD_NAME" \
  -e TPU_DISPATCH_QUEUED_RESOURCE_ID="$QUEUED_RESOURCE_ID" \
  -e TPU_DISPATCH_NODE_ID="$NODE_ID" \
  -e TPU_DISPATCH_ZONE="$ZONE" \
  "$WORKLOAD_IMAGE" > >(forward_log_stream workload-stdout "$WORKLOAD_LOG_FILE") 2> >(forward_log_stream workload-stderr "$WORKLOAD_LOG_FILE")
exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
  upload_logs || true
  write_status "NODE_SUCCEEDED" "0" "TPU VM workload container exited successfully on $(hostname)" "$NODE_STATUS_PATH" || true
else
  upload_logs || true
  write_status "FAILED" "$exit_code" "TPU workload container failed on $(hostname)" "$NODE_STATUS_PATH" || true
fi
upload_log "$LOG_FILE" "$NODE_STARTUP_LOG_PATH"
exit "$exit_code"
'''


def log_dispatch_context(config: DispatchConfig) -> None:
    logger.info(
        "TPU dispatch context: %s",
        json.dumps(
            {
                "project_id": config.project_id,
                "workload_name": config.workload_name,
                "run_id": config.run_id,
                "date": config.date,
                "workload_image": config.workload_image,
                "forecast_regions": config.forecast_regions,
                "common_bucket": config.common_bucket,
                "region_bucket_keys": sorted(json_keys(config.region_buckets)),
                "tpu_zone": config.tpu_zone,
                "tpu_accelerator_type": config.tpu_accelerator_type,
                "tpu_runtime_version": config.tpu_runtime_version,
                "tpu_spot": config.tpu_spot,
                "tpu_network": config.tpu_network,
                "tpu_subnetwork": config.tpu_subnetwork,
                "tpu_service_account": config.tpu_service_account,
                "artifact_registry_host": config.artifact_registry_host,
                "max_attempts": config.max_attempts,
                "poll_interval_seconds": config.poll_interval_seconds,
                "queue_timeout_seconds": config.queue_timeout_seconds,
                "run_timeout_seconds": config.run_timeout_seconds,
                "request_valid_duration": config.request_valid_duration,
                "status_path": config.status_path,
            },
            sort_keys=True,
        ),
    )


def enum_name(value: Any) -> str:
    name = getattr(value, "name", "")
    if name:
        return name
    return str(value).split(".")[-1].upper()


def normalize_state(value: Any) -> str:
    if value is None:
        return ""
    return str(value).split("/")[-1].upper()


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def operation_name(operation: Any) -> str:
    raw_operation = getattr(operation, "operation", None)
    name = getattr(raw_operation, "name", "")
    return name or getattr(operation, "name", "")


def sanitize_resource_id(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    cleaned = re.sub(r"-+", "-", cleaned)
    return cleaned if len(cleaned) <= 63 else cleaned[:58].rstrip("-")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise DispatchError(f"Missing required environment variable {name}")
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value in {None, ""} else int(value)


def json_keys(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return list(parsed.keys()) if isinstance(parsed, dict) else []


def default_project_id() -> str:
    _, project_id = google.auth.default(scopes=[TPU_SCOPE])
    if not project_id:
        raise DispatchError("PROJECT_ID is required")
    return project_id


def install_signal_handlers() -> None:
    def handler(signum: int, _frame: Any) -> None:
        raise ControllerInterrupted(f"Received signal {signum}")

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main() -> None:
    install_signal_handlers()
    config = DispatchConfig.from_env()
    controller = Controller(config, TpuClient(config), StatusStore())
    logger.info(
        "Dispatching workload=%s date=%s run_id=%s zone=%s accelerator=%s spot=%s",
        config.workload_name,
        config.date,
        config.run_id,
        config.tpu_zone,
        config.tpu_accelerator_type,
        config.tpu_spot,
    )
    controller.run()


if __name__ == "__main__":
    main()
