"""Service-boundary regression checks with synthetic data and no live models."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blueprint_workflow.service import WorkflowService
from blueprint_workflow.worker import run_once


def analysis(job: dict, *, proposal: bool = False, memory: bool = False, research: bool = False) -> dict:
    evidence = [job["events"][0]["id"]]
    workflow = {
        "title": "Preserve the current method", "goal": "Create and verify the current result",
        "summary": "Use the current inputs and inspect the resulting output.",
        "inputs": [{"name": "input_value", "description": "This execution's supplied value", "type": "string", "required": True, "default_json": None}],
        "environment": [],
        "steps": [{"id": "produce", "title": "Produce the current result", "instruction": "Produce a result from {{input_value}} and inspect it.",
                   "depends_on": [], "success_criteria": ["The result matches the current input"], "evidence_event_ids": evidence,
                   "outputs": ["result_value"], "when": None, "recovery": "Read the existing result before attempting the step again."}],
        "completion_criteria": ["The final result matches this execution's input"],
        "questions": [], "proposals": [], "artifacts": [], "helpers": [],
    }
    if proposal:
        workflow["proposals"] = [{"id": "alternative", "description": "An alternative method supported by recorded evidence", "reason": "The present method may need a change",
                                  "requires_confirmation": True, "status": "proposed", "evidence_event_ids": evidence}]
    result = {"workflow": workflow, "change_summary": "Maintained the current source-linked method."}
    if memory:
        result["memory_updates"] = [{"id": "preserve_input", "kind": "constraint", "text": "Preserve the value supplied for the current execution", "status": "active",
                                     "evidence_event_ids": evidence, "supersedes": []}]
    if research:
        result["research_requests"] = [{"id": "reference_check", "question": "Which documented capability addresses the recorded limitation?",
                                        "reason": "A specific question in the current recording needs reference material", "evidence_event_ids": evidence,
                                        "official_domains": ["example.com"]}]
    return result


class SyntheticAnalyzer:
    timeout = 10

    def __init__(self, **options):
        self.options = options

    def analyze(self, job):
        return analysis(job, **self.options)


class V030ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        # Any accidental autostart becomes a failure, including an indirect
        # wake after a research report or after status inspection.
        self.no_worker = patch("blueprint_workflow.worker.ensure_worker", side_effect=AssertionError("integration test must not start a background worker"))
        self.no_worker.start()
        self.service = WorkflowService(self.temporary.name, autostart=False)
        self.session = self.service.session_start("Synthetic method", "Learn and preserve the current method")
        self.sid = self.session["session_id"]

    def tearDown(self):
        self.no_worker.stop()
        self.temporary.cleanup()

    def test_service_research_report_preserves_nondefault_worker_claim_identity(self):
        self.assertTrue(run_once(self.service.store, SyntheticAnalyzer(research=True), worker_id="analysis-test"))
        self.assertIsNone(self.service.session_status(self.sid)["last_error"])
        claimed = self.service.research_next(self.sid, worker_id="custom-reference-worker", lease_seconds=120)
        self.assertEqual(claimed["status"], "research_ready")
        self.assertEqual(claimed["worker_id"], "custom-reference-worker")
        result = {
            "status": "succeeded", "summary": "Synthetic reference evidence identifies a possible capability; no method has been changed.",
            "sources": [{"url": "https://example.com/docs", "title": "Synthetic reference fixture", "evidence": "A synthetic source supports the reference check.",
                         "retrieved_at": "2026-11-10T10:00:00+00:00", "opened": True}],
            "limitations": ["Synthetic evidence fixture; no network request and no external operation"],
        }
        # The default foreground identity must not be allowed to report on
        # another worker's valid token, even through the service adapter.
        with self.assertRaisesRegex(ValueError, "claim"):
            self.service.research_report(self.sid, claimed["id"], claimed["claim_token"], result)
        report = self.service.research_report(self.sid, claimed["id"], claimed["claim_token"], result, worker_id="custom-reference-worker")
        self.assertEqual(report["status"], "succeeded")
        self.assertFalse(report["workflow_changed"])
        event = self.service.session_events(self.sid)["events"][-1]
        self.assertEqual(event["id"], report["event_id"])
        self.assertEqual(event["actor"], "tool")
        self.assertTrue(event["details"]["not_user_authorization"])
        self.assertEqual(self.service.research_next(self.sid, worker_id="custom-reference-worker")["status"], "idle")

    def test_service_status_exposes_capture_analysis_memory_decisions_and_pending_updates(self):
        run_once(self.service.store, SyntheticAnalyzer(proposal=True, memory=True), worker_id="analysis-test")
        status = self.service.session_status(self.sid, include_draft=True)
        self.assertEqual(status["capture_state"], "recording")
        self.assertEqual(status["analysis_state"], "caught_up")
        self.assertIsInstance(status["capture"], dict)
        self.assertEqual(status["memory_summary"][0]["id"], "preserve_input")
        self.assertIn("latest_effective_guards", status["effective_decisions"])
        self.assertEqual(len(status["updates"]["updates"]), 1)
        self.assertEqual(status["pending_event_count"], 0)
        self.assertIsNotNone(status["last_analysis_success_at"])
        self.assertEqual(status["analysis_lag_seconds"], 0)
        self.assertEqual(Path(status["workspace"]), Path(self.temporary.name).resolve() / "tasks" / self.sid)
        compact = self.service.session_status(self.sid)
        self.assertNotIn("draft", compact)
        self.assertEqual(compact["draft_step_count"], 1)
        self.assertEqual(compact["updates"], status["updates"], "reading status must not acknowledge an update")
        offered = status["updates"]["updates"][0]
        instruction = "Keep the current method and reject that alternative"
        user = self.service.event_append(self.sid, [{"actor": "human", "kind": "instruction", "summary": instruction}])["events"][0]
        self.service.proposal_decide(self.sid, "alternative", "rejected", instruction, content_revision=offered["content_revision"],
                                     content_hash=offered["content_hash"], user_event_id=user["id"])
        current = self.service.session_status(self.sid)
        self.assertEqual(current["effective_decisions"]["decisions"][0]["decision"], "rejected")
        self.assertGreater(current["pending_event_count"], 0)
        self.assertEqual(current["capture_state"], "recording")

    def test_stale_run_once_is_superseded_without_model_failure_or_retry_delay(self):
        service = self.service
        class CorrectedDuringAnalysis:
            timeout = 10
            calls = 0

            def analyze(self, job):
                self.calls += 1
                if self.calls == 1:
                    service.event_append(job["session_id"], [{"actor": "human", "kind": "correction", "summary": "Preserve the latest confirmed requirement"}])
                return analysis(job)
        analyzer = CorrectedDuringAnalysis()
        self.assertTrue(run_once(service.store, analyzer, worker_id="analysis-test", max_attempts=1))
        state = service.session_status(self.sid)
        self.assertEqual(state["retry_attempts"], 0)
        self.assertIsNone(state["last_error"])
        self.assertEqual(state["analysis_state"], "queued")
        self.assertEqual(state["capture_state"], "recording")
        self.assertEqual(state["status"], "recording")
        self.assertEqual(state["processed_seq"], 0)
        self.assertEqual(state["draft_revision"], 0)
        self.assertEqual(state["next_attempt_at"], 0)
        with service.store._connection() as db:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE session_id=?", (self.sid,)).fetchone()[0], "superseded")
        self.assertTrue(run_once(service.store, analyzer, worker_id="analysis-test", max_attempts=1), "stale work must be eligible to catch up immediately")
        caught_up = service.session_status(self.sid)
        self.assertEqual(analyzer.calls, 2)
        self.assertEqual(caught_up["draft_revision"], 1)
        self.assertEqual(caught_up["processed_seq"], caught_up["latest_seq"])
        self.assertIsNone(caught_up["last_error"])

    def test_legacy_workflow_read_export_and_version_pinned_execution_still_work(self):
        self.service.session_finish(self.sid, "The synthetic demonstrated result was checked")
        run_once(self.service.store, SyntheticAnalyzer(), worker_id="analysis-test")
        state = self.service.session_status(self.sid)
        workflow_id = state["workflow_id"]
        saved = self.service.workflow_read(workflow_id, version=1)
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["test_status"], "not_tested")
        exported = json.loads(self.service.workflow_export(workflow_id, version=1, format="json")["content"])
        self.assertEqual(exported["workflow"], saved["workflow"])
        self.assertIn("Preserve the current method", self.service.workflow_export(workflow_id, version=1, format="markdown")["content"])
        run = self.service.workflow_run_start(workflow_id, {"input_value": "new-synthetic-input"}, version=1)
        ready = self.service.run_next(run["id"])
        self.assertEqual(ready["status"], "step_ready")
        self.service.run_report(run["id"], ready["step"]["id"], {"status": "succeeded", "observation": "The synthetic result matches this execution's supplied value",
                                  "outputs": {"result_value": "new-synthetic-input"}, "evidence": [{"description": "Compared the synthetic result to the supplied value"}]})
        self.assertEqual(self.service.run_next(run["id"])["status"], "awaiting_verification")
        verified = self.service.run_verify(run["id"], {"status": "passed", "observation": "The current synthetic input and actual output match",
                            "checks": [{"criterion": "The final result matches this execution's input", "status": "passed", "evidence": [{"description": "Read back this run's result_value and compared it to input_value"}]}]})
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(self.service.run_status(run["id"])["version"], 1)
        final = self.service.workflow_read(workflow_id, version=1)
        self.assertEqual(final["test_status"], "passed_agent_reported")
        self.assertEqual(final["workflow"], saved["workflow"], "run results must not rewrite the immutable workflow")


if __name__ == "__main__":
    unittest.main()
