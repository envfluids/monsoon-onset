import unittest
from pathlib import Path


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "terraform/modules/orchestration/workflow.yaml.tpl"
)
COMPUTE_MAIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "terraform/modules/compute/main.tf"
)
DEV_MAIN_PATH = Path(__file__).resolve().parents[1] / "terraform/environments/dev/main.tf"
PROD_MAIN_PATH = Path(__file__).resolve().parents[1] / "terraform/environments/prod/main.tf"
ORCHESTRATION_PATH = Path(__file__).resolve().parents[1] / "terraform/modules/orchestration"


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
        self.assertIn(
            'mountOptions = batch_config.model_resources[model].gcs_mount_options',
            self.workflow,
        )

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
        self.assertIn('max_run_duration: "${batch_config.model_resources["diagnostics"].max_run_duration}"', self.workflow)
        self.assertIn(
            'mountOptions = batch_config.model_resources["blend"].gcs_mount_options',
            self.workflow,
        )
        self.assertIn(
            'mountOptions = batch_config.model_resources["diagnostics"].gcs_mount_options',
            self.workflow,
        )
        self.assertIn("FORECAST_REGION: $${region_name}", self.workflow)
        self.assertIn("SYNC_SPEC\n                              value: $${json.encode_to_string(region_cfg.sync)}", self.workflow)
        self.assertIn("SYNC_FINGERPRINT\n                              value: $${sync_action.fingerprint}", self.workflow)
        self.assertIn("SYNC_ITEMS\n                              value: $${json.encode_to_string(sync_action.items)}", self.workflow)

    def test_diagnostics_batch_has_longer_timeout_than_blend(self):
        compute_main = COMPUTE_MAIN_PATH.read_text()
        self.assertIn("diagnostics = {", compute_main)
        self.assertIn('max_run_duration    = "7200s"', compute_main)
        diagnostics_block = compute_main.split("diagnostics = {", 1)[1].split("}", 1)[0]
        self.assertIn('machine_type        = "e2-highmem-8"', diagnostics_block)
        self.assertIn("memory_mib          = 65536", diagnostics_block)
        self.assertIn("gcs_mount_options   = local.gcs_fuse_read_cache_mount_options", diagnostics_block)
        self.assertNotIn("provisioning_model", diagnostics_block)

    def test_blend_batch_has_more_memory_for_fuse_read_cache(self):
        compute_main = COMPUTE_MAIN_PATH.read_text()
        blend_block = compute_main.split("blend = {", 1)[1].split("}", 1)[0]

        self.assertIn('machine_type        = "e2-highmem-8"', blend_block)
        self.assertIn("cpu_milli           = 8000", blend_block)
        self.assertIn("memory_mib          = 65536", blend_block)

    def test_aifs_v2_does_not_mount_common_bucket_when_using_gcs_api_upload(self):
        compute_main = COMPUTE_MAIN_PATH.read_text()
        aifs_v2_block = compute_main.split("AIFS_single_v2 = {", 1)[1].split("}", 1)[0]

        self.assertIn("mount_common_bucket = false", aifs_v2_block)
        self.assertIn("gcs_mount_options   = []", aifs_v2_block)

    def test_gcs_fuse_mount_options_use_checkpointing_and_bounded_read_cache(self):
        compute_main = COMPUTE_MAIN_PATH.read_text()

        self.assertIn("--profile=aiml-checkpointing", compute_main)
        self.assertIn("--metadata-cache-negative-ttl-secs=0", compute_main)
        self.assertIn("--cache-dir=/tmp/gcsfuse-cache", compute_main)
        self.assertIn("--file-cache-max-size-mb=49152", compute_main)
        self.assertIn("--file-cache-cache-file-for-range-read=true", compute_main)
        self.assertIn("--file-cache-enable-parallel-downloads=true", compute_main)

    def test_ethiopia_blend_stage_has_matching_sync_rule(self):
        for path in (DEV_MAIN_PATH, PROD_MAIN_PATH):
            with self.subTest(path=path.name):
                content = path.read_text()
                ethiopia_block = content.split("ethiopia = {", 1)[1].split("disabled_stages_by_region", 1)[0]
                self.assertIn('stages = ["blend", "model_diagnostics", "sync"]', ethiopia_block)
                self.assertIn('"blend"', ethiopia_block)

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


