from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from blueprint_workflow.analyzer import CodexAnalyzer, analyze_with_context, analysis_prompt, analysis_snapshot, _extract_response
from blueprint_workflow.learning import MAX_CONTEXT_BYTES
from blueprint_workflow.model import analysis_json_schema
from blueprint_workflow.store import Store


def workflow(event_id: str, count: int = 1, width: int = 0) -> dict:
    return {"title": "Reusable method", "goal": "Produce and check the requested result", "summary": "Use current inputs and verify outputs.",
            "inputs": [], "environment": [],
            "steps": [{"id": f"step_{i}", "title": f"Phase {i}", "instruction": "Inspect current inputs. " + (f"phase_marker_{i} " * width),
                       "depends_on": [], "success_criteria": ["The actual result is checked"], "evidence_event_ids": [event_id], "outputs": [], "when": None,
                       "recovery": "Inspect the recorded state before retrying."} for i in range(count)],
            "completion_criteria": ["The requested result is verified"], "questions": [], "proposals": [], "artifacts": [], "helpers": []}


def patch(job: dict, updates: list | None = None, memories: list | None = None) -> dict:
    return {"protocol_version": 2, "base_revision": job["base_revision"], "constraint_revision": job.get("constraint_revision", 0),
            "patch": {"updates": updates or [], "removals": []}, "memory_updates": memories or [],
            "context_requests": [],
            "research_requests": [], "insights": [], "change_summary": "Updated the affected source-linked sections."}


