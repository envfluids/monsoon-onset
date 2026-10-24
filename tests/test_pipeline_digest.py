import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts/pipeline_digest.py"

DAY = "20261021"
FAKE_KEY = "xai-test-key-do-not-print"


def load_module():
    """Load scripts/pipeline_digest.py as an isolated module under test."""
    module_name = "pipeline_digest_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def event(time, description, kind="STATUS_CHANGED"):
    return {"eventTime": time, "type": kind, "description": description}


# Shaped like the real Oct 21 jobs; events deliberately out of order, as Batch lists them.
PRIMARY = {
    "createTime": "2026-10-21T07:37:06.123456789Z",
    "labels": {"attempt": "1"},
    "status": {
        "state": "CANCELLED",
        "statusEvents": [
            event(
                "2026-10-21T07:37:14Z",
                "Job state is set from QUEUED to SCHEDULED for job x.",
            ),
            event(
                "2026-10-21T08:05:20.5Z",
                "VM in Managed Instance Group meets error: Batch Error: code - "
                "CODE_GCE_ZONE_RESOURCE_POOL_EXHAUSTED, description - error count is 3, "
                "latest message example: Instance 'i' creation failed: The zone "
                "'projects/p/zones/us-central1-b' does not have enough resources.",
                "OPERATIONAL_INFO",
            ),
            event(
                "2026-10-21T07:57:01Z",
                "Job state is set from SCHEDULED to SCHEDULED_PENDING_QUEUED for job x.",
            ),
            event(
                "2026-10-21T07:57:46Z",
                "Job state is set from SCHEDULED_PENDING_QUEUED to QUEUED for job x.",
            ),
            event(
                "2026-10-21T08:08:56Z",
                "Job state is set from CANCELLATION_IN_PROGRESS to CANCELLED for job x.",
            ),
        ],
    },
}
FB2 = {
    "createTime": "2026-10-21T08:00:03Z",
    "labels": {"attempt": "1"},
    "status": {
        "state": "SUCCEEDED",
        "statusEvents": [
            event(
                "2026-10-21T08:46:18Z",
                "Job state is set from RUNNING to SUCCEEDED for job x.",
            ),
            event(
                "2026-10-21T08:06:33Z",
                "Job state is set from SCHEDULED to RUNNING for job x.",
            ),
        ],
    },
}


class FakeGcp:
    """Stands in for run_cli: answers by command shape, records every call."""

    def __init__(self, module, objects=None, jobs=None, billing=None):
        self.module = module
        self.objects = objects or {}  # "bucket/path" -> text
        self.jobs = jobs or {}  # job id -> description
        self.billing = billing  # rows, or a ReadError to raise
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        tool = Path(args[0]).name
        if tool == "bq":
            if isinstance(self.billing, Exception):
                raise self.billing
            return json.dumps(self.billing or [])
        if args[1:4] == ["batch", "jobs", "describe"]:
            job_id = args[4]
            if job_id not in self.jobs:
                raise self.module.ReadError(
                    "not_found", f"Resource '{job_id}' was not found."
                )
            return json.dumps(self.jobs[job_id])
        if args[1:3] == ["storage", "cat"]:
            key = args[3][len("gs://") :]
            if key not in self.objects:
                raise self.module.ReadError(
                    "not_found", "The following URLs matched no objects"
                )
            return self.objects[key]
        if args[1:4] == ["storage", "objects", "list"]:
            bucket, _, pattern = args[4][len("gs://") :].partition("/")
            import fnmatch

            return json.dumps(
                [
                    {"name": key.partition("/")[2], "creation_time": created}
                    for key, created in self.objects.items()
                    if key.startswith(bucket + "/")
                    and fnmatch.fnmatch(key.partition("/")[2], pattern)
                ]
            )
        raise AssertionError(f"unexpected command {args}")


