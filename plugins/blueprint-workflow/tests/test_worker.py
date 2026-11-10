from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from blueprint_workflow.analyzer import AnalysisError, AnalysisProtocolError, AnalysisTimeout, CodexAnalyzer, _WindowsAnalysisJob, _extract_response
from blueprint_workflow.worker import WorkerLock, _batch_ready, ensure_worker, run_once, run_worker, worker_status


def job(**changes):
    result = {
        "job_id": "job-1", "session_id": "session-1", "title": "整理資料", "goal": "輸出總額",
        "base_revision": 2, "from_seq": 3, "through_seq": 4, "events": [{"id": "event-3", "seq": 3, "kind": "correction", "actor": "human", "summary": "成本須包括運費"}],
        "previous_draft": {"title": "existing method"}, "finalizing": False, "final_seq": None, "retry_attempts": 0,
    }
    result.update(changes)
    return result


class FakeStore:
    def __init__(self, path, jobs):
        self.data_dir = Path(path)
        self.jobs = list(jobs)
        self.commits = []
        self.failures = []
        self.heartbeats = []
        self.lease = True

    def claim_job(self, worker_id, lease_seconds=300):
        return self.jobs.pop(0) if self.jobs else None

    def commit_job(self, job_id, worker_id, analysis):
        self.commits.append((job_id, worker_id, analysis))

    def fail_job(self, job_id, worker_id, error, retry_delay=5):
        self.failures.append((job_id, worker_id, error, retry_delay))

    def heartbeat_job(self, job_id, worker_id, lease_seconds=300):
        self.heartbeats.append((job_id, worker_id))
        return self.lease


