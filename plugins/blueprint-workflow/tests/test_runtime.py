import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blueprint_workflow.hooks import handle_hook
from blueprint_workflow.runtime import analysis_selection, scope_host_id
from blueprint_workflow.service import WorkflowService


class RuntimeTests(unittest.TestCase):
    def test_explicit_provider_does_not_inherit_other_providers_model(self):
        with patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_PROVIDER": "codex", "BLUEPRINT_WORKFLOW_MODEL": "codex-model"}):
            self.assertEqual(analysis_selection("claude-code"), ("claude-code", None))
            self.assertEqual(analysis_selection(), ("codex", "codex-model"))
            self.assertEqual(analysis_selection("claude", "sonnet"), ("claude-code", "sonnet"))

    def test_host_namespace_is_idempotent_and_rejects_other_host(self):
        self.assertEqual(scope_host_id("same-id", "claude-code"), "claude-code:same-id")
        self.assertEqual(scope_host_id("cursor:same-id", "cursor"), "cursor:same-id")
        with self.assertRaises(ValueError):
            scope_host_id("codex:same-id", "cursor")
        scoped = scope_host_id("x" * 256, "claude-code")
        self.assertEqual(scope_host_id(scoped, "claude-code"), scoped)

    def test_legacy_codex_binding_cannot_be_shadowed_after_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WorkflowService(directory, autostart=False)
            legacy = service.store.start_session("Old task", "Preserve my recording", host_session_id="physical-chat")
            with patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_HOST": "codex"}):
                with self.assertRaisesRegex(ValueError, legacy["session_id"]):
                    service.session_start("New task", "Cannot replace the old task", "physical-chat")

    def test_revision_inherits_profile_even_on_another_host(self):
        from test_store import example_workflow
        with tempfile.TemporaryDirectory() as directory:
            service = WorkflowService(directory, autostart=False)
            original = service.store.start_session("Report", "Produce report", analysis_provider="claude-code", analysis_model="sonnet")
            service.store.finish_session(original["session_id"], "Finished")
            job = service.store.claim_job("test")
            workflow = example_workflow([event["id"] for event in job["events"]])
            fixed = service.store.commit_job(job["job_id"], "test", {"workflow": workflow, "change_summary": "Done"})
            with patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_HOST": "codex", "BLUEPRINT_WORKFLOW_PROVIDER": "codex", "BLUEPRINT_WORKFLOW_MODEL": "different-model"}):
                revision = service.workflow_revise(fixed["workflow_id"])
                self.assertEqual((revision["analysis_provider"], revision["analysis_model"]), ("claude-code", "sonnet"))

    def test_model_identifier_cannot_echo_credentials(self):
        from blueprint_workflow.runtime import validate_model
        with self.assertRaises(ValueError):
            validate_model("sk-proj-" + "A" * 30)

    def test_missing_backend_is_persisted_without_silent_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_HOST": "cursor"}, clear=True):
            service = WorkflowService(directory, autostart=False)
            result = service.session_start("Example", "Learn this task", "same-id", analysis_provider="gemini-cli")
            self.assertEqual(result["analysis_provider"], "gemini-cli")
            self.assertEqual(result["host_session_id"], "cursor:same-id")
            changed = service.analysis_configure(result["session_id"], "claude", "sonnet")
            self.assertEqual(changed["analysis_provider"], "claude-code")
            self.assertEqual(changed["analysis_model"], "sonnet")

    def test_four_hosts_capture_only_their_bound_task(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WorkflowService(directory, autostart=False)
            ids = {}
            mapping = {"codex": "UserPromptSubmit", "claude-code": "UserPromptSubmit", "cursor": "beforeSubmitPrompt", "gemini-cli": "BeforeAgent"}
            for host in mapping:
                with patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_HOST": host}):
                    ids[host] = service.session_start(host, "Host isolation", "same-id", analysis_provider="codex")["session_id"]
            for host, event in mapping.items():
                payload = {"session_id": "same-id", "conversation_id": "same-id", "hook_event_name": event,
                           "prompt": "Instruction for " + host, "turn_id": "unique-turn", "transcript_path": "/never/read", "user_email": "never@store.invalid"}
                handle_hook(payload, directory, host=host, store=service.store, worker_launcher=lambda _: None)
            for host, sid in ids.items():
                service.store.drain_capture(sid)
                events = service.store.get_events(sid)
                self.assertEqual(len(events), 2)
                self.assertEqual(events[-1]["summary"], "Instruction for " + host)
                self.assertNotIn("never", str(events))

    def test_native_context_shapes_and_ignored_thoughts(self):
        class Empty:
            def find_session_by_host(self, _):
                return None
        for host, native in (("codex", "SessionStart"), ("claude-code", "SessionStart"), ("cursor", "sessionStart"), ("gemini-cli", "SessionStart")):
            result = handle_hook({"session_id": "x", "hook_event_name": native}, "unused", host=host, store=Empty())
            text = result.get("additional_context") if host == "cursor" else result["hookSpecificOutput"]["additionalContext"]
            self.assertIn(host + ":x", text)
        self.assertEqual(handle_hook({"session_id": "x", "hook_event_name": "afterAgentThought", "text": "private"}, host="cursor"), {})

    def test_claude_failure_retains_observed_error(self):
        class Bound:
            events = []
            def find_session_by_host(self, value):
                return {"session_id": "task", "status": "recording"} if value == "claude-code:x" else None
            def append_events(self, _, events):
                self.events.extend(events)
        store = Bound()
        handle_hook({"session_id": "x", "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                     "tool_use_id": "call", "error": "Command exited 1", "is_interrupt": False},
                    host="claude-code", store=store, worker_launcher=lambda _: None)
        self.assertEqual(store.events[0]["details"]["result"]["error"], "Command exited 1")

    def test_cursor_hook_parse_failure_preserves_native_output_contract(self):
        import io
        from blueprint_workflow.hooks import main
        class Input:
            buffer = io.BytesIO(b"not JSON")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", Input()), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(main(host="cursor"), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("session_status", stderr.getvalue())

    def test_serialized_cursor_tool_output_cannot_hide_media(self):
        import json
        from blueprint_workflow.hooks import without_media
        result = without_media(json.dumps({"content": [{"type": "text", "text": "Report complete"},
            {"type": "image", "mimeType": "image/png", "data": "PRIVATE_IMAGE"}]}))
        self.assertEqual(result["content"][0]["text"], "Report complete")
        self.assertNotIn("PRIVATE_IMAGE", str(result))


if __name__ == "__main__":
    unittest.main()
