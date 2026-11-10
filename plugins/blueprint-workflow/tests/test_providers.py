from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blueprint_workflow.analyzer import AnalysisError, AnalysisProtocolError, AnalysisTimeout, CodexAnalyzer
from blueprint_workflow.providers import (
    AnalysisProvider, ClaudeCodeAnalyzer, GeminiCLIAnalyzer, ProviderUnavailable,
    PROVIDER_NAMES, canonical_provider, create_provider, describe_provider,
    resolve_cli_command,
)


def snapshot():
    return {
        "session_id": "session-provider-test", "title": "A fixed process", "goal": "Produce a checked result",
        "base_revision": 1, "from_seq": 7, "through_seq": 8,
        "previous_draft": {"title": "earlier draft"},
        "events": [{"id": "event-7", "kind": "correction", "actor": "human", "summary": "Include shipping"}],
        "finalizing": True, "final_seq": 8, "at_final_boundary": True,
    }


def response():
    # The Store validates the full business schema and all evidence references;
    # these protocol tests only verify provider response extraction.
    return {"workflow": {"title": "updated method"}, "change_summary": "shipping included"}


class ProviderSelectionTests(unittest.TestCase):
    def test_names_and_aliases_are_explicit(self):
        self.assertEqual(PROVIDER_NAMES, ("codex", "claude-code", "gemini-cli", "cursor"))
        self.assertEqual(canonical_provider(" Claude "), "claude-code")
        self.assertEqual(canonical_provider("gemini"), "gemini-cli")
        self.assertEqual(canonical_provider("cursor-cli"), "cursor")
        with self.assertRaises(ValueError):
            canonical_provider("unknown-provider")

    def test_factory_keeps_selected_provider_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as temporary, patch("blueprint_workflow.providers.resolve_cli_command", return_value=[sys.executable]):
            codex = create_provider(temporary, "codex")
            claude = create_provider(temporary, "claude")
            gemini = create_provider(temporary, "gemini")
            self.assertIsInstance(codex, CodexAnalyzer)
            self.assertIsInstance(claude, ClaudeCodeAnalyzer)
            self.assertIsInstance(gemini, GeminiCLIAnalyzer)
            self.assertIsInstance(claude, AnalysisProvider)
        with tempfile.TemporaryDirectory() as temporary, patch("blueprint_workflow.providers.resolve_cli_command", side_effect=ProviderUnavailable("Claude not installed")) as resolve:
            with self.assertRaisesRegex(ProviderUnavailable, "not installed"):
                create_provider(temporary, "claude-code")
            resolve.assert_called_once_with("claude-code")

    def test_cursor_is_explicitly_unavailable_despite_installable_host(self):
        with patch("blueprint_workflow.providers.resolve_cli_command") as resolve, patch("blueprint_workflow.analyzer.subprocess.Popen") as popen:
            description = describe_provider("cursor")
            self.assertFalse(description["available"])
            self.assertFalse(description["supports_background"])
            self.assertIn("Cursor host adapter is separate", description["reason"])
            with self.assertRaises(ProviderUnavailable):
                create_provider(".", "cursor")
            resolve.assert_not_called()
            popen.assert_not_called()

    def test_describe_is_nonmutating_and_does_not_test_auth_or_model(self):
        with patch("blueprint_workflow.providers.resolve_cli_command", return_value=[sys.executable]), patch("blueprint_workflow.analyzer.subprocess.Popen") as popen:
            description = describe_provider("claude", model="sonnet")
            self.assertEqual(description["provider"], "claude-code")
            self.assertEqual(description["name"], "Claude Code")
            self.assertTrue(description["available"])
            self.assertEqual(description["authentication"], "not_checked")
            self.assertFalse(description["model_request_sent"])
            popen.assert_not_called()

    def test_missing_native_cli_is_reported(self):
        with patch("blueprint_workflow.providers.shutil.which", return_value=None), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderUnavailable):
                resolve_cli_command("gemini-cli")
            description = describe_provider("gemini-cli")
            self.assertFalse(description["available"])
            self.assertIn("not found", description["reason"])

    @unittest.skipUnless(os.name == "nt", "Windows npm shim resolution")
    def test_verified_npm_shim_with_spaces_and_ampersand_uses_native_node(self):
        with tempfile.TemporaryDirectory(prefix="blueprint provider & ") as temporary:
            base = Path(temporary)
            shim = base / "gemini.cmd"
            shim.write_text("@echo off\r\nrem test shim must never run\r\n", encoding="ascii")
            node = base / "node.exe"
            node.write_bytes(b"test native entrypoint placeholder")
            package = base / "node_modules" / "@google" / "gemini-cli"
            (package / "dist").mkdir(parents=True)
            entry = package / "dist" / "index.js"
            entry.write_text("// fake installed entrypoint", encoding="utf-8")
            (package / "package.json").write_text(json.dumps({"name": "@google/gemini-cli", "bin": {"gemini": "dist/index.js"}}), encoding="utf-8")
            command = resolve_cli_command("gemini-cli", str(shim))
            self.assertEqual(command, [str(node.resolve()), str(entry.resolve())])
            self.assertFalse(any(part.lower() in {"cmd", "cmd.exe", "/c"} for part in command))
            self.assertNotIn(str(shim), command)

    @unittest.skipUnless(os.name == "nt", "Windows npm shim verification")
    def test_unverifiable_or_escaping_windows_shim_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            shim = base / "claude.cmd"
            shim.write_text("@echo arbitrary shell command", encoding="ascii")
            with self.assertRaises(ProviderUnavailable):
                resolve_cli_command("claude-code", str(shim))
            package = base / "node_modules" / "@anthropic-ai" / "claude-code"
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"name": "@anthropic-ai/claude-code", "bin": {"claude": "../../../../outside.js"}}), encoding="utf-8")
            with self.assertRaisesRegex(ProviderUnavailable, "contained"):
                resolve_cli_command("claude-code", str(shim))

    @unittest.skipUnless(os.name == "nt" and shutil.which("node"), "Windows native Node shim integration")
    def test_real_node_entrypoint_never_executes_cmd_shim_in_special_character_path(self):
        with tempfile.TemporaryDirectory(prefix="blueprint & npm ") as temporary:
            base = Path(temporary)
            marker = base / "shim-was-executed.txt"
            shim = base / "claude.cmd"
            shim.write_text('@echo unwanted>"' + str(marker) + '"\r\n', encoding="utf-8")
            package = base / "node_modules" / "@anthropic-ai" / "claude-code"
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"name": "@anthropic-ai/claude-code", "bin": {"claude": "cli.js"}}), encoding="utf-8")
            (package / "cli.js").write_text(
                "let input=''; process.stdin.setEncoding('utf8'); process.stdin.on('data',chunk=>input+=chunk);"
                "process.stdin.on('end',()=>{if(!input.includes('BACKGROUND WORKFLOW DESIGNER'))process.exit(3);"
                "process.stdout.write(JSON.stringify({subtype:'success',structured_output:" + json.dumps(response()) + "}));});",
                encoding="utf-8",
            )
            provider = ClaudeCodeAnalyzer(base / "private-data", executable=str(shim), timeout=10)
            self.assertEqual(provider.analyze(snapshot()), response())
            self.assertFalse(marker.exists())

    def test_executable_override_is_one_path_not_shell_words(self):
        with patch.dict(os.environ, {"BLUEPRINT_WORKFLOW_CLAUDE_CODE_EXECUTABLE": "claude --unsafe-option"}), patch("blueprint_workflow.providers.shutil.which", return_value=None):
            with self.assertRaises(ProviderUnavailable):
                resolve_cli_command("claude-code")


