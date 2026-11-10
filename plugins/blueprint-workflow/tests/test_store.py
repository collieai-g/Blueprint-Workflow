from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from blueprint_workflow.model import analysis_json_schema, validate_workflow
from blueprint_workflow.store import Store


def example_workflow(event_ids: list[str]) -> dict:
    return {
        "title": "Create a checked report", "goal": "Produce a report from this run's input",
        "inputs": [{"name": "quantity", "description": "Quantity to calculate", "type": "integer", "required": True, "default_json": None}],
        "environment": ["Python 3.12"],
        "steps": [{"id": "calculate", "title": "Calculate", "instruction": "Calculate using the current quantity and read the result back.",
                   "depends_on": [], "success_criteria": ["The current result is readable"], "evidence_event_ids": event_ids,
                   "outputs": ["result"], "when": None}],
        "completion_criteria": ["The report matches this run's quantity"],
        "questions": [], "proposals": [], "artifacts": [{"path": None, "description": "Generated report"}],
        "summary": "Read the current input, calculate, and verify the report."
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.session = self.store.start_session("Report", "Make a report", "test-host")
        self.sid = self.session["session_id"]

    def tearDown(self):
        self.temp.cleanup()

    def analysis(self, job: dict, workflow: dict | None = None) -> dict:
        return {"workflow": workflow or example_workflow([event["id"] for event in job["events"]]), "change_summary": "Updated the reusable method from new evidence."}

    def finalized(self):
        self.store.finish_session(self.sid, "The report is complete")
        job = self.store.claim_job("worker")
        status = self.store.commit_job(job["job_id"], "worker", self.analysis(job))
        return self.store.get_workflow(status["workflow_id"])

    def test_source_data_rejected(self):
        with self.assertRaises(ValueError):
            Store(Path(__file__).resolve().parents[1] / "private")

    def test_host_isolation(self):
        self.assertEqual(self.store.find_session_by_host("test-host")["session_id"], self.sid)
        self.assertIsNone(self.store.find_session_by_host("another-host"))
        with self.assertRaises(ValueError):
            self.store.start_session("Other", "Other goal", "test-host")

    def test_event_dedup_and_mismatch(self):
        event = {"client_event_id": "one", "kind": "action", "summary": "Read the input"}
        one = self.store.append_events(self.sid, [event])["events"][0]
        again = self.store.append_events(self.sid, [event])["events"][0]
        self.assertEqual(one, again)
        with self.assertRaises(ValueError):
            self.store.append_events(self.sid, [{**event, "summary": "Different"}])
        self.assertEqual(self.store.status(self.sid)["latest_seq"], 2)

    def test_append_is_atomic(self):
        with self.assertRaises(ValueError):
            self.store.append_events(self.sid, [{"kind": "action", "summary": "First"}, {"kind": "bad"}])
        self.assertEqual(self.store.status(self.sid)["latest_seq"], 1)

    def test_redaction_and_no_media(self):
        saved = self.store.append_events(self.sid, [{"kind": "observation", "summary": "Read a result", "details": {"password": "private", "image": {"type": "image", "data": "sensitive bytes"}}}])["events"][0]
        serialized = json.dumps(saved)
        self.assertNotIn("sensitive bytes", serialized)
        self.assertNotIn("private", serialized)

    def test_pause_and_resume(self):
        self.store.pause_session(self.sid)
        self.assertIsNone(self.store.claim_job("worker"))
        with self.assertRaises(ValueError):
            self.store.append_events(self.sid, [{"kind": "action", "summary": "Work"}])
        self.store.resume_session(self.sid)
        self.assertIsNotNone(self.store.claim_job("worker"))

    def test_cursor_and_draft_commit_together(self):
        job = self.store.claim_job("worker")
        with self.assertRaises(ValueError):
            self.store.commit_job(job["job_id"], "worker", {"workflow": {}, "change_summary": "bad"})
        status = self.store.status(self.sid)
        self.assertEqual((status["processed_seq"], status["draft_revision"]), (0, 0))
        good = self.store.commit_job(job["job_id"], "worker", self.analysis(job))
        self.assertEqual((good["processed_seq"], good["draft_revision"]), (1, 1))
        self.assertIsNone(self.store.claim_job("worker2"))

    def test_single_live_lease_and_expired_recovery(self):
        first = self.store.claim_job("one", lease_seconds=300)
        self.assertIsNone(self.store.claim_job("two"))
        with self.store._transaction() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, first["job_id"]))
        second = self.store.claim_job("two")
        self.assertIsNotNone(second)
        with self.assertRaises(ValueError):
            self.store.commit_job(first["job_id"], "one", self.analysis(first))
        self.store.commit_job(second["job_id"], "two", self.analysis(second))

    def test_heartbeat_ownership(self):
        job = self.store.claim_job("one")
        self.assertFalse(self.store.heartbeat_job(job["job_id"], "two"))
        self.assertTrue(self.store.heartbeat_job(job["job_id"], "one"))

    def test_failure_keeps_cursor_and_explicit_retry(self):
        job = self.store.claim_job("worker")
        failed = self.store.fail_job(job["job_id"], "worker", "Provider unavailable", retry_delay=None)
        self.assertEqual(failed["processed_seq"], 0)
        self.assertEqual(failed["status"], "analysis_failed")
        self.assertEqual(self.store.pending_sessions(), [])
        self.assertIsNone(self.store.claim_job("worker"))
        self.store.retry_analysis(self.sid)
        retried = self.store.claim_job("worker")
        self.assertEqual(retried["from_seq"], 1)

    def test_incremental_jobs_only_new_events(self):
        first = self.store.claim_job("worker")
        draft = example_workflow([first["events"][0]["id"]])
        self.store.commit_job(first["job_id"], "worker", self.analysis(first, draft))
        new = self.store.append_events(self.sid, [{"kind": "correction", "actor": "human", "summary": "Include shipping in the calculation"}])["events"][0]
        second = self.store.claim_job("worker")
        self.assertEqual([event["id"] for event in second["events"]], [new["id"]])
        self.assertEqual(second["previous_draft"]["steps"][0]["id"], "calculate")
        changed = deepcopy(second["previous_draft"])
        changed["steps"][0]["instruction"] = "Include shipping in the current calculation."
        changed["steps"][0]["evidence_event_ids"].append(new["id"])
        status = self.store.commit_job(second["job_id"], "worker", self.analysis(second, changed))
        self.assertIn("shipping", status["draft"]["steps"][0]["instruction"])

    def test_finalization_barrier_immutable_and_idempotent(self):
        final = self.store.finish_session(self.sid, "Complete")
        same = self.store.finish_session(self.sid, "Complete")
        self.assertEqual(final["final_seq"], same["final_seq"])
        with self.assertRaises(ValueError):
            self.store.finish_session(self.sid, "Changed result")
        with self.assertRaises(ValueError):
            self.store.append_events(self.sid, [{"kind": "action", "summary": "Late work"}])
        job = self.store.claim_job("worker")
        status = self.store.commit_job(job["job_id"], "worker", self.analysis(job))
        self.assertEqual(status["status"], "ready_to_test")
        self.assertEqual(status["processed_seq"], final["final_seq"])
        with self.assertRaises(ValueError):
            self.store.resume_session(self.sid)

    def prepare_reviewed_draft(self):
        job = self.store.claim_job("worker")
        state = self.store.commit_job(job["job_id"], "worker", self.analysis(job))
        review = {"draft_revision": state["draft_revision"], "through_seq": state["processed_seq"],
                  "checks": [{"criterion": "The report matches this run's quantity", "evidence": [{"description": "Read back the final report and recomputed its quantity"}]}]}
        return state, review

    def test_reviewed_draft_fast_finish_needs_no_model_job(self):
        before, review = self.prepare_reviewed_draft()
        final = self.store.finish_session(self.sid, "The current report is complete", draft_review=review)
        self.assertEqual(final["status"], "ready_to_test")
        self.assertEqual(final["finalization_mode"], "reviewed_draft")
        self.assertEqual(final["processed_seq"], before["processed_seq"] + 1)
        self.assertEqual(final["processed_seq"], final["final_seq"])
        self.assertEqual(final["draft_revision"], before["draft_revision"] + 1)
        self.assertIsNone(self.store.claim_job("unneeded-worker"))
        self.assertEqual(self.store.pending_sessions(), [])
        workflow = self.store.get_workflow(final["workflow_id"])
        self.assertEqual(workflow["workflow"], before["draft"])
        self.assertEqual(workflow["test_status"], "not_tested")
        self.assertEqual(self.store.finish_session(self.sid, "The current report is complete", draft_review=review)["final_seq"], final["final_seq"])

    def test_reviewed_draft_finish_rejects_stale_cursor_or_revision(self):
        _, review = self.prepare_reviewed_draft()
        with self.assertRaises(ValueError):
            self.store.finish_session(self.sid, "Complete", draft_review={**review, "draft_revision": 999})
        self.store.append_events(self.sid, [{"kind": "correction", "actor": "human", "summary": "Use the latest calculation rule"}])
        with self.assertRaises(ValueError):
            self.store.finish_session(self.sid, "Complete", draft_review=review)
        state = self.store.status(self.sid)
        self.assertEqual(state["status"], "recording")
        self.assertIsNone(state["final_seq"])
        self.assertEqual(state["latest_seq"], 2)

    def test_reviewed_draft_finish_requires_full_meaningful_checks(self):
        _, review = self.prepare_reviewed_draft()
        for checks in ([], [{"criterion": "Invented criterion", "evidence": ["Read result"]}],
                       [{"criterion": "The report matches this run's quantity", "evidence": [{}]}], review["checks"] * 2):
            with self.assertRaises(ValueError):
                self.store.finish_session(self.sid, "Complete", draft_review={**review, "checks": checks})
        self.assertEqual(self.store.status(self.sid)["latest_seq"], 1)

    def test_reviewed_draft_finish_rejects_blocking_questions(self):
        job = self.store.claim_job("worker")
        workflow = example_workflow([job["events"][0]["id"]])
        workflow["questions"] = [{"id": "missing", "question": "Which required cost rule applies?", "blocking": True,
                                  "evidence_event_ids": [job["events"][0]["id"]]}]
        state = self.store.commit_job(job["job_id"], "worker", self.analysis(job, workflow))
        review = {"draft_revision": state["draft_revision"], "through_seq": state["processed_seq"],
                  "checks": [{"criterion": workflow["completion_criteria"][0], "evidence": ["Read result"]}]}
        with self.assertRaises(ValueError):
            self.store.finish_session(self.sid, "Complete", draft_review=review)
        self.assertEqual(self.store.status(self.sid)["status"], "recording")

    def test_fast_finish_and_concurrent_append_cannot_both_win(self):
        _, review = self.prepare_reviewed_draft()
        barrier = threading.Barrier(2)
        outcomes = []
        def finish():
            barrier.wait()
            try:
                self.store.finish_session(self.sid, "Complete", draft_review=review)
                outcomes.append("finish")
            except ValueError:
                outcomes.append("finish_rejected")
        def append():
            barrier.wait()
            try:
                self.store.append_events(self.sid, [{"kind": "correction", "summary": "A last change"}])
                outcomes.append("append")
            except ValueError:
                outcomes.append("append_rejected")
        threads = [threading.Thread(target=finish), threading.Thread(target=append)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertIn(set(outcomes), ({"finish", "append_rejected"}, {"append", "finish_rejected"}))

    def test_large_event_preserves_source_and_invalid_batch_remains_atomic(self):
        oversized = {"kind": "observation", "summary": "A huge structured tool result", "details": {"parts": ["semantic text. " * 2000] * 5}}
        with self.assertRaises(ValueError):
            self.store.append_events(self.sid, [oversized, {"kind": "invalid"}])
        self.assertEqual(self.store.status(self.sid)["latest_seq"], 1)
        saved = self.store.append_events(self.sid, [oversized])['events'][0]
        self.assertTrue(saved['source_refs'])
        reference = saved['source_refs'][0]
        self.assertGreater(reference['characters'], 128 * 1024)
        recovered = self.store.source_read(self.sid, reference['source_id'], offset=reference['characters']-1000, limit=1000)
        self.assertIn('semantic text.', recovered['text'])

    def test_finalizing_flag_only_for_batch_containing_marker(self):
        self.store.append_events(self.sid, [{"kind": "observation", "summary": f"Work phase {i}"} for i in range(105)])
        final = self.store.finish_session(self.sid, "Finished all phases")
        first = self.store.claim_job("worker")
        self.assertLess(first["through_seq"], final["final_seq"])
        self.assertFalse(first["finalizing"])
        self.assertFalse(first["at_final_boundary"])
        self.store.commit_job(first["job_id"], "worker", self.analysis(first))
        tail = self.store.claim_job("worker")
        self.assertTrue(tail["finalizing"])
        self.assertEqual(tail["through_seq"], final["final_seq"])

    def test_nonfinal_delta_does_not_decode_historic_payloads(self):
        self.store.append_events(self.sid, [{"kind": "observation", "summary": f"Recorded phase {i}"} for i in range(100)])
        job = self.store.claim_job("worker")
        import blueprint_workflow.store as store_module
        decode = store_module._load
        with patch.object(store_module, "_load", wraps=decode) as observed:
            self.store.commit_job(job["job_id"], "worker", self.analysis(job))
        self.assertLess(observed.call_count, 10, "ordinary delta commits must not parse historical event payloads")

    def test_blocked_finalization_can_resume_to_clarify(self):
        self.store.finish_session(self.sid, "Complete")
        job = self.store.claim_job("worker")
        workflow = example_workflow([job["events"][0]["id"]])
        workflow["questions"] = [{"id": "q", "question": "Which cost rule?", "blocking": True, "evidence_event_ids": [job["events"][0]["id"]]}]
        status = self.store.commit_job(job["job_id"], "worker", self.analysis(job, workflow))
        self.assertEqual(status["status"], "blocked")
        with self.assertRaises(KeyError):
            self.store.get_workflow(status["workflow_id"])
        self.store.resume_session(self.sid)
        self.assertIsNone(self.store.status(self.sid)["final_seq"])
        self.store.append_events(self.sid, [{"kind": "clarification", "actor": "human", "summary": "Use total cost"}])

    def test_finish_during_inflight_analysis_waits_for_final_event(self):
        first = self.store.claim_job("worker")
        self.store.pause_session(self.sid)
        final = self.store.finish_session(self.sid, "Read back the finished report")
        intermediate = self.store.commit_job(first["job_id"], "worker", self.analysis(first))
        self.assertEqual(intermediate["status"], "finalizing")
        self.assertLess(intermediate["processed_seq"], final["final_seq"])
        with self.assertRaises(KeyError):
            self.store.get_workflow(intermediate["workflow_id"])
        tail = self.store.claim_job("worker")
        self.assertEqual(tail["through_seq"], final["final_seq"])
        confirmed = self.store.commit_job(tail["job_id"], "worker", self.analysis(tail))
        self.assertEqual(confirmed["status"], "ready_to_test")

    def test_out_of_session_evidence_rejected(self):
        job = self.store.claim_job("worker")
        with self.assertRaises(ValueError):
            self.store.commit_job(job["job_id"], "worker", self.analysis(job, example_workflow(["invented-event"])))

    def test_analyzer_cannot_approve_its_own_proposal(self):
        job = self.store.claim_job("worker")
        workflow = example_workflow([job["events"][0]["id"]])
        workflow["proposals"] = [{"id": "switch", "description": "Change application", "reason": "Faster", "requires_confirmation": True,
                                   "status": "approved", "evidence_event_ids": [job["events"][0]["id"]]}]
        with self.assertRaises(ValueError):
            self.store.commit_job(job["job_id"], "worker", self.analysis(job, workflow))
        confirmation = self.store.append_events(self.sid, [{"kind": "confirmation", "actor": "human", "summary": "I approve the application change",
            "details": {"proposal_id": "switch", "decision": "approved", "source": "explicit_user_confirmation"}}])["events"][0]
        # The old leased snapshot cannot consume a later confirmation.
        with self.assertRaises(ValueError):
            self.store.commit_job(job["job_id"], "worker", self.analysis(job, workflow))
        self.store.fail_job(job["job_id"], "worker", "Need the new confirmation", retry_delay=0)
        later = self.store.claim_job("worker")
        workflow["proposals"][0]["evidence_event_ids"].append(confirmation["id"])
        self.store.commit_job(later["job_id"], "worker", self.analysis(later, workflow))

    def test_revisions_do_not_overwrite_confirmed_version(self):
        one = self.finalized()
        new = self.store.revise_session(one["workflow_id"], host_session_id="test-host")
        self.assertEqual(new["workflow_version"], 2)
        self.store.finish_session(new["session_id"], "The revised report is complete")
        job = self.store.claim_job("worker")
        changed = example_workflow([job["events"][0]["id"]])
        changed["summary"] = "A revised method"
        self.store.commit_job(job["job_id"], "worker", self.analysis(job, changed))
        self.assertEqual(self.store.get_workflow(one["workflow_id"], 1)["source_hash"], one["source_hash"])
        self.assertEqual(self.store.get_workflow(one["workflow_id"])["version"], 2)

    def test_run_requires_claim_outputs_and_final_verification(self):
        workflow = self.finalized()
        run = self.store.run_start(workflow["workflow_id"], {"quantity": 7})
        result = {"status": "succeeded", "observation": "Read back a result of 14", "evidence": [{"description": "The output contains 14"}], "outputs": {"result": 14}}
        with self.assertRaises(ValueError):
            self.store.run_report(run["id"], "calculate", result)
        ready = self.store.run_next(run["id"])
        self.assertEqual(ready["status"], "step_ready")
        self.assertEqual(self.store.run_next(run["id"])["status"], "awaiting_report")
        with self.assertRaises(ValueError):
            self.store.run_report(run["id"], "calculate", {**result, "outputs": {}})
        self.store.run_report(run["id"], "calculate", result)
        self.store.run_report(run["id"], "calculate", result)
        self.assertEqual(self.store.run_next(run["id"])["status"], "awaiting_verification")
        self.assertEqual(self.store.get_workflow(workflow["workflow_id"])["test_status"], "not_tested")
        with self.assertRaises(ValueError):
            self.store.run_verify(run["id"], {"status": "passed", "observation": "Looks fine", "checks": []})
        checks = [{"criterion": "The report matches this run's quantity", "status": "passed", "evidence": [{"description": "Recomputed 7*2=14 and compared the output"}]}]
        verified = self.store.run_verify(run["id"], {"status": "passed", "observation": "Recomputed and compared the report", "checks": checks})
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(self.store.get_workflow(workflow["workflow_id"])["test_status"], "passed_agent_reported")

    def test_unknown_result_blocks_and_retry_is_explicit(self):
        workflow = self.finalized()
        run = self.store.run_start(workflow["workflow_id"], {"quantity": 7})
        self.store.run_next(run["id"])
        self.store.run_report(run["id"], "calculate", {"status": "unknown", "observation": "The response was lost", "outputs": {}, "evidence": []})
        self.assertEqual(self.store.run_next(run["id"])["status"], "blocked")
        self.assertEqual(self.store.run_retry(run["id"], "calculate")["steps"]["calculate"]["attempt"], 2)

    def test_failed_and_unknown_verification_are_visible(self):
        workflow = self.finalized()
        for status, expected in (("failed", "failed_agent_reported"), ("unknown", "unknown_agent_reported")):
            run = self.store.run_start(workflow["workflow_id"], {"quantity": 7})
            self.store.run_next(run["id"])
            self.store.run_report(run["id"], "calculate", {"status": "succeeded", "observation": "Calculated a value", "evidence": ["Read the current output"], "outputs": {"result": 14}})
            self.store.run_next(run["id"])
            self.store.run_verify(run["id"], {"status": status, "observation": "Final comparison did not pass", "checks": []})
            record = self.store.get_workflow(workflow["workflow_id"])
            self.assertEqual(record["test_status"], expected)
            self.assertEqual(record["latest_run"]["run_id"], run["id"])

    def test_inputs_are_typed_and_not_example_values(self):
        workflow = self.finalized()
        for invalid in ({}, {"quantity": True}, {"quantity": "7"}, {"quantity": 7, "extra": 1}):
            with self.assertRaises(ValueError):
                self.store.run_start(workflow["workflow_id"], invalid)
        self.assertEqual(self.store.run_start(workflow["workflow_id"], {"quantity": 11})["inputs"]["quantity"], 11)

    def test_exports(self):
        workflow = self.finalized()
        self.assertIn("固定步驟", self.store.workflow_export(workflow["workflow_id"], format="markdown"))
        self.assertEqual(json.loads(self.store.workflow_export(workflow["workflow_id"]))["version"], 1)

    def test_cycle_and_invented_condition_output_rejected(self):
        workflow = example_workflow(["event"])
        second = deepcopy(workflow["steps"][0])
        second["id"] = "report"
        second["depends_on"] = ["calculate"]
        workflow["steps"].append(second)
        workflow["steps"][0]["depends_on"] = ["report"]
        with self.assertRaises(ValueError):
            validate_workflow(workflow)
        workflow["steps"][0]["depends_on"] = []
        second["when"] = {"input": None, "step": "calculate", "output": "invented", "equals": True}
        with self.assertRaises(ValueError):
            validate_workflow(workflow)

    def test_strict_schema_objects_have_no_open_properties(self):
        def walk(value):
            if isinstance(value, dict):
                if "properties" in value:
                    self.assertIs(value["additionalProperties"], False)
                    self.assertEqual(set(value["properties"]), set(value["required"]))
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(analysis_json_schema())

    def test_helpers_optional_and_validated_without_execution(self):
        workflow = example_workflow(["event"])
        self.assertEqual(validate_workflow(workflow, evidence_ids={"event"})["helpers"], [])
        helper = {"name": "calculate.py", "language": "python", "description": "Calculate a report value",
                  "content": "raise RuntimeError('must not execute while validating')\n", "evidence_event_ids": ["event"]}
        workflow["helpers"] = [helper]
        normalized = validate_workflow(workflow, evidence_ids={"event"})
        self.assertEqual(normalized["helpers"][0]["content"], helper["content"])
        self.assertIn("calculate.py", __import__("blueprint_workflow.model", fromlist=["render_markdown"]).render_markdown(normalized))

    def test_helper_paths_language_and_duplicates_rejected(self):
        helper = {"name": "calculate.py", "language": "python", "description": "Calculate", "content": "value = 1\n", "evidence_event_ids": ["event"]}
        for unsafe in ("../calculate.py", "C:\\calculate.py", "/tmp/calculate.py", "a/b.py", "a\\b.py", "CON.py", "calc..py", "calculate.js"):
            workflow = example_workflow(["event"])
            workflow["helpers"] = [{**helper, "name": unsafe}]
            with self.assertRaises(ValueError, msg=unsafe):
                validate_workflow(workflow)
        workflow = example_workflow(["event"])
        workflow["helpers"] = [helper, {**helper, "name": "CALCULATE.py"}]
        with self.assertRaises(ValueError):
            validate_workflow(workflow)

    def test_helper_evidence_and_python_syntax_rejected(self):
        helper = {"name": "calculate.py", "language": "python", "description": "Calculate", "content": "value = 1\n", "evidence_event_ids": ["invented"]}
        workflow = example_workflow(["event"])
        workflow["helpers"] = [helper]
        with self.assertRaises(ValueError):
            validate_workflow(workflow, evidence_ids={"event"})
        for invalid_code in ("def broken(:\n", "return 1\n", "break\n"):
            workflow["helpers"] = [{**helper, "content": invalid_code, "evidence_event_ids": ["event"]}]
            with self.assertRaises(ValueError):
                validate_workflow(workflow, evidence_ids={"event"})

    def test_analysis_profile_is_persisted_in_job_and_revision(self):
        session = self.store.start_session("Another host", "Make a second report", "claude:session-a",
                                           analysis_provider="claude-code", analysis_model="claude-model")
        self.store.pause_session(self.sid)
        job = self.store.claim_job("worker")
        self.assertEqual((job["analysis_provider"], job["analysis_model"]), ("claude-code", "claude-model"))
        self.store.commit_job(job["job_id"], "worker", self.analysis(job))
        reopened = Store(self.temp.name)
        self.assertEqual(reopened.status(session["session_id"])["analysis_provider"], "claude-code")
        with reopened._connection() as db:
            revision = db.execute("SELECT analysis_provider,analysis_model FROM revisions WHERE session_id=?", (session["session_id"],)).fetchone()
        self.assertEqual(tuple(revision), ("claude-code", "claude-model"))
        self.assertEqual(self.store.status(self.sid)["analysis_provider"], "codex")

    def test_provider_configuration_after_failure_preserves_committed_draft(self):
        self.prepare_reviewed_draft()
        before = self.store.status(self.sid)
        self.store.append_events(self.sid, [{"kind": "correction", "summary": "Include the latest input"}])
        job = self.store.claim_job("worker")
        self.store.fail_job(job["job_id"], "worker", "Selected engine unavailable", retry_delay=None)
        failed = self.store.status(self.sid)
        changed = self.store.set_analysis_provider(self.sid, "claude-code", "claude-model")
        self.assertEqual(changed["status"], "recording")
        for field in ("draft", "draft_revision", "processed_seq", "final_seq", "latest_seq"):
            self.assertEqual(changed[field], failed[field])
        self.assertEqual(changed["draft"], before["draft"])
        self.assertEqual(changed["retry_attempts"], 0)
        self.assertIsNone(changed["last_error"])
        retry = self.store.claim_job("worker2")
        self.assertEqual((retry["analysis_provider"], retry["analysis_model"]), ("claude-code", "claude-model"))
        with self.store._connection() as db:
            audit = db.execute("SELECT from_provider,to_provider FROM provider_changes WHERE session_id=?", (self.sid,)).fetchone()
        self.assertEqual(tuple(audit), ("codex", "claude-code"))

    def test_provider_configuration_rejects_live_lease_even_when_paused(self):
        job = self.store.claim_job("worker")
        self.store.pause_session(self.sid)
        with self.assertRaisesRegex(ValueError, "still running"):
            self.store.set_analysis_provider(self.sid, "gemini-cli")
        self.store.commit_job(job["job_id"], "worker", self.analysis(job))
        changed = self.store.set_analysis_provider(self.sid, "gemini-cli")
        self.assertEqual(changed["status"], "paused")
        self.assertEqual(changed["analysis_provider"], "gemini-cli")

    def test_provider_change_preserves_finalization_barrier_and_other_host(self):
        self.prepare_reviewed_draft()
        final = self.store.finish_session(self.sid, "Report finished")
        job = self.store.claim_job("worker")
        self.store.fail_job(job["job_id"], "worker", "Selected engine unavailable", retry_delay=None)
        other = self.store.start_session("Other task", "Other goal", "gemini:separate", analysis_provider="gemini-cli")
        self.store.pause_session(other["session_id"])
        changed = self.store.set_analysis_provider(self.sid, "claude-code")
        self.assertEqual(changed["status"], "finalizing")
        self.assertEqual(changed["final_seq"], final["final_seq"])
        self.assertEqual(changed["latest_seq"], final["latest_seq"])
        self.assertEqual(self.store.status(other["session_id"])["analysis_provider"], "gemini-cli")
        job = self.store.claim_job("new-engine")
        self.assertEqual(job["analysis_provider"], "claude-code")
        self.store.commit_job(job["job_id"], "new-engine", self.analysis(job))
        with self.assertRaises(ValueError):
            self.store.set_analysis_provider(self.sid, "codex")

    def test_revision_inherits_or_explicitly_changes_analysis_profile(self):
        self.store.set_analysis_provider(self.sid, "claude-code", "claude-model")
        workflow = self.finalized()
        inherited = self.store.revise_session(workflow["workflow_id"])
        self.assertEqual((inherited["analysis_provider"], inherited["analysis_model"]), ("claude-code", "claude-model"))
        changed = self.store.revise_session(workflow["workflow_id"], analysis_provider="gemini-cli")
        self.assertEqual((changed["analysis_provider"], changed["analysis_model"]), ("gemini-cli", None))
        cleared = self.store.revise_session(workflow["workflow_id"], analysis_model=None)
        self.assertEqual((cleared["analysis_provider"], cleared["analysis_model"]), ("claude-code", None))

    def test_provider_identifier_cannot_be_a_command(self):
        for provider in ("claude; echo leak", "../codex", "CODEx", "", "$(command)"):
            with self.assertRaises(ValueError):
                self.store.set_analysis_provider(self.sid, provider)
        with self.assertRaises(ValueError):
            self.store.set_analysis_provider(self.sid, "codex", "sk-proj-" + "a" * 30)

    def test_two_hosts_route_independently_with_one_worker_and_after_restart(self):
        from blueprint_workflow.worker import run_worker
        self.store.set_analysis_provider(self.sid, "codex", "codex-model")
        other = self.store.start_session("Claude task", "Another report", "claude:host", analysis_provider="claude-code", analysis_model="claude-model")
        selected = []
        received = []
        class FakeProvider:
            timeout = 20
            def __init__(self, provider, model):
                self.provider, self.model = provider, model
            def analyze(self, job):
                received.append((job["session_id"], self.provider, self.model, job["analysis_provider"], job["analysis_model"]))
                return {"workflow": example_workflow([event["id"] for event in job["events"]]), "change_summary": "Processed the selected host's delta"}
        def factory(data_dir, provider="codex", model=None, timeout=240):
            selected.append((provider, model, timeout))
            return FakeProvider(provider, model)
        with patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_ANALYSIS_PROVIDER": "gemini-cli", "BLUEPRINT_WORKFLOW_PROVIDER": "cursor"}):
            result = run_worker(self.temp.name, provider_factory=factory, max_jobs=2, debounce=0, max_batch_wait=0, analysis_timeout=30)
        self.assertEqual(result["jobs_processed"], 2)
        by_session = {row[0]: row[1:] for row in received}
        self.assertEqual(by_session[self.sid], ("codex", "codex-model", "codex", "codex-model"))
        self.assertEqual(by_session[other["session_id"]], ("claude-code", "claude-model", "claude-code", "claude-model"))
        self.store.append_events(other["session_id"], [{"kind": "correction", "summary": "Use the new input"}])
        run_worker(self.temp.name, provider_factory=factory, once=True, analysis_timeout=30)
        self.assertEqual(received[-1][1:3], ("claude-code", "claude-model"))
        self.assertEqual(selected[-1], ("claude-code", "claude-model", 30))

    def test_unavailable_selected_provider_never_falls_back(self):
        from blueprint_workflow.worker import run_once
        self.store.set_analysis_provider(self.sid, "claude-code", "claude-model")
        selected = []
        def unavailable(data_dir, provider="codex", model=None, timeout=240):
            selected.append((provider, model))
            raise RuntimeError("The explicitly selected provider is unavailable")
        self.assertTrue(run_once(self.store, provider_factory=unavailable, max_attempts=1))
        state = self.store.status(self.sid)
        self.assertEqual(state["status"], "analysis_failed")
        self.assertEqual(state["processed_seq"], 0)
        self.assertEqual(selected, [("claude-code", "claude-model")])

    def test_shared_worker_launch_drops_first_hosts_selection_defaults(self):
        from blueprint_workflow.worker import ensure_worker
        with patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_HOST": "claude-code", "BLUEPRINT_WORKFLOW_PROVIDER": "claude-code",
                                     "BLUEPRINT_WORKFLOW_MODEL": "claude-model", "BLUEPRINT_WORKFLOW_AUTOSTART": "1",
                                     "BLUEPRINT_WORKFLOW_ANALYZER": "0"}), patch("blueprint_workflow.worker.subprocess.Popen") as popen:
            popen.return_value.pid = os.getpid()
            ensure_worker(self.temp.name)
        environment = popen.call_args.kwargs["env"]
        for key in ("BLUEPRINT_WORKFLOW_HOST", "BLUEPRINT_WORKFLOW_PROVIDER", "BLUEPRINT_WORKFLOW_MODEL"):
            self.assertNotIn(key, environment)

    def test_legacy_provider_migration_backs_up_and_preserves_every_record(self):
        workflow = self.finalized()
        run = self.store.run_start(workflow["workflow_id"], {"quantity": 7})
        self.store.run_next(run["id"])
        pending = self.store.start_session("Legacy pending task", "Keep pending work", "old-host")
        leased = self.store.claim_job("old-worker")
        with self.store._transaction() as db:
            before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("sessions", "events", "jobs", "revisions", "workflows", "runs")}
            for table in ("sessions", "jobs", "revisions"):
                db.execute(f"ALTER TABLE {table} DROP COLUMN analysis_provider")
                db.execute(f"ALTER TABLE {table} DROP COLUMN analysis_model")
        self.assertFalse((Path(self.temp.name) / "backups").exists())
        upgraded = Store(self.temp.name)
        self.assertIsNotNone(upgraded.migration_backup)
        backup = Path(upgraded.migration_backup)
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(backup)) as old:
            self.assertNotIn("analysis_provider", {row[1] for row in old.execute("PRAGMA table_info(sessions)")})
            self.assertEqual(old.execute("SELECT COUNT(*) FROM runs").fetchone()[0], before["runs"])
        with upgraded._connection() as db:
            after = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
            job_profile = db.execute("SELECT analysis_provider,analysis_model FROM jobs WHERE id=?", (leased["job_id"],)).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(tuple(job_profile), ("codex", None))
        self.assertEqual(upgraded.get_workflow(workflow["workflow_id"])["source_hash"], workflow["source_hash"])
        self.assertEqual(upgraded.run_status(run["id"])["steps"]["calculate"]["status"], "claimed")
        self.assertEqual(upgraded.status(pending["session_id"])["analysis_provider"], "codex")
        self.assertIsNone(Store(self.temp.name).migration_backup)
        self.assertEqual(len(list(backup.parent.glob("*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()
