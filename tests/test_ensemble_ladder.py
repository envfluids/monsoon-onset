"""pipeline-state's fallback-ladder decisions (/ensemble_ladder), on fake Batch jobs."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from test_pipeline_state_actions import (
    COMMON_BUCKET,
    FakeStorageClient,
    load_pipeline_state,
)

MODEL = "AIFS_ENS_v2"
DATE = "20261014T00"
PRIMARY = "aifs-ens-v2-20261014-00"
PRIMARY_CREATED = datetime(2026, 10, 14, 7, 38, tzinfo=timezone.utc)
MARKER = f"intermediate/{MODEL}_india_{DATE}_done"
WINNER = f"batch-state/{MODEL}/{DATE}/winner"
WON_LOGGED = f"batch-state/{MODEL}/{DATE}/won_logged"

LADDER_A = {
    MODEL: {
        "project_id": "proj",
        "primary_region": "us-central1",
        "marker_region": "india",
        "after_minutes": 10,
        "force": "",
        "steps": [{"name": "fb2", "region": "us-central1"}],
    }
}
LADDER_B = {
    MODEL: {
        **LADDER_A[MODEL],
        "steps": [
            {"name": "fb1", "region": "us-east1"},
            {"name": "fb2", "region": "us-central1"},
        ],
    }
}


def batch_time(when):
    # Batch reports nanoseconds.
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + "000000000Z"


def job(
    job_id, region, state="QUEUED", created=PRIMARY_CREATED, ran=False, attempt="1"
):
    events = []
    if ran:
        events.append(
            {
                "description": f"Job state is set from SCHEDULED to RUNNING for job {job_id}"
            }
        )
    return {
        "name": f"projects/proj/locations/{region}/jobs/{job_id}",
        "uid": f"uid-{job_id}",
        "createTime": batch_time(created),
        "labels": {"attempt": attempt},
        "status": {"state": state, "statusEvents": events},
    }


class EnsembleLadderDecisionTest(unittest.TestCase):
    def setUp(self):
        self.module = load_pipeline_state()
        self.storage = FakeStorageClient()
        self.module._storage_client = self.storage
        self.module.GCS_COMMON_BUCKET = COMMON_BUCKET
        self.module.ENSEMBLE_LADDER = json.loads(json.dumps(LADDER_A))
        self.module.REGIONS = {"india": {"models": [MODEL], "stages": []}}
        self.module.REGION_MODELS = {"india": [MODEL]}
        self.jobs = {}
        self.calls = []

        def fake_get(project_id, region, job_id):
            self.calls.append((project_id, region, job_id))
            return self.jobs.get((region, job_id))

        self.module.batch_get_job = fake_get

    def add(self, job_id, region="us-central1", **kwargs):
        self.jobs[(region, job_id)] = job(job_id, region, **kwargs)

    def decide(self, minutes_after_primary):
        now = PRIMARY_CREATED + timedelta(minutes=minutes_after_primary)
        return self.module.ensemble_ladder_decision(MODEL, DATE, now)

    def claim(self, job_id):
        self.storage.put_json(COMMON_BUCKET, WINNER, {"job_id": job_id})

    def test_not_configured_makes_no_batch_calls(self):
        self.module.ENSEMBLE_LADDER = {}
        result = self.decide(30)
        self.assertEqual(
            (result["decision"], result["reason"]), ("none", "not_configured")
        )
        self.assertEqual(self.calls, [])

    def test_job_ids_and_regions(self):
        self.module.ENSEMBLE_LADDER = json.loads(json.dumps(LADDER_B))
        self.decide(1)
        self.assertEqual(
            self.calls,
            [
                ("proj", "us-central1", PRIMARY),
                ("proj", "us-east1", f"{PRIMARY}-fb1"),
                ("proj", "us-central1", f"{PRIMARY}-fb2"),
            ],
        )

    def test_primary_absent_means_nothing_to_do(self):
        result = self.decide(30)
        self.assertEqual(
            (result["decision"], result["reason"]), ("none", "primary_not_submitted")
        )

    def test_fb2_is_due_after_after_minutes(self):
        self.add(PRIMARY, attempt="2")
        early = self.decide(9.9)
        self.assertEqual(
            (early["decision"], early["reason"]), ("none", "waiting_for_fb2")
        )
        due = self.decide(10)
        self.assertEqual(
            (due["decision"], due["step"], due["reason"]), ("submit", "fb2", "stuck")
        )
        self.assertFalse(due["force_claim"])
        self.assertEqual(due["primary_attempt"], "2")  # fb2 inherits it
        self.assertEqual(due["jobs"]["primary"]["waited_minutes"], 10.0)
        self.assertEqual(due["regions"], ["india"])  # FORECAST_REGIONS for fb2

    def test_pr_b_fb1_first_and_fb2_counts_from_fb1(self):
        self.module.ENSEMBLE_LADDER = json.loads(json.dumps(LADDER_B))
        self.add(PRIMARY)
        self.assertEqual(self.decide(10)["step"], "fb1")
        self.add(
            f"{PRIMARY}-fb1",
            region="us-east1",
            created=PRIMARY_CREATED + timedelta(minutes=11),
        )
        waiting = self.decide(20.9)
        self.assertEqual(
            (waiting["decision"], waiting["reason"]), ("none", "waiting_for_fb2")
        )
        due = self.decide(21)
        self.assertEqual((due["decision"], due["step"]), ("submit", "fb2"))
        self.assertTrue(due["ladder_active"])

    def test_started_job_without_claim_waits(self):
        self.add(PRIMARY, state="RUNNING", ran=True)
        result = self.decide(30)
        self.assertEqual(
            (result["decision"], result["reason"]), ("none", "started_awaiting_claim")
        )

    def test_scheduled_job_with_a_running_event_counts_as_started(self):
        self.add(PRIMARY, state="SCHEDULED", ran=True)
        self.assertEqual(self.decide(30)["reason"], "started_awaiting_claim")

    def test_fallback_claim_cancels_the_primary_once_and_logs_won_once(self):
        self.add(PRIMARY)
        self.add(f"{PRIMARY}-fb2", state="RUNNING", ran=True)
        self.claim(f"{PRIMARY}-fb2")
        result = self.decide(20)
        self.assertEqual(result["decision"], "cancel")
        self.assertEqual(
            result["cancel"],
            [
                {
                    "step": "primary",
                    "name": f"projects/proj/locations/us-central1/jobs/{PRIMARY}",
                    "uid": f"uid-{PRIMARY}",
                }
            ],
        )
        self.assertEqual((result["winner_step"], result["log_won"]), ("fb2", True))
        self.storage.put(COMMON_BUCKET, WON_LOGGED, "")
        self.assertFalse(self.decide(20)["log_won"])

    def test_primary_claim_cancels_queued_fallbacks_without_won_log(self):
        self.module.ENSEMBLE_LADDER = json.loads(json.dumps(LADDER_B))
        self.add(PRIMARY, state="RUNNING", ran=True)
        self.add(f"{PRIMARY}-fb1", region="us-east1")
        self.add(f"{PRIMARY}-fb2", state="CANCELLED")
        self.claim(PRIMARY)
        result = self.decide(25)
        self.assertEqual([c["step"] for c in result["cancel"]], ["fb1"])
        self.assertEqual((result["winner_step"], result["log_won"]), ("primary", False))

    def test_finished_winner_and_finished_losers_are_left_alone(self):
        self.add(PRIMARY, state="CANCELLED")
        self.add(f"{PRIMARY}-fb2", state="SUCCEEDED", ran=True)
        self.claim(f"{PRIMARY}-fb2")
        self.storage.put(COMMON_BUCKET, WON_LOGGED, "")
        result = self.decide(60)
        self.assertEqual((result["decision"], result["cancel"]), ("none", []))
        self.assertFalse(result["ladder_active"])

    def test_marker_cancels_unfinished_jobs_even_without_a_claim(self):
        self.add(PRIMARY, state="SUCCEEDED", ran=True)
        self.add(f"{PRIMARY}-fb2")
        self.storage.put(COMMON_BUCKET, MARKER, "done")
        result = self.decide(40)
        self.assertEqual(
            (result["decision"], result["reason"]), ("cancel", "marker_present")
        )
        self.assertEqual([c["step"] for c in result["cancel"]], ["fb2"])

    def test_nothing_queued_leaves_the_retry_to_normal_passes(self):
        self.add(PRIMARY, state="FAILED")
        result = self.decide(30)
        self.assertEqual(
            (result["decision"], result["reason"]), ("none", "no_queued_job")
        )
        self.assertFalse(result["ladder_active"])

    def test_queued_fallback_makes_the_ladder_active(self):
        self.add(PRIMARY)
        self.add(f"{PRIMARY}-fb2")
        result = self.decide(25)
        self.assertTrue(result["ladder_active"])
        self.assertEqual(
            (result["decision"], result["reason"]), ("none", "ladder_exhausted")
        )

    def test_force_submits_the_forced_step_at_once_with_a_claim(self):
        self.module.ENSEMBLE_LADDER[MODEL]["force"] = "fb2"
        self.add(PRIMARY)
        result = self.decide(1)
        self.assertEqual(
            (result["decision"], result["step"], result["force_claim"]),
            ("submit", "fb2", True),
        )
        self.add(f"{PRIMARY}-fb2")
        self.assertEqual(self.decide(2)["reason"], "forced_step_submitted")

    def test_forced_claim_then_cancels_the_primary(self):
        self.module.ENSEMBLE_LADDER[MODEL]["force"] = "fb2"
        self.add(PRIMARY)
        self.add(f"{PRIMARY}-fb2")
        self.claim(f"{PRIMARY}-fb2")
        self.assertEqual([c["step"] for c in self.decide(3)["cancel"]], ["primary"])

    def test_batch_times_with_nanoseconds_parse(self):
        parsed = self.module.parse_batch_time("2026-10-14T07:37:57.810976921Z")
        self.assertEqual(
            parsed, datetime(2026, 10, 14, 7, 37, 57, 810976, tzinfo=timezone.utc)
        )
        self.assertEqual(
            self.module.parse_batch_time("2026-10-14T07:37:57Z"),
            datetime(2026, 10, 14, 7, 37, 57, tzinfo=timezone.utc),
        )
        self.assertIsNone(self.module.parse_batch_time(""))

    def test_state_shows_the_winner(self):
        self.module._ic_date_for_source = lambda *args: DATE
        self.module._missing_ic_paths = lambda *args: []
        self.module.model_region_outputs_present = lambda *args: False
        self.claim(f"{PRIMARY}-fb2")
        state = self.module.compute_state(DATE, 1, PRIMARY_CREATED)
        self.assertEqual(
            state["models"][MODEL]["ladder"],
            {"winner": f"{PRIMARY}-fb2", "steps": ["fb2"]},
        )
        # Still to run until the marker exists, whichever job writes it.
        self.assertEqual(
            state["actions"]["models_to_run_by_model"][MODEL]["date"], DATE
        )


class EnsembleLadderEndpointTest(unittest.TestCase):
    def test_endpoint_requires_model_and_date(self):
        module = load_pipeline_state()
        responses = []
        handler = module.Handler.__new__(module.Handler)
        handler.respond = lambda status, payload: responses.append((status, payload))
        handler.respond_ladder({"model": [MODEL]})
        self.assertEqual(responses[-1][0], 400)
        module.ensemble_ladder_decision = lambda model, date, now: {
            "decision": "none",
            "date": date,
        }
        handler.respond_ladder({"model": [MODEL], "date": [DATE]})
        self.assertEqual(responses[-1], (200, {"decision": "none", "date": DATE}))


if __name__ == "__main__":
    unittest.main()