class ClaudeProviderTests(unittest.TestCase):
    def test_official_no_tools_flags_and_json_schema_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "fake_claude.py"
            expected = response()
            fake.write_text(
                "import json,os,sys\nfrom pathlib import Path\n"
                "args=sys.argv[1:]\n"
                "assert '-p' in args and '--safe-mode' in args and '--no-session-persistence' in args\n"
                "assert args[args.index('--tools')+1]==''\n"
                "assert args[args.index('--disallowedTools')+1]=='*'\n"
                "assert args[args.index('--permission-mode')+1]=='dontAsk'\n"
                "assert '--strict-mcp-config' in args\n"
                "assert json.loads(Path(args[args.index('--mcp-config')+1]).read_text())=={'mcpServers':{}}\n"
                "assert json.loads(Path(args[args.index('--settings')+1]).read_text())['disableAllHooks'] is True\n"
                "assert json.loads(args[args.index('--json-schema')+1])['required']==['workflow','change_summary']\n"
                "prompt=sys.stdin.read()\n"
                "assert '\\\"from_seq\\\": 7' in prompt and '\\\"earlier draft\\\"' in prompt\n"
                "assert os.environ['BLUEPRINT_WORKFLOW_ANALYZER']=='1'\n"
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':" + repr(expected) + "}))\n",
                encoding="utf-8",
            )
            analyzer = ClaudeCodeAnalyzer(temporary, command_prefix=[sys.executable, str(fake)], timeout=10)
            self.assertEqual(analyzer.analyze(snapshot()), expected)
            self.assertEqual(list((Path(temporary) / "analysis-work").iterdir()), [])

    def test_missing_structured_output_does_not_accept_plain_result(self):
        provider = ClaudeCodeAnalyzer(".", command_prefix=[sys.executable])
        for envelope in (
            {"type": "result", "subtype": "success", "result": json.dumps(response())},
            {"type": "result", "is_error": True, "structured_output": response()},
            {"type": "result", "permission_denials": [{"tool": "Bash"}], "structured_output": response()},
        ):
            with self.assertRaises(AnalysisProtocolError):
                provider._decode(json.dumps(envelope))

    def test_provider_failure_and_timeout_are_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "failed_cli.py"
            fake.write_text("import sys\nsys.stderr.write('authentication unavailable')\nsys.exit(1)\n", encoding="utf-8")
            provider = ClaudeCodeAnalyzer(temporary, command_prefix=[sys.executable, str(fake)], timeout=5)
            with self.assertRaisesRegex(AnalysisError, "exited with code 1"):
                provider.analyze(snapshot())
            fake.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
            provider.timeout = 0.1
            with self.assertRaises(AnalysisTimeout):
                provider.analyze(snapshot())