def hcl_block(content, opening):
    """Return the brace-balanced block that starts at `opening` (which ends with "{")."""
    start = content.index(opening)
    depth = 0
    for index in range(start, len(content)):
        if content[index] == "{":
            depth += 1
        elif content[index] == "}":
            depth -= 1
            if depth == 0:
                return content[start : index + 1]
    raise AssertionError(f"Unbalanced block: {opening}")


class PerModelEnvAndRegionModelsContractTest(unittest.TestCase):
    STANDARD_ENV = (
        "DATE",
        "MODEL",
        "FORECAST_REGIONS",
        "GCS_COMMON_BUCKET",
        "GCS_REGION_BUCKETS",
        "REGION_MODELS",
        "REGIONS",
        "PROJECT_ID",
        "UPLOAD_FULL_FIELD",
    )

    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW_PATH.read_text()
        cls.dev = DEV_MAIN_PATH.read_text()
        cls.prod = PROD_MAIN_PATH.read_text()
        cls.orchestration_main = (ORCHESTRATION_PATH / "main.tf").read_text()
        cls.orchestration_vars = (ORCHESTRATION_PATH / "variables.tf").read_text()

    def test_batch_model_env_renders_inside_each_model_submission(self):
        submission = self.workflow.split("- submit_${model}_batch:", 1)[1].split(
            "- ${model}_submit_done:", 1
        )[0]
        env_block = submission.split("env_vars:", 1)[1]
        loop = (
            "%{ for name, value in try(batch_model_env[model], {}) ~}\n"
            "                          ${name}: ${jsonencode(value)}\n"
            "%{ endfor ~}\n"
        )
        self.assertIn(loop, env_block)
        self.assertLess(env_block.index("UPLOAD_FULL_FIELD"), env_block.index(loop))
        self.assertEqual(self.workflow.count("batch_model_env"), 1)

    def test_orchestration_module_passes_batch_model_env_with_safe_default(self):
        self.assertIn(
            "batch_model_env     = var.batch_model_env", self.orchestration_main
        )
        variable = hcl_block(self.orchestration_vars, 'variable "batch_model_env" {')
        self.assertIn("type        = map(map(string))", variable)
        self.assertIn("default     = {}", variable)
        self.assertIn('regex("^[A-Z_][A-Z0-9_]*$", name)', variable)
        for name in self.STANDARD_ENV:
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', variable)
                self.assertIn(f"{name}:", self.workflow)

    def test_dev_sets_ensemble_run_size_for_aifs_ens_v2_only(self):
        orchestration = hcl_block(self.dev, 'module "orchestration" {')
        env = hcl_block(orchestration, "batch_model_env = {")
        self.assertEqual(env.count(" = {"), 2)  # the map itself plus AIFS_ENS_v2
        aifs_ens = hcl_block(env, "AIFS_ENS_v2 = {")
        self.assertIn('AIFS_ENS_N_MEMBERS       = "51"', aifs_ens)
        self.assertIn('AIFS_ENS_LEAD_TIME_HOURS = "168"', aifs_ens)

    def test_prod_does_not_set_batch_model_env(self):
        self.assertNotIn("batch_model_env", self.prod)

    def test_dev_runs_ensemble_for_india_and_switches_it_off_for_ethiopia_by_flag(self):
        variable = hcl_block(self.dev, 'variable "disabled_region_models" {')
        self.assertIn('ethiopia = ["AIFS_ENS_v2"]', variable)
        self.assertIn('contains(["india", "ethiopia"], region)', variable)

        base_regions = hcl_block(self.dev, "base_regions = {")
        india = hcl_block(base_regions, "india = {")
        ethiopia = hcl_block(base_regions, "ethiopia = {")
        self.assertIn('models = ["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm"]', india)
        self.assertNotIn(
            "AIFS_ENS_v2", india.split("sync = {", 1)[1]
        )  # no India sync rule yet
        # Ethiopia keeps its ensemble definition; the flag switches it off.
        self.assertIn(
            'models = ["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm", "gencast"]',
            ethiopia,
        )

    def test_region_model_filter_and_exclusions_use_per_region_disabled_models(self):
        by_region = hcl_block(self.dev, "disabled_model_ids_by_region = {")
        self.assertIn("local.disabled_model_ids,", by_region)
        self.assertIn(
            "try(var.disabled_region_models[region_name], toset([]))", by_region
        )
        uses = "local.disabled_model_ids_by_region[region_name]"
        self.assertIn(uses, hcl_block(self.dev, "disabled_stages_by_region = {"))
        self.assertIn(uses, hcl_block(self.dev, "disabled_sync_rules_by_region = {"))
        self.assertIn(
            f"if !contains({uses}, model)", hcl_block(self.dev, "  regions = {")
        )
        # Per-region switches must not change which models upload full fields.
        self.assertIn(
            'full_field_models = setsubtract(toset(["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm"]), '
            "local.disabled_model_ids)",
            self.dev,
        )
        self.assertIn('AIFS_ENS_v2 = {\n      ethiopia = ["AIFS_ENS_v2"]', self.dev)


