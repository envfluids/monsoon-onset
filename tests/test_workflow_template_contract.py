import re
import unittest
from pathlib import Path

import yaml


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
            'url: $${"https://batch.googleapis.com/v1/projects/${project_id}/locations/" + batch_region + "/jobs?jobId=" + job_id}',
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
        self.assertIn("SYNC_SPEC\n                                value: $${json.encode_to_string(region_cfg.sync)}", self.workflow)
        self.assertIn("SYNC_FINGERPRINT\n                                value: $${sync_action.fingerprint}", self.workflow)
        self.assertIn("SYNC_ITEMS\n                                value: $${json.encode_to_string(sync_action.items)}", self.workflow)

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
        # Plus the fallback ladder's primary and step jobs, which get the same env.
        ladder_loop = (
            "%{ for name, value in try(batch_model_env[model], {}) ~}\n"
            "            ${name}: ${jsonencode(value)}\n"
            "%{ endfor ~}\n"
        )
        ladder = self.workflow.split("ladder_submit_${model}:", 1)[1]
        self.assertEqual(ladder.count(ladder_loop), 2)
        self.assertEqual(self.workflow.count("batch_model_env"), 3)

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
        services = self.compute_main.split("all_cloud_run_services = {", 1)[1]
        job = services.split("    drywetcast = {", 1)[1].split("    }", 1)[0]
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
        self.assertIn('pipeline_schedule       = "0 8-14 * * *"', self.dev)

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
            "ARG DRYWETCAST_COMMIT=22cdbe5beccd3a776ce9e6039a1f603e3d404604",
            "ARG DRYWETCAST_SHA256=93e1164722f7e7779a2f83f8c8c5c8791d27a6546fe03ae84c2dcaf05fb51bc5",
            "sha256sum -c -",
            "pip install --no-cache-dir --no-deps -r requirements.lock",
            "import xgboost",
            "pandas, geopandas, pyogrio, shapely, pyproj",
            "inference, outlook, subdistrict",
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
            # 22cdbe5's subdistrict product
            "pandas==3.0.6",
            "geopandas==1.2.0",
            "pyogrio==0.13.0",
            "shapely==2.2.0",
            "pyproj==3.7.2",
        ):
            self.assertIn(pin, self.dwc_lock)
        self.assertNotIn("nvidia", self.dwc_lock)


class DeliveryAlertAndRetryCapContractTest(unittest.TestCase):
    """PR B: delivery deadline check, hourly passes, ensemble retry cap, delete hardening."""

    ROOT = Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        root = cls.ROOT
        cls.workflow = WORKFLOW_PATH.read_text()
        cls.dev = DEV_MAIN_PATH.read_text()
        cls.prod = PROD_MAIN_PATH.read_text()
        cls.orch_main = (ORCHESTRATION_PATH / "main.tf").read_text()
        cls.orch_vars = (ORCHESTRATION_PATH / "variables.tf").read_text()
        cls.monitoring = (root / "terraform/modules/monitoring/main.tf").read_text()
        cls.submit = cls.workflow.split("\nsubmit_batch_stage:", 1)[1].split(
            "\npipeline_state:", 1
        )[0]

    def test_delivery_check_branch_is_guarded_and_logs_warning(self):
        block = self.workflow.split('%{ if delivery_check_marker != "" ~}', 2)
        self.assertEqual(len(block), 3)  # dispatch switch + branch, both guarded
        self.assertIn('condition: $${action == "check_delivery"}', block[1])
        branch = block[2].split("%{ endif ~}", 1)[0]
        for expected in (
            "time.format(sys.now())",
            '"%DATE%", delivery_date',
            "call: googleapis.storage.v1.objects.get",
            "object: $${text.url_encode(delivery_marker)}",
            "== 404}\n                    next: log_delivery_late",
            "severity: WARNING",
            "text: '$${\"DELIVERY_LATE gs://",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, branch)
        self.assertIn(
            'delivery_check_marker    = var.delivery_check_schedule == null ? "" : var.delivery_check_marker',
            self.orch_main,
        )

    def test_delivery_check_scheduler_job_is_optional(self):
        job = self.orch_main.split(
            'resource "google_cloud_scheduler_job" "delivery_check" {', 1
        )[1]
        self.assertIn("count = var.delivery_check_schedule == null ? 0 : 1", job)
        self.assertIn('argument = jsonencode({ action = "check_delivery" })', job)
        self.assertIn("paused      = var.scheduler_paused", job)
        self.assertIn('time_zone   = "UTC"', job)
        self.assertIn(
            'default     = "intermediate/AIFS_ENS_v2_india_%DATE%_done"', self.orch_vars
        )

    def test_delivery_alert_matches_both_signals_and_is_rate_limited(self):
        policy = self.monitoring.split(
            'resource "google_monitoring_alert_policy" "delivery_late" {', 1
        )[1]
        policy = policy.split("\nresource ", 1)[0]
        self.assertIn(
            "count = var.enable_alerts && var.enable_delivery_alert ? 1 : 0", policy
        )
        self.assertIn(
            '(textPayload:"DELIVERY_LATE" OR textPayload:"BATCH_RETRY_CAP")', policy
        )
        self.assertIn('period = "3600s"', policy)

    def test_submit_batch_stage_caps_attempts_and_rechecks_before_delete(self):
        self.assertIn("- max_attempts  #", self.submit)
        self.assertIn("attempt: $${string(attempt)}", self.submit)
        self.assertIn('map.get(existing_batch_job, ["labels", "attempt"])', self.submit)
        self.assertIn(
            'max_attempts > 0 and existing_batch_job.status.state != "SUCCEEDED" and previous_attempt >= max_attempts',
            self.submit,
        )
        self.assertIn("text: '$${\"BATCH_RETRY_CAP ", self.submit)
        self.assertIn("recheck_batch_job.uid == existing_batch_job.uid", self.submit)
        # The re-read comes after the cap check and immediately before the only delete.
        order = [
            self.submit.index("- enforce_attempt_cap:"),
            self.submit.index("- recheck_before_delete:"),
            self.submit.index("- verify_same_finished_job:"),
            self.submit.index("- delete_existing_batch_job:"),
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.workflow.count("jobs.delete"), 1)
        self.assertIn("- attempt: $${previous_attempt + 1}", self.submit)
        # A job that vanished between the reads still counts as an attempt.
        self.assertIn("next: mark_recreate_attempted  # already gone", self.submit)

    def test_only_the_ensemble_has_a_cap(self):
        self.assertIn(
            "max_attempts: ${try(batch_model_max_attempts[model], 0)}", self.workflow
        )
        self.assertEqual(
            self.workflow.count("                  max_attempts: 0\n"), 2
        )  # blend, diagnostics
        self.assertIn("AIFS_ENS_v2 = var.ensemble_max_attempts", self.dev)
        self.assertIn(
            "default     = {}",
            self.orch_vars.split('variable "batch_model_max_attempts" {', 1)[1],
        )

    def test_dev_wiring_and_prod_unchanged(self):
        self.assertIn('pipeline_schedule       = "0 8-14 * * *"', self.dev)
        deadline = self.dev.split('variable "india_ensemble_deadline_utc" {', 1)[
            1
        ].split("\n}\n", 1)[0]
        self.assertRegex(deadline, r'default\s+= "08:25"')
        self.assertIn(
            'tonumber(split(":", var.india_ensemble_deadline_utc)[1])', self.dev
        )
        self.assertRegex(self.dev, r"enable_delivery_alert\s+= true")
        self.assertIn(
            "default     = 3",
            self.dev.split('variable "ensemble_max_attempts" {', 1)[1],
        )
        for name in (
            "delivery_check",
            "enable_delivery_alert",
            "batch_model_max_attempts",
        ):
            self.assertNotIn(name, self.prod)