class GeminiProviderTests(unittest.TestCase):
    def test_no_tools_settings_and_policy_are_loaded_in_controlled_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "fake_gemini.py"
            expected = response()
            fake.write_text(
                "import json,os,sys\nfrom pathlib import Path\n"
                "args=sys.argv[1:]\n"
                "assert args[args.index('--extensions')+1]=='none'\n"
                "assert args[args.index('--approval-mode')+1]=='default'\n"
                "assert args[args.index('--allowed-mcp-server-names')+1]=='__blueprint_analysis_no_mcp__'\n"
                "settings=json.loads(Path('.gemini/settings.json').read_text())\n"
                "assert settings['tools']['core']==[]\n"
                "assert settings['hooksConfig']['enabled'] is False and settings['skills']['enabled'] is False\n"
                "assert settings['tools']['discoveryCommand']==''\n"
                "policy=Path(args[args.index('--policy')+1]).read_text()\n"
                "assert 'toolName = \\\"*\\\"' in policy and 'decision = \\\"deny\\\"' in policy\n"
                "assert os.environ['GEMINI_CLI_TRUST_WORKSPACE']=='true'\n"
                "prompt=sys.stdin.read()\n"
                "assert '<required_json_output_schema>' in prompt and '\\\"from_seq\\\": 7' in prompt\n"
                "print(json.dumps({'response':json.dumps(" + repr(expected) + "),'stats':{'tools':{'totalCalls':0}}}))\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(Path(temporary) / "absent-system.json")}):
                provider = GeminiCLIAnalyzer(temporary, command_prefix=[sys.executable, str(fake)], timeout=10)
                self.assertEqual(provider.analyze(snapshot()), expected)

    def test_system_policy_is_not_overwritten_or_bypassed(self):
        with tempfile.TemporaryDirectory() as temporary:
            system = Path(temporary) / "admin-settings.json"
            system.write_text(json.dumps({"hooksConfig": {"enabled": True}}), encoding="utf-8")
            original = system.read_bytes()
            provider = GeminiCLIAnalyzer(temporary, command_prefix=[sys.executable])
            with patch.dict(os.environ, {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(system)}), patch("blueprint_workflow.providers.run_analysis_process") as run:
                with self.assertRaisesRegex(ProviderUnavailable, "system policy"):
                    provider.analyze(snapshot())
                run.assert_not_called()
            self.assertEqual(system.read_bytes(), original)

    def test_gemini_rejects_malformed_model_json_and_any_reported_tool_use(self):
        provider = GeminiCLIAnalyzer(".", command_prefix=[sys.executable])
        for envelope in (
            {"response": "```json\n{}\n```"},
            {"response": json.dumps(response()), "stats": {"tools": {"totalCalls": 1}}},
            {"response": json.dumps(response()), "error": {"message": "not authenticated"}},
        ):
            with self.assertRaises(AnalysisProtocolError):
                provider._decode(json.dumps(envelope))

    def test_readonly_availability_reports_managed_safety_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            system = Path(temporary) / "system.json"
            system.write_text(json.dumps({"tools": {"core": ["run_shell_command"]}}), encoding="utf-8")
            with patch.dict(os.environ, {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(system)}), patch("blueprint_workflow.providers.resolve_cli_command", return_value=[sys.executable]):
                result = describe_provider("gemini-cli")
            self.assertFalse(result["available"])
            self.assertIn("system policy", result["reason"])


if __name__ == "__main__":
    unittest.main()
