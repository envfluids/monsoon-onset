import unittest
from pathlib import Path


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "terraform/modules/orchestration/workflow.yaml.tpl"
)


class WorkflowTemplateContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW_PATH.read_text()

    def assertInOrder(self, *snippets):
        cursor = -1
        for snippet in snippets:
            position = self.workflow.find(snippet, cursor + 1)
            self.assertNotEqual(position, -1, f"Missing snippet after offset {cursor}: {snippet}")
            cursor = position

    def test_entrypoint_routes_scheduled_check_and_event_runs(self):
        self.assertInOrder(
            "- requested_date: $${default(map.get(args, \"date\"), \"\")}",
            "- action: $${default(map.get(args, \"action\"), \"run\")}",
            "- event_object_name: $${default(map.get(event_payload, \"name\"), \"\")}",
            "condition: $${event_type != \"\" and text.match_regex(event_object_name, \"^intermediate/.*_done$\")}",
            "next: advance_probe_state",
            "condition: $${event_type != \"\"}",
            "next: return_ignored_event",
            "next: probe_state_initial",
            "condition: $${action == \"check\"}",
            "next: return_checked",
        )
        self.assertIn("status: \"advanced\"", self.workflow)
        self.assertIn("status: \"ignored_event\"", self.workflow)
        self.assertIn("status: \"checked\"", self.workflow)

    def test_downloader_branches_are_independent_and_failure_tolerant(self):
        for source in ("ecmwf", "ncep"):
            self.assertIn(f"- download_{source}:", self.workflow)
            self.assertIn(
                f'{source}_download: $${{default(map.get(state.actions.ic_to_download_by_source, "{source}"), default_ic_download)}}',
                self.workflow,
            )
            self.assertIn(f"- name: SOURCE\n                                      value: \"{source}\"", self.workflow)
            self.assertIn(f"- name: DATE\n                                      value: $${{{source}_download.date}}", self.workflow)
            self.assertIn(f"- log_{source}_download_failed:", self.workflow)
            self.assertIn("severity: ERROR", self.workflow)
        self.assertIn("parallel:\n          branches:", self.workflow)

    def test_batch_model_submissions_have_stable_ids_and_required_environment(self):
        self.assertIn(
            'job_id: $${"${replace(lower(model), "_", "-")}-" + text.replace_all(${model}_action.date, "T", "-")}',
            self.workflow,
        )
        for expected in (
            "DATE: $${${model}_action.date}",
            'MODEL: "${model}"',
            "FORECAST_REGIONS: $${json.encode_to_string(${model}_action.regions)}",
            "GCS_COMMON_BUCKET: $${common_bucket}",
            "GCS_REGION_BUCKETS: $${json.encode_to_string(region_buckets)}",
            "REGION_MODELS: '${jsonencode({ for k, v in regions : k => v.models })}'",
            "REGIONS: '${jsonencode(regions)}'",
            'PROJECT_ID: "${project_id}"',
            'UPLOAD_FULL_FIELD: "${contains(full_field_models, model) ? "true" : "false"}"',
        ):
            self.assertIn(expected, self.workflow)

    def test_batch_jobs_are_private_idempotent_and_retryable(self):
        self.assertInOrder(
            "url: $${\"https://batch.googleapis.com/v1/projects/${project_id}/locations/${region}/jobs?jobId=\" + job_id}",
            "auth:\n              type: OAuth2",
            "maxRetryCount: 3",
            "serviceAccount:\n                  email: \"${pipeline_sa}\"",
            "noExternalIpAddress: true",
            "logsPolicy:\n                destination: CLOUD_LOGGING",
        )
        self.assertIn("condition: $${default(map.get(e, \"code\"), 0) == 409}", self.workflow)
        self.assertIn("next: get_existing_batch_job", self.workflow)
        self.assertIn("condition: $${default(map.get(e, \"code\"), 0) == 404}", self.workflow)
        self.assertIn("next: create_batch_job", self.workflow)
        self.assertIn(
            'condition: $${existing_batch_job.status.state == "FAILED" or existing_batch_job.status.state == "CANCELLED" or existing_batch_job.status.state == "SUCCEEDED"}',
            self.workflow,
        )
        self.assertIn("next: delete_existing_batch_job", self.workflow)
        self.assertIn("return: \"exists\"", self.workflow)
        self.assertIn("return: \"stale_delete_pending\"", self.workflow)

    def test_gencast_dispatch_is_async_and_has_stable_identity(self):
        self.assertInOrder(
            "- submit_gencast:",
            "condition: $${gencast_action.date == \"\"}",
            "next: gencast_submit_done",
            "next: run_gencast_dispatch",
            "call: googleapis.run.v2.projects.locations.jobs.run",
            "- name: WORKLOAD_NAME\n                              value: \"gencast\"",
            "- name: RUN_ID\n                              value: $${\"gencast-\" + text.replace_all(gencast_action.date, \"T\", \"-\")}",
            "- name: DATE\n                              value: $${gencast_action.date}",
            "connector_params:\n                    skip_polling: true",
        )
        for name in (
            "TPU_ZONE",
            "TPU_ACCELERATOR_TYPE",
            "TPU_RUNTIME_VERSION",
            "TPU_SPOT",
            "TPU_NETWORK",
            "TPU_SUBNETWORK",
            "TPU_SERVICE_ACCOUNT",
            "MAX_ATTEMPTS",
            "POLL_INTERVAL_SECONDS",
            "QUEUE_TIMEOUT_SECONDS",
            "RUN_TIMEOUT_SECONDS",
        ):
            self.assertIn(f"- name: {name}", self.workflow)
        self.assertIn("- log_gencast_dispatch_error:", self.workflow)
        self.assertIn("severity: WARNING", self.workflow)

    def test_region_scoped_blend_diagnostics_and_sync_actions_are_guarded(self):
        for stage, action in (
            ("blend", "blend_action"),
            ("diagnostics", "diagnostics_action"),
            ("sync", "sync_action"),
        ):
            self.assertIn(f"- maybe_submit_{stage}:", self.workflow)
            self.assertIn(f"condition: $${{{action}.date == \"\"}}", self.workflow)
        self.assertIn('job_id: $${"blend-" + region_name + "-" + text.replace_all(blend_action.date, "T", "-") + "-" + blend_action.job_suffix}', self.workflow)
        self.assertIn('job_id: $${"diagnostics-" + region_name + "-" + text.replace_all(diagnostics_action.date, "T", "-") + "-" + diagnostics_action.job_suffix}', self.workflow)
        self.assertIn("RUN_MODE: \"blend\"", self.workflow)
        self.assertIn("RUN_MODE: \"diagnostics\"", self.workflow)
        self.assertIn("BLEND_NAMES: $${json.encode_to_string(blend_action.blends)}", self.workflow)
        self.assertIn("BLEND_NAMES: $${json.encode_to_string(diagnostics_action.blends)}", self.workflow)
        self.assertIn("FORECAST_REGION: $${region_name}", self.workflow)
        self.assertIn("SYNC_SPEC\n                              value: $${json.encode_to_string(region_cfg.sync)}", self.workflow)
        self.assertIn("SYNC_FINGERPRINT\n                              value: $${sync_action.fingerprint}", self.workflow)

    def test_pipeline_state_subroutine_uses_oidc_and_preserves_requested_date(self):
        self.assertInOrder(
            "pipeline_state:\n  params: [base_url, date]",
            "condition: $${date == \"\"}",
            "next: set_url_no_date",
            "url: $${base_url + \"/state\"}",
            "url: $${base_url + \"/state?date=\" + date}",
            "auth:\n            type: OIDC",
            "return: $${response.body}",
        )


if __name__ == "__main__":
    unittest.main()