class ExtraPipelinePassesContractTest(unittest.TestCase):
    """Extra scheduler jobs: the early 5-minute window and the half-hourly NCMRWF passes."""

    @classmethod
    def setUpClass(cls):
        cls.orch_main = (ORCHESTRATION_PATH / "main.tf").read_text()
        cls.orch_vars = (ORCHESTRATION_PATH / "variables.tf").read_text()
        cls.dev = DEV_MAIN_PATH.read_text()
        cls.prod = PROD_MAIN_PATH.read_text()

    def block(self, opening):
        return self.orch_main.split(opening, 1)[1].split("\n}\n", 1)[0]

    def test_extra_jobs_mirror_the_main_trigger(self):
        main = self.block('resource "google_cloud_scheduler_job" "pipeline_trigger" {')
        extra = self.block(
            'resource "google_cloud_scheduler_job" "pipeline_trigger_extra" {'
        )
        self.assertIn("for_each = var.extra_pipeline_schedules", extra)
        self.assertIn(
            'name        = "${var.name_prefix}-${var.environment}-pipeline-trigger-${each.key}"',
            extra,
        )
        self.assertIn("schedule    = each.value", extra)
        for shared in (
            'time_zone   = "UTC"',
            "paused      = var.scheduler_paused",
            'uri         = "https://workflowexecutions.googleapis.com/v1/${google_workflows_workflow.main_pipeline.id}/executions"',
            "argument = jsonencode({})",
            "retry_count          = 3",
        ):
            with self.subTest(shared=shared):
                self.assertIn(shared, main)
                self.assertIn(shared, extra)
        self.assertIn(
            "default     = {}",
            self.orch_vars.split('variable "extra_pipeline_schedules" {', 1)[1],
        )

    def test_dev_adds_the_early_window_and_half_hourly_passes_and_prod_none(self):
        self.assertIn('pipeline_schedule       = "0 8-14 * * *"', self.dev)
        extra = self.dev.split("extra_pipeline_schedules = {", 1)[1].split("}", 1)[0]
        # The early window ends at 07:55 so a 5-min pass can't relaunch DryWetCast
        # between the ensemble-triggered run and the :00/:30 passes.
        self.assertIn('early       = "*/5 7 * * *"', extra)
        self.assertNotIn("7-8", extra)
        self.assertIn('ncmrwf-half = "30 8-13 * * *"', extra)
        self.assertNotIn("ncmrwf-0930", self.dev)
        self.assertNotIn("extra_pipeline_schedules", self.prod)

    def test_drywetcast_poll_is_shorter_than_the_pass_spacing(self):
        # A run is ~2.5 min of setup plus the poll. It must end before the next
        # :00/:30 pass so the claim doesn't skip it, and a 13:30 run must end
        # before the 14:00 cutoff pass.
        compute = COMPUTE_MAIN_PATH.read_text()
        env = compute.split("  cloud_run_job_env = {", 1)[1].split("\n  }\n", 1)[0]
        self.assertRegex(
            env.split("drywetcast = {", 1)[1], r'NCMRWF_WAIT_MINUTES += "20"'
        )
        dwc = (
            Path(__file__).resolve().parents[1] / "docker/drywetcast/src/main.py"
        ).read_text()
        self.assertIn('os.environ.get("NCMRWF_WAIT_MINUTES", "30")', dwc)

    def test_drywetcast_job_gets_the_versioned_subdistrict_bundle(self):
        compute = COMPUTE_MAIN_PATH.read_text()
        env = compute.split("  cloud_run_job_env = {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn(
            'DRYWETCAST_SUBDISTRICT_GCS = "gs://${var.common_gcs_bucket}/weights/drywetcast/subdistrict/v1/"',
            env.split("drywetcast = {", 1)[1],
        )
        dwc = (
            Path(__file__).resolve().parents[1] / "docker/drywetcast/src/main.py"
        ).read_text()
        self.assertIn('os.environ.get("DRYWETCAST_SUBDISTRICT_GCS", "")', dwc)
        # The job must never set the path DryWetCast reads; the wrapper sets it
        # only after the copy verifies.
        self.assertNotIn("DRYWETCAST_SUBDISTRICT_DATA", compute)

    def test_cloud_run_resources_ignore_gcloud_client_fields(self):
        # gcloud deploys set client/client_version; without the ignore they show
        # up as drift in every plan.
        compute = COMPUTE_MAIN_PATH.read_text()
        for resource, image in (
            (
                'resource "google_cloud_run_v2_job" "pipeline_jobs"',
                "template[0].template[0].containers[0].image,",
            ),
            (
                'resource "google_cloud_run_v2_service" "pipeline_state"',
                "template[0].containers[0].image,",
            ),
        ):
            with self.subTest(resource=resource):
                body = compute.split(resource, 1)[1].split("\nresource ", 1)[0]
                ignored = body.split("ignore_changes = [", 1)[1].split("]\n", 1)[0]
                for field in (image, "client,", "client_version,"):
                    self.assertIn(field, ignored)


