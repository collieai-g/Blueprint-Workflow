from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from blueprint_workflow.service import WorkflowService
from test_store import example_workflow


class ServiceTests(unittest.TestCase):
    def test_materialize_includes_registered_snapshot_after_original_changes(self):
        import hashlib
        import json
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as source_directory:
            service = WorkflowService(directory, autostart=False)
            sid = service.session_start('Task', 'Produce the requested artifact')['session_id']
            original = Path(source_directory) / 'result.bin'
            original.write_bytes(b'original task output\x00\xff')
            receipt = service.artifact_register(sid, str(original), 'Explicitly selected task result')
            original.write_bytes(b'changed later')
            service.store.finish_session(sid, 'The demonstration is complete')
            job = service.store.claim_job('test')
            workflow = example_workflow([e['id'] for e in job['events']])
            finished = service.store.commit_job(job['job_id'], 'test', {'workflow': workflow, 'change_summary': 'Preserved the method and registered result'})
            exported = service.workflow_materialize(finished['workflow_id'])
            index = json.loads((Path(exported['directory']) / 'artifacts.json').read_text(encoding='utf-8'))
            self.assertFalse(index['executed'])
            artifact = index['artifacts'][0]
            result = Path(exported['directory']) / artifact['path']
            self.assertEqual(result.read_bytes(), b'original task output\x00\xff')
            self.assertEqual(hashlib.sha256(result.read_bytes()).hexdigest(), receipt['snapshot_hash'])
            self.assertNotIn(str(original), json.dumps(index))

    def test_background_launch_failure_preserves_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WorkflowService(directory, autostart=True)
            with patch("blueprint_workflow.worker.ensure_worker", side_effect=OSError("spawn failed")):
                result = service.session_start("Task", "Create a report")
                self.assertIn("background_warning", result)
                sid = result["session_id"]
                appended = service.event_append(sid, [{"kind": "observation", "summary": "Read the source", "client_event_id": "one"}])
                self.assertIn("background_warning", appended)
                self.assertEqual(service.store.status(sid)["latest_seq"], 2)
            listed = service.session_list()["sessions"][0]
            self.assertNotIn("draft", listed)

    def test_materializes_helpers_without_executing(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WorkflowService(directory, autostart=False)
            started = service.session_start("Task", "Create a report")
            sid = started["session_id"]
            service.store.finish_session(sid, "Finished")
            job = service.store.claim_job("test")
            evidence = [event["id"] for event in job["events"]]
            workflow = example_workflow(evidence)
            workflow["helpers"] = [{"name": "calculate.py", "language": "python", "description": "A generated optional helper",
                "content": "raise RuntimeError('Materializing must never run this source')\n", "evidence_event_ids": evidence}]
            finished = service.store.commit_job(job["job_id"], "test", {"workflow": workflow, "change_summary": "Add helper"})
            exported = service.workflow_materialize(finished["workflow_id"])
            self.assertFalse(exported["executed"])
            self.assertEqual(len(exported["files"]), 3)
            self.assertTrue((Path(exported["directory"]) / "helpers" / "calculate.py").is_file())
            self.assertTrue((Path(exported["directory"]) / "workflow.json").is_file())

    @unittest.skipUnless(os.name == "nt", "Windows directory junction regression")
    def test_materialize_rejects_directory_junction_escape(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            service = WorkflowService(directory, autostart=False)
            started = service.session_start("Task", "Create a report")
            service.store.finish_session(started["session_id"], "Finished")
            job = service.store.claim_job("test")
            evidence = [event["id"] for event in job["events"]]
            workflow = example_workflow(evidence)
            workflow["helpers"] = [{"name": "helper.py", "language": "python", "description": "Optional helper",
                                    "content": "print('example')\n", "evidence_event_ids": evidence}]
            finished = service.store.commit_job(job["job_id"], "test", {"workflow": workflow, "change_summary": "Helper"})
            root = Path(directory) / "exports" / finished["workflow_id"] / "v1"
            root.mkdir(parents=True)
            junction = root / "helpers"
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), outside], capture_output=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest("This test environment does not permit directory junction creation")
            try:
                with self.assertRaises(ValueError):
                    service.workflow_materialize(finished["workflow_id"])
                self.assertEqual(list(Path(outside).iterdir()), [])
            finally:
                if junction.is_junction():
                    os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