def real_day_objects(m):
    """The common- and india-bucket objects of a normal fb2-won day."""
    c, i = m.COMMON_BUCKET, m.INDIA_BUCKET
    d = f"{DAY}T00"
    objects = {
        f"{c}/batch-state/AIFS_ENS_v2/{d}/winner": json.dumps(
            {
                "job_id": f"aifs-ens-v2-{DAY}-00-fb2",
                "claimed_utc": "2026-10-21T08:06:34.495114+00:00",
            }
        ),
        f"{c}/intermediate/AIFS_ENS_v2_india_{d}_done": "2026-10-21T08:46:08+0000",
        f"{c}/intermediate/drywetcast_india_gefs_reduced_{d}_done": "2026-10-21T08:20:00+0000",
        f"{c}/intermediate/drywetcast_india_gefs_full_{d}_done": "2026-10-21T08:29:30+0000",
        f"{i}/drywetcast-state/{d}/status.json": json.dumps(
            {
                "configs": {
                    "gefs_reduced": {
                        "state": "done",
                        "attempts": 0,
                        "updated_utc": "2026-10-21T08:20:00Z",
                        "message": "",
                    },
                    "gefs_full": {
                        "state": "done",
                        "attempts": 0,
                        "updated_utc": "2026-10-21T08:29:30Z",
                        "message": "",
                    },
                    "ncmrwf_reduced": {
                        "state": "failed",
                        "attempts": 1,
                        "updated_utc": "2026-10-21T09:30:00Z",
                        "message": "inputs: AIFS store gs://monsoon-dev-india-ai-weather-operational/output/x is empty",
                    },
                }
            }
        ),
        f"{i}/output/drywetcast/{d}/manifest.json": json.dumps(
            {
                "drywetcast_commit": "22cdbe5beccd3a776ce9e6039a1f603e3d404604",
                "subdistrict_bundle": {"state": "staged"},
                # Rewritten by the last execution: only its own configs.
                "this_run": {"ncmrwf_reduced": {"state": "failed"}},
            }
        ),
        # Uploaded subdistrict products: gefs_full both, gefs_reduced the .pkl only.
        f"{i}/output/drywetcast/{d}/gefs_full/{DAY}_00_subdistrict.pkl": "2026-10-21T08:29:20+0000",
        f"{i}/output/drywetcast/{d}/gefs_full/{DAY}_00_subdistrict.png": "2026-10-21T08:29:21+0000",
        f"{i}/output/drywetcast/{d}/gefs_reduced/{DAY}_00_subdistrict.pkl": "2026-10-21T08:19:50+0000",
        f"{i}/output/drywetcast/{d}/gefs_reduced/{DAY}_00.png": "2026-10-21T08:19:40+0000",
        f"{i}/sync-items/{d}/drywetcast/gefs_reduced.done.json": "2026-10-21T08:21:00+0000",
        f"{i}/sync-items/{d}/drywetcast/gefs_full.failed.json": "2026-10-21T08:40:00+0000",
    }
    return objects


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.m = load_module()
        self.objects = real_day_objects(self.m)
        # objects list returns creation times; cat returns content. The fake keeps
        # one value per key, so markers/sync items hold their creation time.
        self.fake = FakeGcp(
            self.m,
            objects=self.objects,
            jobs={f"aifs-ens-v2-{DAY}-00": PRIMARY, f"aifs-ens-v2-{DAY}-00-fb2": FB2},
        )
        patcher = mock.patch.object(self.m, "run_cli", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.m.main([*argv, "--out-dir", self.tmp.name])
        return code, out.getvalue()

    # -- ensemble ----------------------------------------------------------

    def test_job_facts_sorts_events_and_reads_stockouts_and_requeues(self):
        facts = self.m.job_facts(PRIMARY)
        self.assertEqual(facts["created"], "2026-10-21T07:37:06Z")
        self.assertIsNone(facts["first_running"])
        self.assertEqual(facts["failed_or_cancelled"], "2026-10-21T08:08:56Z")
        self.assertEqual(facts["requeues"], 1)
        self.assertEqual(
            facts["stockouts"],
            [
                {
                    "time": "2026-10-21T08:05:20Z",
                    "zone": "us-central1-b",
                    "error_count": 3,
                }
            ],
        )
        fb2 = self.m.job_facts(FB2)
        self.assertEqual(fb2["first_running"], "2026-10-21T08:06:33Z")
        self.assertEqual(fb2["succeeded"], "2026-10-21T08:46:18Z")

    def test_ensemble_describes_each_job_by_name_and_maps_the_claim(self):
        result = self.m.gather_ensemble(DAY)
        described = [
            c[4] for c in self.fake.calls if c[1:4] == ["batch", "jobs", "describe"]
        ]
        self.assertEqual(
            described,
            [
                f"aifs-ens-v2-{DAY}-00",
                f"aifs-ens-v2-{DAY}-00-fb1",
                f"aifs-ens-v2-{DAY}-00-fb2",
            ],
        )
        self.assertFalse(
            any("list" in c for c in self.fake.calls if c[1:3] == ["batch", "jobs"])
        )
        self.assertEqual(result["jobs"]["fb1"]["status"], "not found")
        self.assertEqual(result["jobs"]["fb1"]["region"], "us-east1")
        self.assertEqual(result["winner"]["step"], "fb2")
        self.assertFalse(result["winner"]["forced"])

    def test_missing_claim_says_not_found(self):
        del self.objects[
            f"{self.m.COMMON_BUCKET}/batch-state/AIFS_ENS_v2/{DAY}T00/winner"
        ]
        result = self.m.gather_ensemble(DAY)
        self.assertEqual(result["winner"]["status"], "not found")
        self.assertEqual(self.m.claim_cell(result["jobs"]["fb2"]), "claim missing")

    # -- delivery ----------------------------------------------------------

    def test_markers_report_utc_ist_and_minutes_against_0830(self):
        markers = self.m.gather_markers(DAY)["markers"]
        self.assertEqual(markers["ensemble"]["utc"], "2026-10-21T08:46:08Z")
        self.assertEqual(markers["ensemble"]["minutes_vs_0830_utc"], 16.1)
        self.assertEqual(markers["gefs_reduced"]["minutes_vs_0830_utc"], -10.0)
        self.assertEqual(self.m.hm_ist(markers["ensemble"]["utc"]), "14:16:08")
        self.assertEqual(markers["ncmrwf_full"]["status"], "not found")
        self.assertIsNone(markers["ncmrwf_full"]["utc"])
        self.assertNotIn("minutes_vs_0830_utc", markers["ncmrwf_full"])

    # -- drywetcast and sync -----------------------------------------------

    def test_drywetcast_states_errors_and_missing_entries(self):
        dwc = self.m.gather_drywetcast(DAY)
        sub = {c: v["subdistrict"] for c, v in dwc["configs"].items()}
        # Read from the uploaded files, not the manifest (which only lists ncmrwf_reduced).
        self.assertEqual(sub["gefs_full"], {"state": "done", "present": ["pkl", "png"]})
        self.assertEqual(sub["gefs_reduced"], {"state": "partial", "present": ["pkl"]})
        self.assertEqual(sub["ncmrwf_full"], {"state": "missing", "present": []})
        self.assertEqual(
            self.m.subdistrict_cell(sub["gefs_reduced"]),
            "partial (only .pkl; .png missing)",
        )
        self.assertEqual(dwc["configs"]["ncmrwf_full"]["state"], "missing")
        self.assertEqual(dwc["subdistrict_bundle"], "staged")
        self.assertEqual(dwc["drywetcast_commit"], "22cdbe5")
        self.assertEqual(len(dwc["errors"]), 1)
        self.assertIn("ncmrwf_reduced", dwc["errors"][0])

    def test_absent_status_and_manifest_say_not_found(self):
        dwc = self.m.gather_drywetcast("20261001")
        self.assertEqual(
            (dwc["status_file"], dwc["manifest_file"]), ("not found", "not found")
        )
        self.assertTrue(all(c["state"] == "missing" for c in dwc["configs"].values()))

    def test_sync_done_failed_and_not_found(self):
        items = self.m.gather_sync(DAY)["items"]
        self.assertEqual(items["gefs_reduced"]["state"], "done")
        self.assertEqual(items["gefs_full"]["state"], "failed")
        self.assertEqual(items["ncmrwf_full"], {"state": "not found", "utc": None})

    # -- cost --------------------------------------------------------------

    def test_cost_splits_gpu_storage_other(self):
        self.fake.billing = [
            {"category": "gpu", "net": "3.5", "currency": "USD"},
            {"category": "storage", "net": "0.25", "currency": "USD"},
            {"category": "other", "net": "1.0", "currency": "USD"},
        ]
        cost = self.m.gather_cost(DAY)
        self.assertEqual(
            (cost["day_utc"], cost["total"], cost["gpu"]), ("2026-10-20", 4.75, 3.5)
        )
        sql = self.fake.calls[-1][-1]
        self.assertIn("'2026-10-20'", sql)
        self.assertTrue(sql.lstrip().upper().startswith("SELECT"))

    def test_cost_access_denied_is_skipped(self):
        self.fake.billing = self.m.ReadError(
            "denied", "Access Denied: User does not have bigquery.jobs.create"
        )
        code, out = self.run_main("--date", DAY, "--cost")
        self.assertEqual(code, 0)
        self.assertIn("Cost: access denied", out)
        self.assertIn("### Ensemble ladder", out)

    # -- rendering and main ------------------------------------------------

    def test_main_prints_and_writes_the_digest(self):
        code, out = self.run_main("--date", DAY)
        self.assertEqual(code, 0)
        self.assertIn(
            "| fb1 | aifs-ens-v2-20261021-00-fb1 | us-east1 | not found (never queued) |",
            out,
        )
        self.assertIn("us-central1-b ×3 (latest 08:05:20)", out)
        self.assertIn("| Ensemble (India) | 08:46:08 | 14:16:08 | 16.1 min late |", out)
        self.assertIn("Claim: **fb2**", out)
        # A job that isn't the claim holder is never shown as the one that delivered.
        self.assertIn(
            "| primary | aifs-ens-v2-20261021-00 | us-central1 | "
            "CANCELLED (attempt 1) at 08:08:56 | lost / not claimed |",
            out,
        )
        self.assertIn("| SUCCEEDED (attempt 1) | won |", out)
        self.assertIn("Subdistrict bundle (manifest): staged.", out)
        self.assertIn("| gefs_full | done | 0 | 08:29:30 | done |", out)
        self.assertIn(
            "| gefs_reduced | done | 0 | 08:20:00 | partial (only .pkl; .png missing) |",
            out,
        )
        self.assertIn("| ncmrwf_full | missing | missing | missing | missing |", out)
        self.assertNotIn("each execution rewrites", out)
        self.assertEqual((Path(self.tmp.name) / f"{DAY}.md").read_text(), out)

    def test_subdistrict_listing_error_is_shown_not_guessed(self):
        real = self.fake.__call__

        def failing(args):
            if (
                args[1:4] == ["storage", "objects", "list"]
                and "_subdistrict" in args[4]
            ):
                raise self.m.ReadError("denied", "403 Access denied")
            return real(args)

        with mock.patch.object(self.m, "run_cli", failing):
            sub = self.m.subdistrict_products(DAY)
        self.assertEqual(sub["gefs_full"]["state"], "error")
        self.assertIn("error: 403", self.m.subdistrict_cell(sub["gefs_full"]))

    def test_days_table_has_one_row_per_day(self):
        _, out = self.run_main("--date", DAY, "--days", "3")
        rows = [line for line in out.splitlines() if line.startswith("| 202610")]
        self.assertEqual(
            [r.split(" | ")[0] for r in rows],
            ["| 20261021", "| 20261020", "| 20261019"],
        )
        self.assertIn(
            "| 20261021 | primary/fb2 | fb2 | 1 | 08:46:08 | 16.1 min late |", out
        )
        self.assertIn("| 20261020 | none | not found | 0 | not found | missing |", out)

    def test_no_gcp_writes(self):
        self.run_main("--date", DAY, "--days", "2")
        verbs = {tuple(c[1:4]) for c in self.fake.calls}
        allowed = {("batch", "jobs", "describe"), ("storage", "objects", "list")}
        for verb in verbs:
            self.assertTrue(verb in allowed or verb[:2] == ("storage", "cat"), verb)


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.m = load_module()
        self.fake = FakeGcp(
            self.m,
            objects=real_day_objects(self.m),
            jobs={f"aifs-ens-v2-{DAY}-00": PRIMARY, f"aifs-ens-v2-{DAY}-00-fb2": FB2},
        )
        for name, value in (
            ("run_cli", self.fake),
            ("XAI_KEY_FILE", Path("/nonexistent/key")),
        ):
            patcher = mock.patch.object(self.m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict("os.environ", {"XAI_API_KEY": FAKE_KEY})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", err):
            self.m.main(["--date", DAY, "--out-dir", self.tmp.name, *argv])
        return out.getvalue() + err.getvalue()

    def response(self, status=200, body=None):
        resp = mock.Mock(status_code=status)
        resp.json.return_value = body if body is not None else {}
        return resp

    def test_sends_only_sanitized_facts_and_appends_a_labelled_summary(self):
        body = {
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "fb2 delivered late."}],
                },
            ]
        }
        with mock.patch.object(
            self.m.requests, "post", return_value=self.response(body=body)
        ) as post:
            out = self.run_main(
                "--summarize",
                "--xai-model",
                "grok-test",
                "--xai-endpoint",
                "https://example.test/v1/responses",
            )
        (url,), kwargs = post.call_args
        self.assertEqual(url, "https://example.test/v1/responses")
        payload = kwargs["json"]
        self.assertEqual(payload["model"], "grok-test")
        self.assertFalse(payload["store"])
        instructions = payload["input"][0]["content"]
        for rule in (
            "2-4 short",
            "overall verdict",
            "Nothing unusual.",
            "never quote field names, JSON keys",
            "do not add, compute, round or estimate any number",
            "NCMRWF configs have no target",
        ):
            self.assertIn(rule, instructions)
        sent = payload["input"][1]["content"]
        json.loads(sent)
        for leaked in (
            "gs://",
            "monsoon-dev-",
            "/Users/",
            "batch-state/",
            "sync-items/",
            "intermediate/",
        ):
            self.assertNotIn(leaked, sent)
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {FAKE_KEY}")
        self.assertIn("### AI summary (grok-test;", out)
        self.assertIn("fb2 delivered late.", out)
        # The table is still there, unchanged, before the summary.
        self.assertLess(out.index("### Ensemble ladder"), out.index("### AI summary"))
        self.assertNotIn(FAKE_KEY, out)

    def test_failure_still_prints_the_table_with_a_note_and_no_key(self):
        with mock.patch.object(
            self.m.requests, "post", return_value=self.response(status=401)
        ):
            out = self.run_main("--summarize")
        self.assertIn("### Ensemble ladder", out)
        self.assertIn("_AI summary unavailable: HTTP 401._", out)
        self.assertNotIn(FAKE_KEY, out)

    def test_network_error_does_not_echo_the_request(self):
        error = self.m.requests.ConnectionError(
            f"boom Authorization: Bearer {FAKE_KEY}"
        )
        with mock.patch.object(self.m.requests, "post", side_effect=error):
            out = self.run_main("--summarize")
        self.assertIn(
            "_AI summary unavailable: request failed (ConnectionError)._", out
        )
        self.assertNotIn(FAKE_KEY, out)

    def test_missing_key_is_reported_without_a_call(self):
        with (
            mock.patch.dict("os.environ", {"XAI_API_KEY": ""}),
            mock.patch.object(self.m.requests, "post") as post,
        ):
            out = self.run_main("--summarize")
        post.assert_not_called()
        self.assertIn("AI summary unavailable: no xAI key", out)

    def test_without_flag_nothing_is_sent(self):
        with mock.patch.object(self.m.requests, "post") as post:
            self.run_main()
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