class DriveSyncContractTest(unittest.TestCase):
    """DryWetCast outputs to Google Drive through the sync job."""

    ROOT = Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW_PATH.read_text()
        cls.dev = DEV_MAIN_PATH.read_text()
        cls.prod = PROD_MAIN_PATH.read_text()
        cls.compute = COMPUTE_MAIN_PATH.read_text()
        cls.compute_vars = (cls.ROOT / "terraform/modules/compute/variables.tf").read_text()
        cls.monitoring = (cls.ROOT / "terraform/modules/monitoring/main.tf").read_text()
        cls.sync_yaml = (cls.ROOT / "sync/config/sync.yaml").read_text()

    def test_run_sync_is_async_and_never_fails_the_pass(self):
        block = self.workflow.split("    - submit_sync:", 1)[1].split("    - submit_", 1)[0]
        for expected in (
            "- run_sync:\n                try:",
            "call: googleapis.run.v2.projects.locations.jobs.run",
            "skip_polling: true",
            "except:",
            "- log_sync_start_error:",
            "severity: WARNING",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, block)
        self.assertNotIn("severity: ERROR", block)

    def test_dev_drywetcast_rule_is_behind_a_switch_that_is_on(self):
        # On after the sync job's probe passed; the switch stays so Drive
        # delivery can be turned off without touching the rules.
        variable = hcl_block(self.dev, 'variable "drywetcast_drive_sync" {')
        self.assertIn("type        = bool", variable)
        self.assertRegex(variable, r"default += true")
        india = hcl_block(hcl_block(self.dev, "base_regions = {"), "india = {")
        self.assertIn('var.drywetcast_drive_sync ? ["drywetcast"] : []', india)
        always = hcl_block(india, "sync = {").split("concat(", 1)[1].split("var.", 1)[0]
        self.assertNotIn("drywetcast", always)

    def test_git_push_is_off_for_every_dev_region(self):
        # Prod's spec is left as is; the sync job's git push is a no-op there.
        self.assertNotIn("git_push  = true", self.dev)
        india = hcl_block(hcl_block(self.dev, "base_regions = {"), "india = {")
        self.assertIn("git_push  = false", india)

    def test_sync_job_is_headless_and_gets_the_folder_id(self):
        sync_env = self.compute.split("    sync = merge(", 1)[1].split("\n    )\n", 1)[0]
        self.assertIn('MONSOON_DRIVE_HEADLESS = "1"', sync_env)
        self.assertIn(
            'var.drive_folder_id == "" ? {} : { MONSOON_DRIVE_FOLDER_ID = var.drive_folder_id }',
            sync_env,
        )
        self.assertIn('default     = ""', hcl_block(self.compute_vars, 'variable "drive_folder_id" {'))
        self.assertIn(
            'drive_folder_id = "1zGAcTf_kovWZvKlI1j2ud9Hrx207Sv04"',
            hcl_block(self.dev, 'module "compute" {'),
        )

    def test_stale_sync_claims_outlast_every_attempt_of_the_sync_job(self):
        # A claim is stale only after the job's attempts could all have run, and
        # pipeline-state and the sync job must agree on when that is.
        import re

        state = (self.ROOT / "docker/pipeline-state/src/main.py").read_text()
        wrapper = (self.ROOT / "docker/sync/src/main.py").read_text()
        ttl = {}
        for name, source in (
            ("ACTIVE_SYNC_MAX_AGE_SECONDS", state),
            ("SYNC_ITEM_CLAIM_TTL_SECONDS", wrapper),
        ):
            match = re.search(rf'"{name}", str\((\d+) \* 60\)\)', source)
            self.assertIsNotNone(match, name)
            ttl[name] = int(match.group(1)) * 60
        self.assertEqual(ttl["ACTIVE_SYNC_MAX_AGE_SECONDS"], 45 * 60)
        self.assertEqual(ttl["ACTIVE_SYNC_MAX_AGE_SECONDS"], ttl["SYNC_ITEM_CLAIM_TTL_SECONDS"])

        services = self.compute.split("all_cloud_run_services = {", 1)[1]
        job = services.split("    sync = {", 1)[1].split("    }", 1)[0]
        timeout = int(re.search(r'timeout = "(\d+)s"', job).group(1))
        retries = int(re.search(r"retries = (\d+)", job).group(1))
        self.assertGreater(ttl["ACTIVE_SYNC_MAX_AGE_SECONDS"], timeout * (retries + 1))

    def test_sync_yaml_drywetcast_rule_mirrors_config_folders(self):
        rule = self.sync_yaml.split("      - name: drywetcast\n", 1)[1].split("\n\n", 1)[0]
        for expected in (
            "kind: dated_directory",
            'gcs_prefix: "output/drywetcast/{date}/"',
            'gcs_stage_dir: "drywetcast/output/india/{date}"',
            'local_root: "drywetcast/output/india"',
            'drive_path: "{drive_root}/{date}"',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, rule)

    def test_log_alerts_for_subdistrict_and_repeated_sync_failures(self):
        for resource, tokens, job in (
            (
                'resource "google_monitoring_alert_policy" "drywetcast_subdistrict" {',
                ("DRYWETCAST_SUBDISTRICT_UNAVAILABLE", "DRYWETCAST_SUBDISTRICT_FAILED"),
                "drywetcast",
            ),
            (
                'resource "google_monitoring_alert_policy" "sync_gave_up" {',
                ("SYNC_ITEMS_GAVE_UP",),
                "sync",
            ),
        ):
            with self.subTest(resource=resource):
                block = hcl_block(self.monitoring, resource)
                self.assertIn("count = var.enable_alerts ? 1 : 0", block)
                self.assertIn(
                    f'resource.labels.job_name="${{var.name_prefix}}-${{var.environment}}-{job}"',
                    block,
                )
                for token in tokens:
                    self.assertIn(f'textPayload:"{token}"', block)
                self.assertIn('period = "3600s"', block)
                self.assertIn("notification_channels = [for ch in google_monitoring_notification_channel.email : ch.id]", block)



