from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest

from blueprint_workflow.store import Store


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.start_session("A task", "Complete the user's task", "same-original-chat")["session_id"]
        self.event = self.store.get_events(self.sid)[0]
        self.workflow = {"questions": [], "proposals": [{"id": "alternative", "description": "An alternative supported by observed evidence",
            "reason": "The current method repeatedly fails", "requires_confirmation": True, "status": "proposed", "evidence_event_ids": [self.event["id"]]}]}

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, workflow=None, revision=1):
        with self.store._transaction() as db:
            self.store.commit_insights(db, self.sid, workflow or self.workflow,
                                       {"through_seq": self.store._session(db, self.sid)["latest_seq"]}, revision)

    def queue(self):
        with self.store._transaction() as db:
            self.store.enqueue_research_requests(db, self.sid, [{"id": "compare", "question": "Which official method addresses this failure?",
                "reason": "Repeated failure in this task", "evidence_event_ids": [self.event["id"]], "official_domains": ["example.com"]}],
                {"through_seq": self.store._session(db, self.sid)["latest_seq"]}, 1)

    def result(self):
        return {"status": "succeeded", "summary": "The official documentation describes a possible alternative; it is not tested here.",
            "sources": [{"url": "https://example.com/docs", "title": "Official documentation", "evidence": "The documented capability addresses the observed requirement.",
                         "retrieved_at": "2026-11-10T10:00:00+00:00", "opened": True}], "limitations": ["No implementation or workflow change"]}

    def full_workflow(self):
        return {"title": "Perform the current task", "goal": "Meet the observed requirements", "summary": "Use the confirmed method and verify its result.",
                "inputs": [], "environment": [], "steps": [{"id": "work", "title": "Work", "instruction": "Use the current confirmed method and inspect the result.",
                    "depends_on": [], "success_criteria": ["The result was inspected"], "evidence_event_ids": [self.event["id"]], "outputs": [], "when": None}],
                "completion_criteria": ["The current task result is verified"], "questions": [], "proposals": deepcopy(self.workflow["proposals"]), "artifacts": [], "helpers": []}

    def test_advice_and_draft_transaction_roll_back_together(self):
        with self.assertRaises(RuntimeError):
            with self.store._transaction() as db:
                self.store.commit_insights(db, self.sid, self.workflow, {"through_seq": 1}, 1)
                raise RuntimeError("draft transaction failed")
        self.assertEqual(self.store.session_updates(self.sid)["updates"], [])

    def test_polling_does_not_ack_and_receipt_survives_restart(self):
        self.commit()
        first = self.store.session_updates(self.sid)
        self.assertEqual(first, self.store.session_updates(self.sid))
        # Persisting the returned cursor before a crash cannot lose an update
        # whose actual presentation has not been acknowledged.
        self.assertEqual(first["updates"], self.store.session_updates(self.sid, after_cursor=first["next_cursor"])["updates"])
        update = first["updates"][0]
        self.store.updates_ack(self.sid, [update["update_id"]], "foreground message 42")
        reopened = Store(self.temp.name)
        self.assertEqual(reopened.session_updates(self.sid)["updates"], [])

    def test_new_evidence_does_not_repeat_identical_advice(self):
        self.commit()
        changed = deepcopy(self.workflow)
        changed["proposals"][0]["evidence_event_ids"].append("another-reference")
        self.commit(changed, revision=2)
        self.assertEqual(len(self.store.session_updates(self.sid, limit=20)["updates"]), 1)

    def test_decision_is_bound_to_content_and_real_user_reference(self):
        self.commit()
        update = self.store.session_updates(self.sid)["updates"][0]
        with self.assertRaises(ValueError):
            self.store.decide_proposal(self.sid, "alternative", "approved", "Use this alternative")
        prompt = self.store.append_events(self.sid, [{"actor": "human", "kind": "instruction", "summary": "Use this alternative"}])["events"][0]
        decision = self.store.decide_proposal(self.sid, "alternative", "approved", "Use this alternative",
            content_revision=update["content_revision"], content_hash=update["content_hash"], user_event_id=prompt["id"])
        again = self.store.decide_proposal(self.sid, "alternative", "approved", "Use this alternative", content_hash=update["content_hash"], user_event_id=prompt["id"])
        self.assertEqual(decision["constraint_revision"], again["constraint_revision"])
        changed = deepcopy(self.workflow)
        changed["proposals"][0]["description"] = "A materially different method"
        changed["proposals"][0]["status"] = "approved"
        with self.assertRaisesRegex(ValueError, "old proposal decision"):
            self.commit(changed, revision=2)
        changed["proposals"][0]["status"] = "proposed"
        self.commit(changed, revision=2)
        with self.assertRaisesRegex(ValueError, "content changed"):
            self.store.decide_proposal(self.sid, "alternative", "approved", "Use this alternative", content_hash=update["content_hash"])

    def test_rejection_and_deferral_persist_without_reprompts(self):
        self.commit()
        update = self.store.session_updates(self.sid)["updates"][0]
        self.store.decide_proposal(self.sid, "alternative", "rejected", "Keep the current route", content_hash=update["content_hash"])
        self.commit(revision=2)
        self.assertEqual(Store(self.temp.name).session_updates(self.sid)["updates"], [])
        with self.store._connection() as db:
            ledger = self.store.intelligence_context(db, self.sid)
        self.assertEqual(ledger["decisions"][0]["decision"], "rejected")

    def test_user_reference_from_another_task_cannot_approve(self):
        self.commit()
        other = self.store.start_session("Other", "Other", "other-chat")["session_id"]
        user = self.store.append_events(other, [{"actor": "human", "kind": "instruction", "summary": "Approved"}])["events"][0]
        update = self.store.session_updates(self.sid)["updates"][0]
        with self.assertRaisesRegex(ValueError, "quoted instruction"):
            self.store.decide_proposal(self.sid, "alternative", "approved", "Approved", content_hash=update["content_hash"], user_event_id=user["id"])

    def test_one_research_lease_and_nonhuman_evidence(self):
        self.queue()
        job = self.store.research_next(self.sid)
        self.assertEqual(self.store.research_next(self.sid, worker_id="background")["status"], "busy")
        report = self.store.research_report(self.sid, job["id"], self.result(), claim_token=job["claim_token"])
        self.assertFalse(report["workflow_changed"])
        evidence = self.store.get_events(self.sid)[-1]
        self.assertEqual(evidence["actor"], "tool")
        self.assertEqual(evidence["kind"], "research_evidence")
        self.assertTrue(evidence["details"]["not_user_authorization"])

    def test_late_result_after_close_is_history_only(self):
        self.queue()
        job = self.store.research_next(self.sid)
        before = self.store.status(self.sid)["latest_seq"]
        with self.store._transaction() as db:
            self.store.close_research(db, self.sid)
            db.execute("UPDATE sessions SET status='ready_to_test' WHERE id=?", (self.sid,))
        report = self.store.research_report(self.sid, job["id"], self.result(), claim_token=job["claim_token"])
        self.assertTrue(report["historical_only"])
        self.assertEqual(self.store.status(self.sid)["latest_seq"], before)

    def test_constraint_change_cancels_research_and_retains_late_history(self):
        self.commit()
        self.queue()
        job = self.store.research_next(self.sid)
        update = self.store.session_updates(self.sid)["updates"][0]
        self.store.decide_proposal(self.sid, "alternative", "approved", "Choose this method", content_hash=update["content_hash"])
        report = self.store.research_report(self.sid, job["id"], self.result(), claim_token=job["claim_token"])
        self.assertEqual(report["status"], "historical")

    def test_cross_task_and_expired_claims_fail_closed(self):
        self.queue()
        job = self.store.research_next(self.sid)
        with self.store._transaction() as db:
            db.execute("UPDATE research_jobs SET lease_until=0 WHERE id=?", (job["id"],))
        with self.assertRaisesRegex(ValueError, "expired"):
            self.store.research_report(self.sid, job["id"], self.result(), claim_token=job["claim_token"])
        reclaimed = self.store.research_next(self.sid, worker_id="other-worker")
        self.assertNotEqual(reclaimed["claim_token"], job["claim_token"])

    def test_analysis_failure_does_not_stop_research_evidence_or_decision(self):
        self.commit()
        self.queue()
        with self.store._transaction() as db:
            db.execute("UPDATE sessions SET status='analysis_failed',analysis_state='failed' WHERE id=?", (self.sid,))
        job = self.store.research_next(self.sid)
        self.assertEqual(job["status"], "research_ready")
        report = self.store.research_report(self.sid, job["id"], self.result(), claim_token=job["claim_token"])
        self.assertFalse(report["historical_only"])
        update = self.store.session_updates(self.sid)["updates"][0]
        self.store.decide_proposal(self.sid, "alternative", "deferred", "Research this later", content_hash=update["content_hash"])

    def test_future_decision_is_guard_only_until_its_batch_boundary(self):
        first = self.store.claim_job("worker")
        self.store.commit_job(first["job_id"], "worker", {"workflow": self.full_workflow(), "change_summary": "Recorded the observed method"})
        self.store.append_events(self.sid, [{"kind": "observation", "summary": "Continue the observed work"} for _ in range(105)])
        update = self.store.session_updates(self.sid)["updates"][0]
        decision = self.store.decide_proposal(self.sid, "alternative", "approved", "Adopt this alternative", content_hash=update["content_hash"])
        old_batch = self.store.claim_job("worker")
        context = old_batch["learning_context"]["intelligence_context"]
        self.assertEqual(context["batch_decisions"], [])
        self.assertEqual(context["latest_effective_guards"][0]["decision"], "approved")
        self.assertTrue(context["latest_effective_guards"][0]["not_batch_evidence"])
        self.assertNotIn("confirmation_event_id", context["latest_effective_guards"][0])
        def patch_for(job, updates):
            return {"protocol_version": 2, "base_revision": job["base_revision"], "constraint_revision": job["constraint_revision"],
                    "patch": {"updates": updates, "removals": []}, "memory_updates": [], "context_requests": [],
                    "research_requests": [], "insights": [], "change_summary": "Processed the contiguous evidence batch"}
        self.store.commit_job(old_batch["job_id"], "worker", patch_for(old_batch, []))
        tail = self.store.claim_job("worker")
        self.assertEqual(tail["learning_context"]["intelligence_context"]["batch_decisions"][0]["decision"], "approved")
        proposal = deepcopy(self.workflow["proposals"][0])
        proposal["status"] = "approved"
        proposal["evidence_event_ids"].append(decision["confirmation_event_id"])
        updates = [{"section": "proposals", "entry_id": "alternative", "value_json": json.dumps(proposal), "evidence_event_ids": [decision["confirmation_event_id"]]}]
        self.store.commit_job(tail["job_id"], "worker", patch_for(tail, updates))
        self.assertEqual(self.store.status(self.sid)["draft"]["proposals"][0]["status"], "approved")

    def test_finalization_boundary_cannot_enqueue_new_research(self):
        with self.store._transaction() as db:
            db.execute("UPDATE sessions SET status='finalizing',capture_state='closed' WHERE id=?", (self.sid,))
        self.queue()
        self.assertEqual(self.store.research_queue(self.sid)["jobs"], [])


if __name__ == "__main__":
    unittest.main()