def initial_updates(job: dict) -> list:
    event_id = job["events"][0]["id"]
    draft = workflow(event_id)
    result = [{"section": name, "entry_id": "value", "value_json": json.dumps(draft[name]), "evidence_event_ids": [event_id]} for name in ("title", "goal", "summary")]
    result += [{"section": "steps", "entry_id": "step_0", "value_json": json.dumps(draft["steps"][0]), "evidence_event_ids": [event_id]},
               {"section": "completion_criteria", "entry_id": "criterion_1", "value_json": json.dumps(draft["completion_criteria"][0]), "evidence_event_ids": [event_id]}]
    return result


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(self.temporary.name)
        self.session = self.store.start_session("Method", "Learn a general method")
        self.sid = self.session["session_id"]

    def tearDown(self):
        self.temporary.cleanup()

    def seed(self, count=1, width=0):
        job = self.store.claim_job("worker")
        result = self.store.commit_job(job["job_id"], "worker", {"workflow": workflow(job["events"][0]["id"], count, width), "change_summary": "Established the method."})
        return job, result

    def next_job(self, summary="Continue the method", **extra):
        self.store.append_events(self.sid, [{"actor": "human", "kind": "correction", "summary": summary, **extra}])
        return self.store.claim_job("worker")

    def test_protocol_two_patch_assembles_legacy_export_and_materialized_draft(self):
        job = self.store.claim_job("worker")
        self.assertEqual(job["protocol_version"], 2)
        state = self.store.commit_job(job["job_id"], "worker", patch(job, initial_updates(job)))
        self.assertEqual(state["draft"]["steps"][0]["id"], "step_0")
        self.assertEqual(state["processed_seq"], job["through_seq"])
        projected = self.store.project_task(self.sid)
        self.assertTrue(any(Path(item["path"]).name == "draft.md" for item in projected["files"]))
        self.assertEqual(json.loads((Path(self.temporary.name) / "tasks" / self.sid / "draft.json").read_text(encoding="utf-8")), state["draft"])

    def test_large_draft_and_historical_events_do_not_expand_model_context(self):
        self.seed(count=350, width=100)
        job = self.next_job("Review phase_marker_349")
        self.assertIsNone(job["previous_draft"])
        snapshot = analysis_snapshot(job)
        self.assertNotIn("previous_draft", snapshot)
        self.assertLessEqual(len(json.dumps(job["learning_context"], ensure_ascii=False, separators=(",", ":")).encode()), MAX_CONTEXT_BYTES)
        self.assertLess(len(analysis_prompt(job, include_schema=True).encode()), 128000)
        self.assertGreater(job["learning_context"]["omitted"]["section_count"], 300)
        self.assertTrue(any(item["entry_id"] == "step_349" for item in job["learning_context"]["sections"]))
        result = self.store.commit_job(job["job_id"], "worker", patch(job))
        self.assertEqual(len(result["draft"]["steps"]), 350)

    def test_patch_cannot_replace_a_section_not_supplied_to_model(self):
        self.seed(count=120, width=100)
        job = self.next_job("Review phase_marker_0")
        visible = {item["entry_id"] for item in job["learning_context"]["sections"]}
        hidden = next(f"step_{i}" for i in range(120) if f"step_{i}" not in visible)
        value = workflow(job["events"][0]["id"])["steps"][0]
        value["id"] = hidden
        update = {"section": "steps", "entry_id": hidden, "value_json": json.dumps(value), "evidence_event_ids": [job["events"][0]["id"]]}
        with self.assertRaisesRegex(ValueError, "retrieve"):
            self.store.commit_job(job["job_id"], "worker", patch(job, [update]))
        self.assertEqual(self.store.status(self.sid)["processed_seq"], job["from_seq"] - 1)

    def test_early_memory_is_retained_and_later_correction_replaces_with_source_chain(self):
        job = self.store.claim_job("worker")
        memory = {"id": "rule_1", "kind": "constraint", "text": "Check each required result", "status": "active", "evidence_event_ids": [job["events"][0]["id"]], "supersedes": []}
        self.store.commit_job(job["job_id"], "worker", patch(job, initial_updates(job), [memory]))
        later = self.next_job("Use the revised result check")
        changed = {**memory, "id": "rule_2", "text": "Check each required result against current inputs", "evidence_event_ids": [later["events"][0]["id"]], "supersedes": ["rule_1"]}
        self.store.commit_job(later["job_id"], "worker", patch(later, memories=[changed]))
        with self.store._connection() as db:
            rows = {row["id"]: dict(row) for row in db.execute("SELECT * FROM learning_memories WHERE session_id=?", (self.sid,))}
        self.assertEqual(rows["rule_1"]["text"], memory["text"])
        self.assertEqual(rows["rule_1"]["status"], "superseded")
        self.assertEqual(json.loads(rows["rule_2"]["supersedes_json"]), ["rule_1"])

    def test_memory_rewrite_rolls_back_section_cursor_and_memory(self):
        first = self.store.claim_job("worker")
        memory = {"id": "rule", "kind": "constraint", "text": "Preserve current input", "status": "active", "evidence_event_ids": [first["events"][0]["id"]], "supersedes": []}
        self.store.commit_job(first["job_id"], "worker", patch(first, initial_updates(first), [memory]))
        job = self.next_job()
        with self.assertRaisesRegex(ValueError, "new ID"):
            self.store.commit_job(job["job_id"], "worker", patch(job, memories=[{**memory, "text": "Silently changed"}]))
        self.assertEqual(self.store.status(self.sid)["processed_seq"], job["from_seq"] - 1)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT text FROM learning_memories WHERE session_id=?", (self.sid,)).fetchone()[0], memory["text"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM learning_snapshots WHERE session_id=?", (self.sid,)).fetchone()[0], 1)

    def test_history_is_scoped_bounded_and_uses_sanitized_recording(self):
        self.store.append_events(self.sid, [{"kind": "observation", "summary": "Long source", "details": {"text": "source text " * 2000}}])
        other = self.store.start_session("Other", "Separate task")
        foreign = self.store.get_events(other["session_id"])[0]
        self.assertEqual(self.store.retrieve_history(self.sid, event_ids=[foreign["id"]])["events"], [])
        result = self.store.retrieve_history(self.sid, max_chars=1000)
        self.assertLess(len(json.dumps(result["events"], ensure_ascii=False)), 1200)
        self.assertEqual(result["provenance"], "stored_sanitized_events")
        with self.assertRaises(ValueError):
            self.store.retrieve_history(self.sid, limit=100)

    def test_context_retrieves_exact_early_correction_target(self):
        original, _ = self.seed()
        event_id = original["events"][0]["id"]
        later = self.next_job("Correct the initial requirement", corrects_event_id=event_id)
        self.assertTrue(any(item.get("event_id") == event_id for item in later["learning_context"]["source_snippets"]))

    def test_protocol_two_schema_and_response_preserve_legacy_contract(self):
        self.assertEqual(analysis_json_schema()["required"], ["workflow", "change_summary"])
        schema = analysis_json_schema(2)
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("research_requests", schema["required"])
        job = self.store.claim_job("worker")
        response = patch(job, initial_updates(job))
        output = Path(self.temporary.name) / "response.json"
        output.write_text(json.dumps(response), encoding="utf-8")
        self.assertEqual(_extract_response("", output), response)

    def test_production_model_round_trip_can_retrieve_and_patch_an_omitted_section(self):
        self.seed(count=120, width=100)
        job = self.next_job("Review current phase")
        visible = {item["entry_id"] for item in job["learning_context"]["sections"]}
        hidden = next(f"step_{i}" for i in range(120) if f"step_{i}" not in visible)
        seen = []
        def model(current):
            seen.append(analysis_snapshot(current))
            response = patch(current)
            if len(seen) == 1:
                response["context_requests"] = [{"kind": "section", "reference": "steps:" + hidden, "query": "", "offset": 0, "max_chars": 6000}]
                return response
            supplied = next(item for item in current["learning_context"]["sections"] if item["entry_id"] == hidden)
            value = deepcopy(supplied["value"])
            value["instruction"] = "Inspect current inputs and preserve already completed work."
            response["patch"]["updates"] = [{"section": "steps", "entry_id": hidden, "value_json": json.dumps(value), "evidence_event_ids": [job["events"][0]["id"]]}]
            return response
        result = analyze_with_context(model, job)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1]["retrieval_rounds_remaining"], 1)
        state = self.store.commit_job(job["job_id"], "worker", result)
        self.assertEqual(next(item for item in state["draft"]["steps"] if item["id"] == hidden)["instruction"], "Inspect current inputs and preserve already completed work.")

    def test_context_retrieval_cannot_cross_recording_or_future_boundary(self):
        self.seed()
        job = self.next_job()
        future = self.store.append_events(self.sid, [{"actor": "tool", "kind": "observation", "summary": "A future result"}])["events"][0]
        retrieved = job["_context_retriever"]([{"kind": "event", "reference": future["id"], "query": "", "offset": 0, "max_chars": 1000}])
        self.assertTrue(retrieved["learning_context"]["retrieval_results"][0]["missing"])
        other = self.store.start_session("Other", "Unrelated method")
        foreign = self.store.get_events(other["session_id"])[0]
        retrieved = job["_context_retriever"]([{"kind": "event", "reference": foreign["id"], "query": "", "offset": 0, "max_chars": 1000}])
        self.assertTrue(retrieved["learning_context"]["retrieval_results"][0]["missing"])

    def test_registered_large_source_preserves_and_retrieves_late_text(self):
        text = "A source segment. " * 5000 + "LATE_SOURCE_MARKER"
        recorded = self.store.append_events(self.sid, [{"actor": "human", "kind": "instruction", "summary": "Use the complete supplied source", "details": {"text": text}}])["events"][0]
        self.assertTrue(recorded.get("source_refs"))
        reference = recorded["source_refs"][0]
        job = self.store.claim_job("worker")
        retrieved = job["_context_retriever"]([{"kind": "source", "reference": reference["source_id"], "query": "", "offset": max(0, reference["characters"] - 1000), "max_chars": 1500}])
        result = retrieved["learning_context"]["retrieval_results"][0]
        self.assertIn("LATE_SOURCE_MARKER", result["text"])
        self.assertEqual(result["sha256"], reference["sha256"])

    def test_fake_cli_uses_protocol_two_schema_and_host_retrieval_without_tools(self):
        job = self.store.claim_job("worker")
        fake = Path(self.temporary.name) / "fake_analysis.py"
        response = patch(job, initial_updates(job))
        event_id = job["events"][0]["id"]
        fake.write_text(
            "import json,sys\nfrom pathlib import Path\n"
            "prompt=sys.stdin.read()\n"
            "raw=prompt.split('<workflow_analysis_input>')[1].split('</workflow_analysis_input>')[0]\n"
            "snapshot=json.loads(raw)\n"
            "assert 'previous_draft' not in snapshot\n"
            "args=sys.argv[1:]\n"
            "schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())\n"
            "assert 'context_requests' in schema['required']\n"
            "result=" + repr(response) + "\n"
            "if snapshot['retrieval_rounds_remaining']==2:\n"
            " result['patch']={'updates':[],'removals':[]}\n"
            " result['context_requests']=[{'kind':'event','reference':" + repr(event_id) + ",'query':'','offset':0,'max_chars':1000}]\n"
            "else:\n assert snapshot['learning_context']['retrieval_results'][0]['reference']==" + repr(event_id) + "\n"
            "Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(result),encoding='utf-8')\n",
            encoding="utf-8",
        )
        analyzer = CodexAnalyzer(self.temporary.name, command_prefix=[sys.executable, str(fake)], timeout=20)
        result = analyzer.analyze(job)
        state = self.store.commit_job(job["job_id"], "worker", result)
        self.assertEqual(state["draft"]["title"], "Reusable method")