class EnsembleLadderContractTest(unittest.TestCase):
    """The fallback ladder: pipeline-state decides, the workflow acts, jobs claim."""

    ROOT = Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW_PATH.read_text()
        cls.dev = DEV_MAIN_PATH.read_text()
        cls.prod = PROD_MAIN_PATH.read_text()
        cls.orchestration_main = (ORCHESTRATION_PATH / "main.tf").read_text()
        cls.orchestration_vars = (ORCHESTRATION_PATH / "variables.tf").read_text()
        cls.monitoring = (cls.ROOT / "terraform/modules/monitoring/main.tf").read_text()
        cls.storage = (cls.ROOT / "terraform/modules/storage/main.tf").read_text()
        cls.storage_vars = (
            cls.ROOT / "terraform/modules/storage/variables.tf"
        ).read_text()
        cls.pipeline_state = (
            cls.ROOT / "docker/pipeline-state/src/main.py"
        ).read_text()
        cls.ens_wrapper = (cls.ROOT / "docker/aifs-ens-v2/src/main.py").read_text()
        cls.ladder = cls.workflow.split("ensemble_ladder_${model}:\n", 1)[1].split(
            "\nladder_submit_${model}:", 1
        )[0]
        cls.ladder_submit = cls.workflow.split("ladder_submit_${model}:\n", 1)[1].split(
            "\npipeline_state:", 1
        )[0]
        cls.submit = cls.workflow.split("submit_batch_stage:\n  params:", 1)[1].split(
            "\npipeline_state:", 1
        )[0]

    def assertOrdered(self, text, *snippets):
        positions = [text.index(snippet) for snippet in snippets]
        self.assertEqual(positions, sorted(positions), snippets)

    def test_ladder_is_rendered_only_for_configured_models(self):
        self.assertIn(
            "%{ for model, ladder in ensemble_ladder ~}\n# ----", self.workflow
        )
        self.assertIn(
            "%{ if length(ensemble_ladder) > 0 ~}\n    - maybe_check_ensemble:",
            self.workflow,
        )
        self.assertIn("%{ if contains(keys(ensemble_ladder), model) ~}", self.workflow)
        self.assertIn(
            "if contains(local.batch_models_in_use, model)", self.orchestration_main
        )

    def test_check_action_only_runs_the_ladder(self):
        block = self.workflow.split("- check_ensemble_init:", 1)[1].split(
            "- return_ensemble_checked:", 1
        )[0]
        self.assertIn("call: ensemble_ladder_${model}", block)
        self.assertIn("run_primary: false", block)
        self.assertNotIn("submit_ready_work", block)
        self.assertNotIn("drywetcast", block)
        self.assertIn("next: check_ensemble_init", self.workflow)
        self.assertIn('condition: $${action == "check_ensemble"}', self.workflow)

    def test_normal_passes_resubmit_only_the_job_that_owns_the_date(self):
        self.assertIn(
            "call: ensemble_ladder_${model}\n"
            "                      args:\n"
            "                        date: $${${model}_action.date}\n"
            "                        regions: $${${model}_action.regions}\n"
            "                        run_primary: true",
            self.workflow,
        )
        self.assertOrdered(
            self.ladder,
            'condition: $${run_primary and ladder.winner_step != ""}',
            "next: resubmit_ladder_winner",
            "condition: $${run_primary and not ladder.ladder_active}",
            "next: submit_ladder_primary",
        )
        # The primary is never resubmitted while a fallback is queued or owns the date.
        self.assertEqual(self.ladder.count('step: "primary"'), 1)

    def test_decision_comes_from_pipeline_state(self):
        self.assertIn(
            'url: $${"${pipeline_state_url}/ensemble_ladder?model=${model}&date=" + date}',
            self.ladder,
        )
        self.assertIn("type: OIDC", self.ladder)
        # An old pipeline-state image (404) falls back to today's primary-only submit.
        fetch = self.ladder.split("- fetch_ladder:", 1)[1].split("- read_ladder:", 1)[0]
        self.assertIn('condition: $${default(map.get(e, "code"), 0) != 404}', fetch)
        self.assertIn("raise: $${e}", fetch)
        self.assertIn("ENSEMBLE_LADDER_UNAVAILABLE", fetch)
        self.assertIn("ladder_active: false", fetch)
        self.assertIn('winner_step: ""', fetch)
        self.assertIn('if parsed.path == "/ensemble_ladder":', self.pipeline_state)

    def test_losers_are_cancelled_only_after_a_uid_recheck(self):
        self.assertOrdered(
            self.ladder,
            "- cancel_ladder_losers:",
            "in: $${ladder.cancel}",
            "call: googleapis.batch.v1.projects.locations.jobs.get",
            "loser_job.uid == loser.uid",
            '$${"https://batch.googleapis.com/v1/" + loser.name + ":cancel"}',
            "ENSEMBLE_LADDER_CANCELLED",
        )
        self.assertNotIn("jobs.delete", self.ladder)
        self.assertIn('loser_job.status.state == "QUEUED"', self.ladder)
        self.assertIn("ENSEMBLE_LADDER_CANCEL_FAILED", self.ladder)

    def test_claims_are_create_only(self):
        for step in ("- write_forced_ladder_claim:", "- mark_ladder_won_logged:"):
            with self.subTest(step=step):
                block = self.ladder.split(step, 1)[1].split("\n    - ", 1)[0]
                self.assertIn("ifGenerationMatch: 0", block)
                self.assertIn("== 412", block)
        self.assertIn('name: $${claim_prefix + "/winner"}', self.ladder)
        self.assertIn('name: $${claim_prefix + "/won_logged"}', self.ladder)
        # The forced claim is written before the forced step is submitted.
        self.assertOrdered(
            self.ladder, "- write_forced_ladder_claim:", "- submit_ladder_step:"
        )

    def test_one_claim_path_everywhere(self):
        self.assertIn('claim_prefix: $${"batch-state/${model}/" + date}', self.ladder)
        self.assertIn(
            'claim_object: $${"batch-state/${model}/" + date + "/winner"}',
            self.ladder_submit,
        )
        self.assertIn('return f"batch-state/{model}/{date}"', self.pipeline_state)
        self.assertIn(
            'return f"{ladder_state_prefix(model, date)}/winner"', self.pipeline_state
        )
        self.assertIn('os.environ.get("ENSEMBLE_CLAIM_OBJECT", "")', self.ens_wrapper)

    def test_every_ladder_job_claims_before_running(self):
        self.assertEqual(
            self.ladder_submit.count("ENSEMBLE_CLAIM_OBJECT: $${claim_object}"), 2
        )
        self.assertIn("LADDER_JOB_ID: $${primary_job_id}\n", self.ladder_submit)
        self.assertIn(
            'LADDER_JOB_ID: $${primary_job_id + "-${step.name}"}', self.ladder_submit
        )
        main = self.ens_wrapper.split("def main(", 1)[1]
        self.assertOrdered(
            main,
            "if not claim_ladder_win(common_bucket, model, date):",
            "_setup_directories(regions, model)",
            "_download_inputs(date, model, common_bucket)",
        )
        self.assertIn("if_generation_match=0", self.ens_wrapper)
        # Without GCS the primary runs and fallbacks exit, so the container's
        # primary id must be the workflow's.
        self.assertIn(
            '$${"${replace(lower(model), "_", "-")}-" + '
            'text.replace_all(date, "T", "-")}',
            self.ladder_submit,
        )
        self.assertIn(
            "primary_job_id = f\"{model.lower().replace('_', '-')}-"
            "{date.replace('T', '-')}\"",
            self.ens_wrapper,
        )

    def test_step_jobs_use_their_own_shape_region_and_labels(self):
        step = self.ladder_submit.split("- submit_ladder_job_${step.name}:", 1)[1]
        self.assertIn('job_id: $${primary_job_id + "-${step.name}"}', step)
        self.assertIn('machine_type: "${step.machine_type}"', step)
        self.assertIn("count = step.gpu_count", step)
        self.assertIn('batch_region: "${step.region}"', step)
        self.assertIn('batch_subnet: "${step.subnet}"', step)
        self.assertIn('ladder-step: "${step.name}"', step)
        self.assertIn("ladder-of: $${primary_job_id}", step)
        # A stockout isn't a failed attempt: steps inherit the primary's attempt.
        self.assertIn("initial_attempt: $${attempt}", step)
        self.assertIn("attempt: $${int(ladder.primary_attempt)}", self.ladder)
        # No full-field mirror (and no bucket mount) unless the step keeps it.
        self.assertIn(
            '${step.upload_full_field && contains(full_field_models, model) ? "true" : "false"}',
            step,
        )
        self.assertIn("volumes: ${step.upload_full_field && batch_config", step)

    def test_submit_batch_stage_defaults_are_todays_job(self):
        self.assertIn('- batch_region: "${region}"', self.submit)
        self.assertIn('- batch_subnet: "${batch_config.vpc_subnet}"', self.submit)
        self.assertIn("- initial_attempt: 1", self.submit)
        # No default (a map isn't a literal): every call passes it, {} for none.
        self.assertIn("    - extra_labels  #", self.submit)
        self.assertEqual(self.workflow.count("call: submit_batch_stage"), 5)
        self.assertEqual(self.workflow.count("extra_labels: {}"), 4)
        self.assertIn('- $${"regions/" + batch_region}', self.submit)
        self.assertIn("subnetwork: $${batch_subnet}", self.submit)
        self.assertIn("labels: $${batch_labels}", self.submit)
        # The attempt label follows every (re)create.
        self.assertEqual(
            self.submit.count("- batch_labels.attempt: $${string(attempt)}"), 2
        )
        self.assertNotIn('"regions/${region}"', self.workflow)

    def test_fallback_logs_feed_the_fallback_alert(self):
        self.assertIn("ENSEMBLE_FALLBACK_SUBMITTED ${model}", self.ladder)
        self.assertIn("ENSEMBLE_FALLBACK_WON ${model}", self.ladder)
        self.assertIn('condition: $${step_submit.status == "created"}', self.ladder)
        policy = hcl_block(
            self.monitoring,
            'resource "google_monitoring_alert_policy" "ensemble_fallback" {',
        )
        self.assertIn(
            "count = var.enable_alerts && var.enable_ensemble_fallback_alert ? 1 : 0",
            policy,
        )
        self.assertIn(
            '(textPayload:"ENSEMBLE_FALLBACK_SUBMITTED" OR textPayload:"ENSEMBLE_FALLBACK_WON")',
            policy,
        )
        self.assertIn('period = "300s"', policy)
        self.assertRegex(self.dev, r"enable_ensemble_fallback_alert\s+= true")

    def test_claim_unavailable_feeds_the_fallback_alert(self):
        self.assertEqual(
            self.ens_wrapper.count('"ENSEMBLE_LADDER_CLAIM_UNAVAILABLE gs://'), 2
        )
        # Its own policy: a log-match policy takes exactly one condition.
        policy = hcl_block(
            self.monitoring,
            'resource "google_monitoring_alert_policy" "ensemble_ladder_claim_unavailable" {',
        )
        self.assertIn(
            "count = var.enable_alerts && var.enable_ensemble_fallback_alert ? 1 : 0",
            policy,
        )
        condition = policy.split("filter = <<-EOT\n", 1)[1].split("EOT\n", 1)[0]
        # The token comes from the Batch job's container log, not the workflow;
        # Batch logs every stderr line at ERROR, so no severity filter.
        self.assertIn('resource.type="batch.googleapis.com/Job"', condition)
        self.assertIn('textPayload:"ENSEMBLE_LADDER_CLAIM_UNAVAILABLE"', condition)
        self.assertNotIn("severity", condition)
        documentation = policy.split("documentation {", 1)[1]
        self.assertIn("ENSEMBLE_LADDER_CLAIM_UNAVAILABLE", documentation)
        fallback = hcl_block(
            self.monitoring,
            'resource "google_monitoring_alert_policy" "ensemble_fallback" {',
        )
        self.assertNotIn("ENSEMBLE_LADDER_CLAIM_UNAVAILABLE", fallback)

    def test_two_minute_check_schedule(self):
        job = hcl_block(
            self.orchestration_main,
            'resource "google_cloud_scheduler_job" "ensemble_ladder" {',
        )
        self.assertIn("count = length(var.ensemble_ladder) > 0 ? 1 : 0", job)
        self.assertIn('argument = jsonencode({ action = "check_ensemble" })', job)
        self.assertIn("paused      = var.scheduler_paused", job)
        variable = hcl_block(
            self.orchestration_vars, 'variable "ensemble_ladder_schedule" {'
        )
        self.assertIn('default     = "*/2 7-9 * * *"', variable)

    def test_scheduler_jobs_leave_default_retries_unset(self):
        # retry_count = 0 is Cloud Scheduler's default: the API returns no
        # retryConfig for it, so the block shows as a change on every plan.
        job = hcl_block(
            self.orchestration_main,
            'resource "google_cloud_scheduler_job" "ensemble_ladder" {',
        )
        self.assertNotIn("retry_config {", job)
        self.assertIn("default is no retries", job)
        self.assertNotRegex(self.orchestration_main, r"retry_count\s*=\s*0\b")

    def test_pipeline_state_can_read_batch_jobs_in_dev_only(self):
        grant = hcl_block(
            self.storage,
            'resource "google_project_iam_member" "pipeline_batch_job_viewer" {',
        )
        self.assertIn('role    = "roles/batch.jobsViewer"', grant)
        self.assertIn("count = var.grant_pipeline_batch_job_viewer ? 1 : 0", grant)
        self.assertIn(
            "default     = false",
            hcl_block(
                self.storage_vars, 'variable "grant_pipeline_batch_job_viewer" {'
            ),
        )
        self.assertIn("grant_pipeline_batch_job_viewer = true", self.dev)

    def test_dev_ladder_is_fb2_on_one_gpu_after_ten_minutes(self):
        ladder = hcl_block(self.dev, "locals {\n  ensemble_ladder = {")
        self.assertIn("after_minutes = var.ensemble_ladder_after_minutes", ladder)
        wait = hcl_block(self.dev, 'variable "ensemble_ladder_after_minutes" {')
        self.assertIn("default     = 10", wait)
        self.assertIn("var.ensemble_ladder_after_minutes >= 1", wait)
        self.assertIn('marker_region = "india"', ladder)
        self.assertIn('name              = "fb2"', ladder)
        self.assertIn('machine_type      = "a2-highgpu-1g"', ladder)
        self.assertIn("gpu_count         = 1", ladder)
        self.assertIn("region            = var.region", ladder)
        self.assertIn("upload_full_field = true", ladder)
        self.assertIn("force         = var.ensemble_ladder_force", ladder)
        force = hcl_block(self.dev, 'variable "ensemble_ladder_force" {')
        self.assertIn('default     = ""', force)
        self.assertIn("ensemble_ladder       = local.ensemble_ladder", self.dev)
        self.assertIn("ENSEMBLE_LADDER = jsonencode({", self.dev)

    def test_ladder_variable_defaults_off_and_is_validated(self):
        variable = hcl_block(self.orchestration_vars, 'variable "ensemble_ladder" {')
        self.assertIn("default = {}", variable)
        self.assertIn('regex("^fb[0-9]$", step.name)', variable)
        self.assertIn("ladder.after_minutes >= 1", variable)

    def test_prod_has_no_ladder(self):
        for name in (
            "ensemble_ladder",
            "ENSEMBLE_LADDER",
            "grant_pipeline_batch_job_viewer",
            "enable_ensemble_fallback_alert",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, self.prod)