class DryWetCastJobContractTest(unittest.TestCase):
    """PR 4: the DryWetCast Cloud Run job, its workflow step, build, and dev-only wiring."""

    ROOT = Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        root = cls.ROOT
        cls.workflow = WORKFLOW_PATH.read_text()
        cls.compute_main = COMPUTE_MAIN_PATH.read_text()
        cls.compute_vars = (root / "terraform/modules/compute/variables.tf").read_text()
        cls.dev = DEV_MAIN_PATH.read_text()
        cls.prod = PROD_MAIN_PATH.read_text()
        cls.cloudbuild = (root / "cloudbuild.yaml").read_text()
        cls.image_deps = (root / ".image-deps.yaml").read_text()
        cls.ens_dockerfile = (root / "docker/aifs-ens-v2/Dockerfile").read_text()
        cls.dwc_dockerfile = (root / "docker/drywetcast/Dockerfile").read_text()
        cls.dwc_lock = (root / "docker/drywetcast/requirements.lock").read_text()

    def test_workflow_step_is_guarded_async_and_passes_the_action(self):
        block = self.workflow.split(
            '%{ if contains(keys(cloud_run_jobs), "drywetcast") ~}', 1
        )[1]
        block = block.split("%{ endif ~}", 1)[0]
        for expected in (
            "- submit_drywetcast:",
            'map.get(actions, ["regions_to_drywetcast_by_region", region_name])',
            "jobs/${cloud_run_jobs.drywetcast.name}",
            "value: $${drywetcast_action.date}",
            "value: $${json.encode_to_string(drywetcast_action.configs)}",
            "value: $${string(drywetcast_action.finalize_ncmrwf)}",
            "skip_polling: true",
            "severity: ERROR",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, block)
        self.assertEqual(self.workflow.count("cloud_run_jobs.drywetcast"), 1)

    def test_compute_job_is_optional_and_references_the_existing_secret(self):
        job = self.compute_main.split("    drywetcast = {", 1)[1].split("    }", 1)[0]
        for expected in (
            'memory  = "8Gi"',
            'cpu     = "4"',
            'timeout = "3600s"',
            "retries = 0",
            "secrets = []",
        ):
            self.assertIn(expected, job)
        self.assertIn(
            'if name != "drywetcast" || var.drywetcast_image != null', self.compute_main
        )
        self.assertIn(
            "NCMRWF_API_KEY = var.drywetcast_ncmrwf_secret_id", self.compute_main
        )
        self.assertIn(
            "for_each = try(local.cloud_run_existing_secret_ids[each.key], {})",
            self.compute_main,
        )
        # Referenced only: no Secret Manager resource is built from the existing secret id.
        self.assertEqual(self.compute_main.count("drywetcast_ncmrwf_secret_id"), 1)
        self.assertIn('variable "drywetcast_image" {', self.compute_vars)
        self.assertIn(
            "default     = null",
            self.compute_vars.split('variable "drywetcast_image" {', 1)[1],
        )
        self.assertIn("for_each = var.pipeline_state_env", self.compute_main)

    def test_dev_deploys_the_job_with_the_stage_enabled(self):
        self.assertIn(
            'drywetcast_image     = "${module.storage.artifact_registry_url}/monsoon-drywetcast:latest"',
            self.dev,
        )
        self.assertIn('drywetcast_ncmrwf_secret_id = "ncmrwf-api-key"', self.dev)
        self.assertIn('stages = ["model_diagnostics", "drywetcast", "sync"]', self.dev)
        disabled = self.dev.split('variable "disabled_stages" {', 1)[1].split(
            "validation {", 1
        )[0]
        # Enabled after the NCMRWF reachability probe passed (Oct 8).
        self.assertIn('india    = ["model_diagnostics"]', disabled)
        self.assertNotIn("drywetcast", disabled.split("default = {", 1)[1])
        self.assertIn(
            'contains(["blend", "model_diagnostics", "drywetcast", "sync"], stage)',
            self.dev,
        )
        cutoff = self.dev.split('variable "drywetcast_ncmrwf_cutoff_utc" {', 1)[
            1
        ].split("\n}\n", 1)[0]
        self.assertIn('default     = "14:00"', cutoff)
        self.assertIn(
            "DRYWETCAST_NCMRWF_CUTOFF_UTC = var.drywetcast_ncmrwf_cutoff_utc", self.dev
        )
        # The cutoff only takes effect if a scheduled pass runs at or after it (14:00 UTC).
        self.assertIn('pipeline_schedule       = "0 8,10,14 * * *"', self.dev)

    def test_prod_is_unchanged(self):
        self.assertNotIn("drywetcast", self.prod)

    def test_build_adds_the_image_and_records_code_versions(self):
        self.assertIn("  drywetcast:\n    - docker/drywetcast/", self.image_deps)
        for step in (
            "pull-cache-drywetcast",
            "build-drywetcast",
            "push-drywetcast",
            "deploy-drywetcast",
        ):
            self.assertIn(f"- id: {step}", self.cloudbuild)
        self.assertIn(
            'git rev-parse --short HEAD > code_version 2>/dev/null || echo "${_SHORT_SHA}" > code_version',
            self.cloudbuild,
        )
        ens_build = self.cloudbuild.split("- id: build-aifs-ens-v2", 1)[1].split(
            "- id:", 1
        )[0]
        self.assertIn('--build-arg AIFS_CODE_VERSION="$$(cat code_version)"', ens_build)
        dwc_build = self.cloudbuild.split("- id: build-drywetcast", 1)[1].split(
            "- id:", 1
        )[0]
        self.assertIn('--build-arg CODE_VERSION="$$(cat code_version)"', dwc_build)
        deploy = self.cloudbuild.split("- id: deploy-drywetcast", 1)[1]
        self.assertIn("not created yet; skipping deploy", deploy)

    def test_ensemble_dockerfile_records_code_version_after_the_heavy_layers(self):
        arg = self.ens_dockerfile.index("ARG AIFS_CODE_VERSION=unknown")
        self.assertIn("ENV AIFS_CODE_VERSION=${AIFS_CODE_VERSION}", self.ens_dockerfile)
        self.assertGreater(arg, self.ens_dockerfile.index("RUN pip install"))
        self.assertGreater(arg, self.ens_dockerfile.index("COPY AIFS/utils/"))
        self.assertLess(arg, self.ens_dockerfile.index("ENTRYPOINT"))

    def test_drywetcast_image_pins_the_commit_checksum_and_dependencies(self):
        for expected in (
            "FROM python:3.11-slim-bookworm",
            "libgomp1",
            "ARG DRYWETCAST_COMMIT=56f4805a532718afd61176c85eec75fa8c46b137",
            "ARG DRYWETCAST_SHA256=a77fb404bb26c1732e19aed3f2a11770cb1d8ec18670161af37cc8a7f39afe18",
            "sha256sum -c -",
            "pip install --no-cache-dir --no-deps -r requirements.lock",
            "import xgboost",
        ):
            self.assertIn(expected, self.dwc_dockerfile)
        self.assertGreater(
            self.dwc_dockerfile.index("ARG CODE_VERSION"),
            self.dwc_dockerfile.index("RUN pip install"),
        )
        for pin in (
            "numpy==2.4.6",
            "rasterio==1.4.4",
            "xgboost-cpu==3.2.0",
            "zarr==3.1.6",
            "netcdf4==1.7.4",
        ):
            self.assertIn(pin, self.dwc_lock)
        self.assertNotIn("nvidia", self.dwc_lock)


if __name__ == "__main__":
    unittest.main()