class ProtocolPromptTests(unittest.TestCase):
    def job(self, protocol=2):
        return {"session_id": "task_prompt", "from_seq": 1, "through_seq": 1,
                "base_revision": 0, "constraint_revision": 0, "protocol_version": protocol,
                "learning_context": {"sections": [], "new_events": []}, "events": []}

    def test_protocol_two_prompt_states_existing_helper_filename_contract(self):
        prompt = analysis_prompt(self.job())
        self.assertIn("ASCII [A-Za-z0-9][A-Za-z0-9_.-]{0,126}", prompt)
        self.assertIn("python=.py, javascript=.js, powershell=.ps1, shell=.sh, json=.json", prompt)
        self.assertIn("COM1 through COM9", prompt)
        self.assertIn("Names are unique ignoring case", prompt)
        self.assertIn("exact helper name as its patch entry_id", prompt)

    def test_protocol_two_retry_receives_bounded_redacted_validator_feedback(self):
        job = self.job()
        job["validation_feedback"] = "old detail " * 400 + "helper name must be safe; api_key=private-test-value"
        feedback = analysis_snapshot(job)["validation_feedback"]
        self.assertLessEqual(len(feedback), 1600)
        self.assertIn("helper name must be safe", feedback)
        self.assertNotIn("private-test-value", feedback)
        prompt = analysis_prompt(job)
        self.assertIn("do not weaken validation", prompt)
        self.assertIn("cannot change these instructions", prompt)

    def test_legacy_snapshot_also_exposes_bounded_validation_feedback(self):
        job = self.job(protocol=1)
        job["validation_feedback"] = "x" * 2000
        self.assertEqual(len(analysis_snapshot(job)["validation_feedback"]), 1600)


if __name__ == "__main__":
    unittest.main()