if __name__ == "__main__":
    unittest.main()


def stub_render(template):
    """The template as Workflows YAML without Terraform: directive lines
    dropped (each loop body kept once), each ${...} interpolation replaced by
    TF, and $${ unescaped to ${."""
    text = "\n".join(
        line for line in template.splitlines() if not line.lstrip().startswith("%{")
    )
    out, i = [], 0
    while i < len(text):
        if text.startswith("$${", i):
            out.append("${")
            i += 3
        elif text.startswith("${", i):
            depth, i = 1, i + 2
            while depth:
                depth += {"{": 1, "}": -1}.get(text[i], 0)
                i += 1
            out.append("TF")
        else:
            out.append(text[i])
            i += 1
    return yaml.safe_load("".join(out))


def steps_of(steps):
    for step in steps or []:
        ((name, body),) = step.items()
        yield name, body


def nested_blocks(body):
    """(kind, steps) of the blocks nested in one step."""
    if isinstance(body.get("try"), dict) and "steps" in body["try"]:
        yield "try", body["try"]["steps"]
    if body.get("except"):
        yield "except", body["except"].get("steps", [])
    if "steps" in body:
        yield "steps", body["steps"]
    for condition in body.get("switch") or []:
        if "steps" in condition:
            yield "switch", condition["steps"]
    if "for" in body:
        yield "for", body["for"].get("steps", [])
    for branch in (body.get("parallel") or {}).get("branches", []):
        ((_, branch_body),) = branch.items()
        yield "branch", branch_body.get("steps", [])