class WorkerTests(unittest.TestCase):
    def test_commits_incremental_analysis(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = FakeStore(temporary, [job()])
            analyzer = Mock()
            analyzer.timeout = 20
            analyzer.analyze.return_value = {"workflow": {"title": "updated method"}, "change_summary": "included shipping"}
            self.assertTrue(run_once(store, analyzer, worker_id="w"))
            self.assertEqual(store.commits[0][2], analyzer.analyze.return_value)
            self.assertEqual(analyzer.analyze.call_args.args[0]["from_seq"], 3)
            self.assertEqual(len(store.failures), 0)
            self.assertFalse(run_once(store, analyzer, worker_id="w"))

    def test_failure_does_not_commit_cursor(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = FakeStore(temporary, [job()])
            analyzer = Mock(timeout=20)
            analyzer.analyze.side_effect = AnalysisError("service temporarily unavailable")
            self.assertTrue(run_once(store, analyzer, worker_id="w"))
            self.assertEqual(store.commits, [])
            self.assertEqual(store.failures[0][3], 2)

    def test_retry_limit_blocks_instead_of_spinning(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = FakeStore(temporary, [job(retry_attempts=4)])
            analyzer = Mock(timeout=20)
            analyzer.analyze.side_effect = AnalysisError("quota exhausted")
            run_once(store, analyzer, worker_id="w")
            self.assertIsNone(store.failures[0][3])

    def test_lost_lease_discards_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = FakeStore(temporary, [job()])
            store.lease = False
            analyzer = Mock(timeout=20)
            def analyze(_job):
                time.sleep(0.04)
                return {"workflow": {}, "change_summary": "late"}
            analyzer.analyze.side_effect = analyze
            run_once(store, analyzer, worker_id="w", heartbeat_interval=0.005)
            self.assertEqual(store.commits, [])
            self.assertIn("lease was lost", store.failures[0][2])

    def test_os_lock_is_released_and_excludes_second_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = WorkerLock(Path(temporary) / "lock")
            second = WorkerLock(Path(temporary) / "lock")
            self.assertTrue(first.acquire())
            try:
                self.assertFalse(second.acquire())
            finally:
                first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_debounce_and_finalization_priority(self):
        store = Mock()
        store.pending_sessions.return_value = [{"id": "s", "last_event_at": time.time(), "status": "recording", "next_attempt_at": 0}]
        first_seen = {}
        self.assertFalse(_batch_ready(store, first_seen, 2, 10))
        first_seen["s"] = time.time() - 11
        self.assertTrue(_batch_ready(store, first_seen, 2, 10))
        store.pending_sessions.return_value[0]["status"] = "finalizing"
        self.assertTrue(_batch_ready(store, {}, 2, 10))
        store.pending_sessions.return_value[0]["next_attempt_at"] = time.time() + 100
        self.assertFalse(_batch_ready(store, {}, 2, 10))

    def test_autostart_and_analyzer_exclusion_do_not_spawn(self):
        with tempfile.TemporaryDirectory() as temporary, patch("blueprint_workflow.worker.subprocess.Popen") as popen:
            with patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_AUTOSTART": "0"}):
                self.assertEqual(ensure_worker(temporary)["status"], "autostart_disabled")
            with patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_AUTOSTART": "1", "BLUEPRINT_WORKFLOW_ANALYZER": "1"}):
                self.assertEqual(ensure_worker(temporary)["status"], "analyzer_excluded")
            popen.assert_not_called()

    def test_hidden_launch_uses_argument_list_and_returns_before_analysis(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_AUTOSTART": "1", "BLUEPRINT_WORKFLOW_ANALYZER": "0"}), patch("blueprint_workflow.worker.subprocess.Popen") as popen:
            popen.return_value.pid = os.getpid()
            result = ensure_worker(temporary)
            self.assertEqual(result["status"], "starting")
            command = popen.call_args.args[0]
            self.assertEqual(command[:4], [sys.executable, "-m", "blueprint_workflow", "worker"])
            self.assertEqual(command[-1], str(Path(temporary).resolve()))
            self.assertNotIn("shell", popen.call_args.kwargs)
            if os.name == "nt":
                self.assertTrue(popen.call_args.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
            self.assertIn("PYTHONPATH", popen.call_args.kwargs["env"])

    def test_foreground_empty_worker_stops_and_restores_marker(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_WORKER": "original"}):
            analyzer = Mock(timeout=20)
            result = run_worker(temporary, analyzer=analyzer, once=True)
            self.assertEqual(result["jobs_processed"], 0)
            analyzer.analyze.assert_not_called()
            self.assertFalse(worker_status(temporary)["running"])
            self.assertEqual(os.environ["BLUEPRINT_WORKFLOW_WORKER"], "original")

    def test_real_store_gets_revisions_before_finish_and_finalizes_only_tail(self):
        from blueprint_workflow.store import Store

        class IncrementalAnalyzer:
            timeout = 20
            seen = []

            def analyze(self, current):
                self.seen.append(current)
                previous = current.get("previous_draft")
                evidence = list(previous["steps"][0]["evidence_event_ids"]) if previous else []
                evidence += [event["id"] for event in current["events"]]
                instruction = "Read the current inputs and calculate the total including shipping."
                return {"workflow": {
                    "title": "Cost calculation", "goal": "Calculate total", "inputs": [], "environment": [],
                    "steps": [{"id": "calculate", "title": "Calculate", "instruction": instruction,
                               "depends_on": [], "success_criteria": ["The total matches the confirmed formula"],
                               "evidence_event_ids": evidence, "outputs": ["total"], "when": None}],
                    "completion_criteria": ["The total is checked"], "questions": [], "proposals": [],
                    "artifacts": [], "summary": "A reusable cost calculation method"},
                    "change_summary": "Updated from the new event range"}

        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            session = store.start_session("Cost calculation", "Calculate total")
            session_id = session["session_id"]
            analyzer = IncrementalAnalyzer()
            store.append_events(session_id, [{"kind": "instruction", "actor": "human", "summary": "Calculate the current input costs"}])
            self.assertTrue(run_once(store, analyzer, worker_id="integration-worker"))
            first = store.status(session_id)
            self.assertEqual(first["draft_revision"], 1)
            self.assertEqual(first["status"], "recording")
            store.append_events(session_id, [{"kind": "correction", "actor": "human", "summary": "Include shipping in the total"}])
            self.assertTrue(run_once(store, analyzer, worker_id="integration-worker"))
            second = store.status(session_id)
            self.assertEqual(second["draft_revision"], 2)
            self.assertGreater(analyzer.seen[1]["from_seq"], analyzer.seen[0]["through_seq"])
            self.assertIsNotNone(analyzer.seen[1]["previous_draft"])
            store.finish_session(session_id, "The checked total has been produced")
            self.assertTrue(run_once(store, analyzer, worker_id="integration-worker"))
            final = store.status(session_id)
            self.assertEqual(final["status"], "ready_to_test")
            self.assertEqual(final["processed_seq"], final["final_seq"])
            self.assertEqual(analyzer.seen[2]["from_seq"], second["processed_seq"] + 1)


class AnalyzerTests(unittest.TestCase):
    def test_command_is_read_only_and_disables_execution_surfaces(self):
        analyzer = CodexAnalyzer(".", executable="codex.exe")
        command = analyzer._command(Path("schema.json"), Path("response.json"), Path("work"))
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        disabled = [command[index + 1] for index, part in enumerate(command[:-1]) if part == "--disable"]
        self.assertTrue({"hooks", "plugins", "apps", "shell_tool", "computer_use", "browser_use", "multi_agent"}.issubset(disabled))
        self.assertEqual(command[-1], "-")

    def test_fake_cli_receives_only_incremental_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "fake_cli.py"
            fake.write_text(
                "import json,os,sys\n"
                "from pathlib import Path\n"
                "prompt=sys.stdin.read()\n"
                "assert os.environ['BLUEPRINT_WORKFLOW_ANALYZER']=='1'\n"
                "assert 'OPENAI_API_KEY' not in os.environ\n"
                "assert 'Codex' not in prompt or 'BACKGROUND WORKFLOW DESIGNER' in prompt\n"
                "assert '\\\"from_seq\\\": 3' in prompt\n"
                "assert '\\\"at_final_boundary\\\": true' in prompt\n"
                "out=Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
                "out.write_text(json.dumps({'workflow':{'title':'updated'},'change_summary':'delta processed'}),encoding='utf-8')\n"
                "print(json.dumps({'type':'item.completed','item':{'type':'reasoning','text':'never retain this stream'}}))\n",
                encoding="utf-8",
            )
            analyzer = CodexAnalyzer(temporary, command_prefix=[sys.executable, str(fake)], timeout=10)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-not-for-child"}):
                result = analyzer.analyze(job(finalizing=True, final_seq=4, at_final_boundary=True))
            self.assertEqual(result["change_summary"], "delta processed")
            self.assertEqual(list((Path(temporary) / "analysis-work").iterdir()), [])

    def test_tool_operation_discards_otherwise_valid_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "response.json"
            output.write_text(json.dumps({"workflow": {}, "change_summary": "x"}), encoding="utf-8")
            stream = json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "do something"}})
            with self.assertRaises(AnalysisProtocolError):
                _extract_response(stream, output)

    def test_no_json_or_failed_turn_never_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "absent.json"
            with self.assertRaises(AnalysisProtocolError):
                _extract_response("", output)
            with self.assertRaises(AnalysisProtocolError):
                _extract_response(json.dumps({"type": "turn.failed"}), output)
            output.write_text("```json\n{}\n```", encoding="utf-8")
            with self.assertRaises(AnalysisProtocolError):
                _extract_response("", output)

    def test_timeout_terminates_process_tree(self):
        with tempfile.TemporaryDirectory() as temporary, patch("blueprint_workflow.analyzer.subprocess.Popen") as popen, patch("blueprint_workflow.analyzer._terminate_process_tree") as terminate, patch("blueprint_workflow.analyzer._WindowsAnalysisJob") as windows_job:
            process = popen.return_value
            process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 1), ("", "")]
            analyzer = CodexAnalyzer(temporary, executable="codex.exe", timeout=1)
            with self.assertRaises(AnalysisTimeout):
                analyzer.analyze(job())
            terminate.assert_called_once_with(process)
            if os.name == "nt":
                windows_job.return_value.assign.assert_called_once_with(process)
                windows_job.return_value.close.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows job ownership contract")
    def test_windows_job_is_assigned_before_gate_receives_payload_and_closed(self):
        with tempfile.TemporaryDirectory() as temporary, patch("blueprint_workflow.analyzer.subprocess.Popen") as popen, patch("blueprint_workflow.analyzer._WindowsAnalysisJob") as windows_job:
            sequence = []
            process = popen.return_value
            process.returncode = 0
            windows_job.return_value.assign.side_effect = lambda _: sequence.append("assigned")
            windows_job.return_value.close.side_effect = lambda: sequence.append("closed")
            def communicate(payload, timeout):
                sequence.append("released")
                decoded = json.loads(payload)
                self.assertIn("--output-schema", decoded["command"])
                self.assertIn("BACKGROUND WORKFLOW DESIGNER", decoded["prompt"])
                return json.dumps({"workflow": {}, "change_summary": "ok"}), ""
            process.communicate.side_effect = communicate
            result = CodexAnalyzer(temporary, executable="codex.exe").analyze(job())
            self.assertEqual(result["change_summary"], "ok")
            self.assertEqual(sequence, ["assigned", "released", "closed"])
            launch_command = popen.call_args.args[0]
            self.assertEqual(launch_command[:3], [sys.executable, "-X", "utf8"])
            self.assertNotIn("codex.exe", launch_command)

    @unittest.skipUnless(os.name == "nt", "Windows job assignment failure")
    def test_failed_windows_assignment_terminates_gate_without_releasing_command(self):
        with tempfile.TemporaryDirectory() as temporary, patch("blueprint_workflow.analyzer.subprocess.Popen") as popen, patch("blueprint_workflow.analyzer._WindowsAnalysisJob") as windows_job, patch("blueprint_workflow.analyzer._terminate_process_tree") as terminate:
            process = popen.return_value
            process.communicate.return_value = ("", "")
            windows_job.return_value.assign.side_effect = AnalysisError("Cannot assign process job")
            with self.assertRaisesRegex(AnalysisError, "Cannot assign"):
                CodexAnalyzer(temporary, executable="codex.exe").analyze(job())
            terminate.assert_called_once_with(process)
            windows_job.return_value.close.assert_called_once()
            # communicate without a payload only drains/ends the killed gate.
            self.assertTrue(all(not call.args for call in process.communicate.call_args_list))

    @unittest.skipUnless(os.name == "nt", "Windows job handle creation lifecycle")
    def test_windows_job_closes_when_process_creation_fails(self):
        with tempfile.TemporaryDirectory() as temporary, patch("blueprint_workflow.analyzer.subprocess.Popen", side_effect=OSError("launch failed")), patch("blueprint_workflow.analyzer._WindowsAnalysisJob") as windows_job:
            with self.assertRaisesRegex(AnalysisError, "Cannot start"):
                CodexAnalyzer(temporary, executable="codex.exe").analyze(job())
            windows_job.return_value.assign.assert_not_called()
            windows_job.return_value.close.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows kill-on-close behavior")
    def test_closing_windows_job_terminates_only_its_owned_test_child(self):
        owner = _WindowsAnalysisJob()
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            owner.assign(process)
            self.assertIsNone(process.poll())
            owner.close()
            self.assertIsNotNone(process.wait(timeout=10))
        finally:
            owner.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdin:
                process.stdin.close()

    @unittest.skipUnless(os.name == "nt", "Windows parent-crash process ownership")
    def test_killing_test_owner_also_terminates_its_owned_child(self):
        import ctypes
        from ctypes import wintypes

        program = (
            "import subprocess,sys,time\n"
            "from pathlib import Path\n"
            "from blueprint_workflow.analyzer import _WindowsAnalysisJob\n"
            "owner=_WindowsAnalysisJob()\n"
            "child=subprocess.Popen([sys.executable,'-c','import sys; sys.stdin.read()'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)\n"
            "owner.assign(child)\n"
            "Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii')\n"
            "time.sleep(60)\n"
        )
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "child.pid"
            parent = subprocess.Popen(
                [sys.executable, "-c", program, str(marker)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            child_handle = None
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not marker.exists():
                    self.fail("Owned test process did not finish Windows Job setup")
                child_pid = int(marker.read_text(encoding="ascii"))
                child_handle = kernel.OpenProcess(0x00100001, False, child_pid)  # SYNCHRONIZE | PROCESS_TERMINATE for failed-test cleanup
                self.assertTrue(child_handle)
                self.assertEqual(kernel.WaitForSingleObject(child_handle, 0), 258)  # WAIT_TIMEOUT: child alive
                # Only the owner created by this test is killed. No model or
                # unrelated application is involved in this crash simulation.
                parent.kill()
                parent.wait(timeout=10)
                self.assertEqual(kernel.WaitForSingleObject(child_handle, 5000), 0)
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=10)
                if child_handle:
                    if kernel.WaitForSingleObject(child_handle, 0) == 258:
                        kernel.TerminateProcess(child_handle, 1)
                        kernel.WaitForSingleObject(child_handle, 5000)
                    kernel.CloseHandle(child_handle)
                if parent.stderr:
                    parent.stderr.close()


if __name__ == "__main__":
    unittest.main()
