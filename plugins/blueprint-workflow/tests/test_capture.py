from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from blueprint_workflow import capture
from blueprint_workflow.hooks import handle_hook
from blueprint_workflow.store import Store


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="blueprint-capture-")
        self.store = Store(self.temp.name)
        self.sid = self.store.start_session("Long task", "Learn while working", "capture-host")["session_id"]

    def tearDown(self):
        self.temp.cleanup()

    def event(self, identity="one", **extra):
        return {"kind": "observation", "summary": "Observed current result", "client_event_id": identity, **extra}

    def test_receipt_is_durable_before_analysis_and_survives_restart(self):
        receipt = self.store.capture_enqueue(self.sid, [self.event()])
        self.assertTrue(receipt["durable"])
        self.assertEqual(receipt["queued_count"], 1)
        restarted = Store(self.temp.name)
        self.assertEqual(restarted.capture_status(self.sid)["queued_count"], 1)
        self.assertEqual(restarted.drain_capture(self.sid)["drained_count"], 1)
        self.assertEqual(restarted.capture_status(self.sid)["queued_count"], 0)
        self.assertEqual(len(restarted.get_events(self.sid)), 2)

    def test_capture_does_not_wait_for_main_database_writer(self):
        db = sqlite3.connect(self.store.db_path, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            receipt = self.store.capture_enqueue(self.sid, [self.event()])
            self.assertTrue(receipt["durable"])
        finally:
            db.rollback()
            db.close()
        self.assertTrue(self.store.drain_capture(self.sid)["complete"])

    def test_capture_can_enqueue_while_consumer_waits_for_database_writer(self):
        self.store.capture_enqueue(self.sid, [self.event("before-writer")])
        db = sqlite3.connect(self.store.db_path, isolation_level=None)
        began, errors = threading.Event(), []
        transaction = self.store._transaction
        @contextmanager
        def observed_transaction():
            if threading.current_thread().name == "blocked-drain":
                began.set()
            with transaction() as connection:
                yield connection
        def drain():
            try:
                self.store.drain_capture(self.sid)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=drain, name="blocked-drain")
        try:
            db.execute("BEGIN IMMEDIATE")
            with patch.object(self.store, "_transaction", observed_transaction):
                thread.start()
                self.assertTrue(began.wait(2))
                receipt = self.store.capture_enqueue(self.sid, [self.event("while-draining")])
                self.assertTrue(receipt["durable"])
                db.rollback()
                thread.join(10)
        finally:
            db.rollback()
            db.close()
            thread.join(10)
        self.assertFalse(errors)
        self.assertFalse(thread.is_alive())
        self.store.drain_capture(self.sid)
        self.assertEqual(len(self.store.get_events(self.sid)), 3)

    def test_duplicate_receipt_and_conflicting_content(self):
        one = self.store.capture_enqueue(self.sid, [self.event()])
        again = self.store.capture_enqueue(self.sid, [self.event()])
        self.assertEqual(one["receipts"][0]["receipt_id"], again["receipts"][0]["receipt_id"])
        with self.assertRaises(ValueError):
            self.store.capture_enqueue(self.sid, [self.event(summary="Changed result")])
        self.store.drain_capture(self.sid)
        self.assertEqual(len(self.store.get_events(self.sid)), 2)
        delivered = self.store.capture_enqueue(self.sid, [self.event()])
        self.assertEqual(delivered["receipts"][0]["status"], "drained")
        self.assertEqual(self.store.capture_status(self.sid)["received_count"], 1)
        self.assertFalse(list((Path(self.temp.name) / "tasks" / self.sid / "inbox").glob("*.json")))
        archived = next((Path(self.temp.name) / "tasks" / self.sid / "receipts").glob("*.json"))
        self.assertNotIn("event", json.loads(archived.read_text(encoding="utf-8")))

    def test_crash_after_database_commit_replays_without_duplicate_event(self):
        self.store.capture_enqueue(self.sid, [self.event()])
        atomic = capture._atomic
        def crash_on_ack(path, data):
            if '"status":"drained"' in data.decode("utf-8", errors="replace"):
                raise SystemExit("simulated process loss before receipt ack")
            return atomic(path, data)
        with patch.object(capture, "_atomic", crash_on_ack):
            with self.assertRaises(SystemExit):
                self.store.drain_capture(self.sid)
        restarted = Store(self.temp.name)
        restarted.drain_capture(self.sid)
        self.assertEqual(len(restarted.get_events(self.sid)), 2)

    def test_crash_after_receipt_ack_cleans_queue_copy_without_replaying(self):
        self.store.capture_enqueue(self.sid, [self.event()])
        unlink = Path.unlink
        def crash_before_queue_cleanup(path, *args, **kwargs):
            if path.parent.name == "inbox" and path.suffix == ".json":
                raise SystemExit("simulated process loss after receipt ack")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", crash_before_queue_cleanup):
            with self.assertRaises(SystemExit):
                self.store.drain_capture(self.sid)
        restarted = Store(self.temp.name)
        restarted.drain_capture(self.sid)
        self.assertEqual(restarted.capture_status(self.sid)["received_count"], 1)
        self.assertEqual(restarted.capture_status(self.sid)["queued_count"], 0)
        self.assertEqual(len(restarted.get_events(self.sid)), 2)

    def test_long_hook_text_preserves_cleaned_tail_and_source_segments(self):
        text = ("a meaningful result with spaces\n" * 4000) + "TAIL-MARKER password=private-value"
        handle_hook({"session_id": "capture-host", "hook_event_name": "PostToolUse", "tool_name": "read_document",
                     "tool_use_id": "large-result", "tool_response": {"text": text, "image": {"type": "image", "data": "private-media"}}},
                    self.temp.name, store=self.store, worker_launcher=lambda _: None)
        self.store.drain_capture(self.sid)
        event = self.store.get_events(self.sid)[-1]
        ref = event["source_refs"][0]
        offset, parts = 0, []
        while True:
            part = self.store.source_read(self.sid, ref["source_id"], offset=offset)
            parts.append(part["text"])
            if not part["has_more"]:
                break
            offset = part["next_offset"]
        full = "".join(parts)
        self.assertIn("TAIL-MARKER", full)
        self.assertNotIn("private-value", full)
        self.assertNotIn("private-media", full)
        self.assertEqual(hashlib.sha256(full.encode("utf-8")).hexdigest(), ref["sha256"])
        self.assertLess(len(json.dumps(event).encode("utf-8")), 128 * 1024)
        self.assertGreater(len(list((Path(self.temp.name) / "tasks" / self.sid / "sources").glob("*/chunks/*.txt"))), 1)

    def test_source_read_requires_same_task_and_unmodified_snapshot(self):
        event = self.store.externalize_event(self.sid, self.event(details={"text": "many words " * 4000}))
        source_id = event["source_refs"][0]["source_id"]
        other = self.store.start_session("Other", "Separate", "other-host")["session_id"]
        with self.assertRaises(KeyError):
            self.store.source_read(other, source_id)
        source_path = next((Path(self.temp.name) / "tasks" / self.sid / "sources").glob("*/source.txt"))
        source_path.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.source_read(self.sid, source_id)

    def test_failed_analysis_keeps_capture_accepting(self):
        with self.store._transaction() as db:
            db.execute("UPDATE sessions SET status='analysis_failed',capture_state='recording',analysis_state='failed' WHERE id=?", (self.sid,))
        handle_hook({"session_id": "capture-host", "hook_event_name": "UserPromptSubmit", "turn_id": "after-failure", "prompt": "Keep this correction."},
                    self.temp.name, store=self.store, worker_launcher=lambda _: None)
        self.assertEqual(self.store.capture_status(self.sid)["queued_count"], 1)
        self.assertTrue(self.store.drain_capture(self.sid)["complete"])
        self.assertEqual(self.store.get_events(self.sid)[-1]["summary"], "Keep this correction.")

    def test_finish_boundary_drains_accepted_events_and_rejects_late_capture(self):
        self.store.capture_enqueue(self.sid, [self.event()])
        errors, started = [], threading.Event()
        def enqueue_late():
            started.set()
            try:
                self.store.capture_enqueue(self.sid, [self.event("late")])
            except ValueError as exc:
                errors.append(str(exc))
        with self.store.capture_boundary(self.sid):
            self.assertTrue(self.store.drain_capture(self.sid)["complete"])
            thread = threading.Thread(target=enqueue_late)
            thread.start()
            started.wait(1)
            with self.store._transaction() as db:
                db.execute("UPDATE sessions SET capture_state='closed' WHERE id=?", (self.sid,))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual(self.store.capture_status(self.sid)["queued_count"], 0)

    def test_explicit_binary_snapshot_survives_original_change(self):
        artifact = Path(self.temp.name) / "result.png"
        original = b"\x89PNG\r\n\x1a\nexplicit-output-version"
        artifact.write_bytes(original)
        receipt = self.store.artifact_register(self.sid, str(artifact), "Selected output image")
        artifact.write_bytes(b"new version")
        self.assertEqual(Path(receipt["snapshot_path"]).read_bytes(), original)
        self.assertEqual(receipt["source_hash"], hashlib.sha256(original).hexdigest())
        self.assertFalse(receipt["model_content_allowed"])
        event = self.store.get_events(self.sid)[-1]
        self.assertEqual(event["kind"], "artifact_registered")
        self.assertEqual(event["actor"], "host")
        self.assertEqual(event["details"]["artifact_id"], receipt["artifact_id"])
        self.assertFalse(event["details"]["model_content_allowed"])
        self.assertNotIn("explicit-output-version", json.dumps(event))

    def test_artifact_registry_and_learning_event_commit_together(self):
        artifact = Path(self.temp.name) / "result.bin"
        artifact.write_bytes(b"explicitly selected output")
        with patch.object(self.store, "_append", side_effect=RuntimeError("Cannot save learning event")):
            with self.assertRaises(RuntimeError):
                self.store.artifact_register(self.sid, str(artifact), "Selected binary output")
        with self.store._connection() as db:
            count = db.execute("SELECT COUNT(*) FROM capture_artifacts WHERE session_id=?", (self.sid,)).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(len(self.store.get_events(self.sid)), 1)

    def test_text_snapshot_redacts_and_records_distinct_hashes(self):
        artifact = Path(self.temp.name) / "report.json"
        artifact.write_text('{"password":"private-value","rows":8}', encoding="utf-8")
        receipt = self.store.artifact_register(self.sid, str(artifact), "Selected report")
        self.assertNotIn("private-value", Path(receipt["snapshot_path"]).read_text(encoding="utf-8"))
        self.assertNotEqual(receipt["source_hash"], receipt["snapshot_hash"])
        self.assertTrue(receipt["text_redacted"])

    def test_rejects_credential_file_and_directory(self):
        private = Path(self.temp.name) / "cookies.json"
        private.write_text("{}", encoding="utf-8")
        for path in (private, Path(self.temp.name)):
            with self.assertRaises(ValueError):
                self.store.artifact_register(self.sid, str(path), "Do not capture")

    def test_gap_is_visible_without_recording_failure_payload(self):
        self.store.record_capture_gap(self.sid, "enqueue_failed")
        status = self.store.capture_status(self.sid)
        self.assertEqual(status["capture_gap_count"], 1)
        self.assertEqual(status["latest_capture_gap"]["reason"], "enqueue_failed")

    def test_source_index_cannot_read_an_unregistered_external_path(self):
        event = self.store.externalize_event(self.sid, self.event(details={"text": "many words " * 4000}))
        source_id = event["source_refs"][0]["source_id"]
        with tempfile.TemporaryDirectory(prefix="blueprint-unregistered-") as outside:
            private = Path(outside) / "not-selected.txt"
            private.write_text("unregistered-private-content", encoding="utf-8")
            with self.store._transaction() as db:
                db.execute("UPDATE capture_sources SET relative_path=? WHERE session_id=? AND id=?", (str(private), self.sid, source_id))
            with self.assertRaises(ValueError):
                self.store.source_read(self.sid, source_id)

    def test_linked_artifacts_and_source_directories_are_rejected(self):
        target = Path(self.temp.name) / "unselected"
        target.mkdir()
        (target / "result.txt").write_text("unselected result", encoding="utf-8")
        linked = Path(self.temp.name) / "linked"
        try:
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(target), str(linked))
            else:
                linked.symlink_to(target, target_is_directory=True)
        except (OSError, AttributeError):
            self.skipTest("Directory links are unavailable in this environment")
        with self.assertRaises(ValueError):
            self.store.artifact_register(self.sid, str(linked / "result.txt"), "Linked output")
        with self.assertRaises(ValueError):
            self.store._capture_path(linked / "result.txt")

    def test_unattributed_ingress_gap_contains_only_health_metadata(self):
        capture.record_ingress_failure(self.temp.name, "host_input_limit")
        self.assertEqual(self.store.capture_status(self.sid)["unattributed_ingress_gap_count"], 1)
        marker = next((Path(self.temp.name) / "capture-health").glob("*.json"))
        self.assertEqual(set(json.loads(marker.read_text(encoding="utf-8"))), {"at", "reason", "task_attribution", "recoverable"})


if __name__ == "__main__":
    unittest.main()