def walk(steps):
    for name, body in steps_of(steps):
        yield name, body
        for _, inner in nested_blocks(body):
            yield from walk(inner)


def jumps(body):
    if "next" in body:
        yield body["next"]
    for condition in body.get("switch") or []:
        if "next" in condition:
            yield condition["next"]


def created(body):
    names = {
        re.split(r"[.\[]", key, maxsplit=1)[0]
        for item in body.get("assign") or []
        for key in item
    }
    for block in (body, body.get("try")):
        if isinstance(block, dict) and "result" in block:
            names.add(block["result"])
    return names


def read(body):
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', yaml.safe_dump(body))
    return set(
        re.findall(r"(?<![\w.])([A-Za-z_]\w*)", " ".join(re.findall(r"\$\{.*", text)))
    )


class WorkflowSyntaxContractTest(unittest.TestCase):
    """Rules from the Google Workflows syntax reference that `tofu validate`
    can't see: a breach only shows when the workflow is deployed (#32's apply
    failed on the first one)."""

    @classmethod
    def setUpClass(cls):
        cls.template = WORKFLOW_PATH.read_text()
        cls.doc = stub_render(cls.template)

    def test_stub_render_has_every_subworkflow(self):
        names = set(
            re.findall(
                r"^([A-Za-z_][\w$}{.]*):\n  params:", self.template, re.MULTILINE
            )
        )
        self.assertEqual(len(self.doc), len(names))
        self.assertIn("submit_batch_stage", self.doc)

    def test_subworkflow_parameter_defaults_are_literals(self):
        # "default value for parameter ... must be a literal": a quoted string,
        # a number, true/false or null; not a map, list or expression.
        literal = re.compile(
            r'^("[^"$]*(\$\{[^}$]*\}[^"$]*)*"|-?\d+(\.\d+)?|true|false|null)$'
        )
        checked = 0
        for block in re.findall(
            r"^  params:\n((?:    .*\n)+)", self.template, re.MULTILINE
        ):
            for line in block.splitlines():
                item = line.split("#", 1)[0].strip()
                if not item.startswith("- ") or ":" not in item:
                    continue
                name, default = (part.strip() for part in item[2:].split(":", 1))
                checked += 1
                with self.subTest(param=name):
                    self.assertRegex(default, literal)
        for inline in re.findall(r"^  params: \[(.*)\]$", self.template, re.MULTILINE):
            self.assertNotIn(":", inline)
        self.assertGreaterEqual(checked, 3)
        for sub, spec in self.doc.items():
            for param in spec.get("params") or []:
                if isinstance(param, dict):
                    ((name, default),) = param.items()
                    with self.subTest(subworkflow=sub, param=name):
                        self.assertNotIsInstance(default, (dict, list))

    def test_subworkflow_calls_pass_every_required_parameter(self):
        signatures = {}
        for sub, spec in self.doc.items():
            params = spec.get("params") or []
            required = {p for p in params if isinstance(p, str)}
            optional = {next(iter(p)) for p in params if isinstance(p, dict)}
            signatures[sub] = (required, optional)
        calls = 0
        for sub, spec in self.doc.items():
            for name, body in walk(spec["steps"]):
                callee = body.get("call")
                if callee not in signatures:
                    continue
                calls += 1
                required, optional = signatures[callee]
                passed = set(body.get("args") or {})
                with self.subTest(step=f"{sub}.{name}", call=callee):
                    self.assertEqual(required - passed, set())
                    self.assertEqual(passed - required - optional, set())
        self.assertGreaterEqual(calls, 8)
        self.assertIn("extra_labels", signatures["submit_batch_stage"][0])

    def test_jumps_inside_for_loops_stay_at_loop_level(self):
        # "Only jumping between named steps belonging to the same for loop is
        # allowed"; next: continue/break is documented only on a loop's own
        # steps, so nothing jumps from inside a try, except or steps block.
        loops = 0
        for sub, spec in self.doc.items():
            for name, body in walk(spec["steps"]):
                loop = body.get("for") or (body.get("parallel") or {}).get("for")
                if not loop:
                    continue
                loops += 1
                own = {step for step, _ in steps_of(loop["steps"])}
                for step, step_body in steps_of(loop["steps"]):
                    for target in jumps(step_body):
                        with self.subTest(loop=f"{sub}.{name}", step=step):
                            self.assertIn(target, own | {"continue", "break", "end"})
                    for _, inner in nested_blocks(step_body):
                        for inner_name, inner_body in walk(inner):
                            with self.subTest(loop=f"{sub}.{name}", step=inner_name):
                                self.assertEqual(list(jumps(inner_body)), [])
        self.assertGreaterEqual(loops, 4)

    def test_except_blocks_only_set_variables_that_already_exist(self):
        # A variable created in an except block is local to it; one read after
        # the block must be assigned before it.
        def check(sub, steps, known):
            known = set(known)
            items = list(steps_of(steps))
            for index, (name, body) in enumerate(items):
                for kind, inner in nested_blocks(body):
                    if kind == "except":
                        local = set().union(*(created(b) for _, b in walk(inner)))
                        later = set().union(
                            set(), *(read(b) for _, b in items[index + 1 :])
                        )
                        with self.subTest(step=f"{sub}.{name}"):
                            self.assertEqual((local - known) & later, set())
                    check(sub, inner, known | created(body))
                known |= created(body)

        for sub, spec in self.doc.items():
            params = {
                p if isinstance(p, str) else next(iter(p))
                for p in spec.get("params") or []
            }
            check(sub, spec["steps"], params)
        ladder = self.template.split("ensemble_ladder_${model}:\n", 1)[1]
        self.assertLess(
            ladder.index("- ladder_response: null"), ladder.index("- fetch_ladder:")
        )


