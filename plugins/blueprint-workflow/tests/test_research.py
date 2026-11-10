from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blueprint_workflow.analyzer import AnalysisProtocolError
from blueprint_workflow.research import (CodexResearchProvider, _decode_research, research_capabilities,
                                        run_research_once, validate_research_request, validate_research_result)
from blueprint_workflow.store import Store


def request():
    return {"id": "inspect", "question": "What officially supported alternative satisfies the known requirement?",
            "reason": "The observed method failed repeatedly", "evidence_event_ids": ["event-1"], "official_domains": ["example.com"]}


def result():
    return {"status": "succeeded", "summary": "A documented alternative may help; it remains untested.",
            "sources": [{"url": "https://example.com/docs", "title": "Primary documentation", "evidence": "Documented behavior relevant to the current question.",
                         "retrieved_at": "2026-11-10T10:00:00+00:00", "opened": True}], "limitations": ["No workflow change"]}


class ResearchTests(unittest.TestCase):
    def test_requests_require_task_evidence_and_public_domains(self):
        for changes in ({"evidence_event_ids": []}, {"official_domains": ["127.0.0.1"]}, {"official_domains": ["http://example.com"]}, {"official_domains": ["service.internal"]}):
            with self.assertRaises(ValueError):
                validate_research_request(request() | changes)

    def test_success_requires_opened_official_https_pages(self):
        for changes in ({"url": "http://example.com/docs"}, {"url": "https://wrong.example.org/docs"}, {"opened": False}, {"retrieved_at": "unknown"}):
            value = result()
            value["sources"][0].update(changes)
            with self.assertRaises(ValueError):
                validate_research_result(value, request())

    def test_observed_nonweb_tool_discards_whole_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            output.write_text(json.dumps(result()), encoding="utf-8")
            stream = json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "forbidden"}})
            with self.assertRaisesRegex(AnalysisProtocolError, "non-web"):
                _decode_research(stream, output, request())

    def test_citations_require_actual_open_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            output.write_text(json.dumps(result()), encoding="utf-8")
            search = {"type": "item.completed", "item": {"type": "web_search", "action": {"type": "search", "query": "an official alternative"}}}
            with self.assertRaisesRegex(AnalysisProtocolError, "not observed"):
                _decode_research(json.dumps(search), output, request())
            opened = {"type": "item.completed", "item": {"type": "web_search", "action": {"type": "open_page", "url": "https://example.com/docs"}}}
            checked = _decode_research(json.dumps(search) + "\n" + json.dumps(opened), output, request())
            self.assertEqual(checked["status"], "succeeded")
            incomplete = result()
            incomplete["sources"].append(incomplete["sources"][0] | {"url": "https://example.com/second-page"})
            output.write_text(json.dumps(incomplete), encoding="utf-8")
            with self.assertRaisesRegex(AnalysisProtocolError, "not observed"):
                _decode_research(json.dumps(opened), output, request())

    def test_native_requires_exact_identity_and_acceptance_marker(self):
        profile = {"version": "0.167.0-alpha.3.2", "executable_sha256": "fixed-binary", "command_prefix": ["codex.exe"]}
        with tempfile.TemporaryDirectory() as temporary, patch("blueprint_workflow.research._cli_profile", return_value=profile):
            self.assertFalse(research_capabilities(temporary)["native_research_available"])
            marker = {"version": profile["version"], "executable_sha256": profile["executable_sha256"], "negative_tools_verified": True, "live_research_verified": True}
            path = Path(temporary) / "research-capabilities.json"
            path.write_text(json.dumps(marker), encoding="utf-8")
            self.assertTrue(research_capabilities(temporary)["native_research_available"])
            marker["executable_sha256"] = "changed-binary"
            path.write_text(json.dumps(marker), encoding="utf-8")
            self.assertFalse(research_capabilities(temporary)["native_research_available"])

    def test_other_provider_never_silently_uses_codex(self):
        for provider in ("claude-code", "gemini-cli", "cursor"):
            capabilities = research_capabilities("unused", provider)
            self.assertFalse(capabilities["native_research_available"])
            self.assertEqual(capabilities["provider"], provider)
            self.assertTrue(capabilities["foreground_fallback"])

    def test_research_command_keeps_designer_isolation_except_hosted_web(self):
        profile = {"version": "0.167.0-alpha.3.2", "executable_sha256": "binary", "command_prefix": ["codex.exe"]}
        with tempfile.TemporaryDirectory() as temporary, patch("blueprint_workflow.research._cli_profile", return_value=profile):
            provider = CodexResearchProvider(temporary, model="selected-model", allow_unverified=True)
            command = provider._command(Path(temporary), Path(temporary) / "schema", Path(temporary) / "output", ["example.com"])
            self.assertIn('web_search="live"', command)
            self.assertIn("read-only", command)
            self.assertIn("shell_tool", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("mcp_servers={}", command)
            self.assertIn("selected-model", command)
            self.assertNotIn("danger-full-access", command)
            short = CodexResearchProvider(temporary, timeout=2, allow_unverified=True)
            self.assertEqual(short.timeout, 2)
            with self.assertRaises(ValueError):
                CodexResearchProvider(temporary, timeout=0, allow_unverified=True)

    def test_separate_research_runner_respects_backlog_profile_and_failed_analysis(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            sid = store.start_session("Task", "Complete a task", "original-chat", analysis_provider="codex", analysis_model="same-selected-model")["session_id"]
            event = store.get_events(sid)[0]
            queued = request() | {"evidence_event_ids": [event["id"]]}
            with store._transaction() as db:
                store.enqueue_research_requests(db, sid, [queued], {"through_seq": 1}, 1)
            store.append_events(sid, [{"kind": "observation", "summary": "A relevant work checkpoint"} for _ in range(40)])
            selected = []
            class FakeResearch:
                def research(self, job):
                    return result()
            def factory(data_dir, provider, model):
                selected.append((provider, model))
                return FakeResearch()
            self.assertFalse(run_research_once(store, temporary, factory))
            self.assertEqual(selected, [])
            with store._transaction() as db:
                db.execute("UPDATE sessions SET status='analysis_failed',analysis_state='failed' WHERE id=?", (sid,))
            self.assertTrue(run_research_once(store, temporary, factory))
            self.assertEqual(selected, [("codex", "same-selected-model")])
            self.assertEqual(store.get_events(sid)[-1]["kind"], "research_evidence")
            self.assertEqual(store.get_events(sid)[-1]["actor"], "tool")


if __name__ == "__main__":
    unittest.main()