class MonitoringAlertPolicyContractTest(unittest.TestCase):
    """Cloud Monitoring API rules `tofu validate` can't see (#33's apply
    failed on the first): a policy with a log-match condition has exactly one
    condition, and needs a notification rate limit."""

    MONITORING = ORCHESTRATION_PATH.parent / "monitoring/main.tf"

    @classmethod
    def setUpClass(cls):
        content = cls.MONITORING.read_text()
        names = re.findall(
            r'^resource "google_monitoring_alert_policy" "(\w+)" \{',
            content,
            re.MULTILINE,
        )
        cls.policies = {
            name: hcl_block(
                content, f'resource "google_monitoring_alert_policy" "{name}" {{'
            )
            for name in names
        }

    def test_log_match_policies_have_exactly_one_condition(self):
        log_policies = [
            name
            for name, block in self.policies.items()
            if "condition_matched_log" in block
        ]
        for name in log_policies:
            block = self.policies[name]
            with self.subTest(policy=name):
                self.assertEqual(
                    len(re.findall(r"^\s*conditions \{", block, re.MULTILINE)), 1
                )
                self.assertEqual(block.count("condition_matched_log {"), 1)
                self.assertIn("notification_rate_limit {", block)
        self.assertGreaterEqual(len(log_policies), 5)

    def test_every_policy_has_conditions(self):
        for name, block in self.policies.items():
            with self.subTest(policy=name):
                self.assertGreaterEqual(
                    len(re.findall(r"^\s*conditions \{", block, re.MULTILINE)), 1
                )
        self.assertGreaterEqual(len(self.policies), 7)
