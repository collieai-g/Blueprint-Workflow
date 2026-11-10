from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from blueprint_workflow import installation


def provider_info(host, provider, model):
    selected = provider or host
    return {"provider": selected, "available": selected != "cursor", "supports_background": selected != "cursor",
            "selection_required": host == "cursor" and provider is None, "authentication": "not_checked"}


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="blueprint-install-")
        self.root = Path(self.temporary.name)
        self.data = self.root / "private"
        self.home = self.root / "fake-home"
        self.home.mkdir()
        self.providers = patch.object(installation, "_provider_info", side_effect=provider_info)
        self.providers.start()
        self.addCleanup(self.providers.stop)
        self.addCleanup(self.temporary.cleanup)

    def kwargs(self, **extra):
        return {"data_dir": self.data, "config_root": self.home, "runtime_python": sys.executable,
                "skip_dependencies": True, **extra}

    def install(self, host, **extra):
        with patch.object(installation, "_quiesce", return_value=True):
            return installation.install_host(host, **self.kwargs(**extra))

    def test_plans_do_not_create_data_or_change_fakehome(self):
        for host in installation.HOSTS:
            result = installation.plan_install(host, self.data, self.home, provider="codex" if host == "cursor" else None)
            self.assertFalse(result["mutated"])
            self.assertFalse(result["requirements"]["model_called"])
        self.assertFalse(self.data.exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_all_staged_adapters_share_one_core_and_runtime(self):
        stable_release = installation._snapshot()
        with patch.object(installation, "_snapshot", return_value=stable_release):
            receipts = [self.install(host, stage_only=True, provider="codex" if host == "cursor" else None)
                        for host in installation.HOSTS]
        self.assertEqual(len({receipt["core_dir"] for receipt in receipts}), 1)
        self.assertEqual(len({receipt["runtime_python"] for receipt in receipts}), 1)
        self.assertFalse(any(receipt["registered"] for receipt in receipts))
        self.assertEqual(list(self.home.iterdir()), [])
        code = Path(receipts[0]["core_dir"])
        self.assertTrue((code / "blueprint_workflow" / "store.py").is_file())
        self.assertFalse((code / "tests").exists())
        self.assertFalse((code / "local-settings.json").exists())
        for receipt in receipts:
            wrapper = Path(receipt["native_plugin_dir"])
            settings = json.loads((wrapper / "local-settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["core_root"], str(code))
            self.assertEqual(settings["host"], receipt["host"])
            self.assertFalse((wrapper / "blueprint_workflow").exists())

    def test_native_metadata_formats_and_model_selection(self):
        receipts = {host: self.install(host, stage_only=True, provider="claude-code" if host == "cursor" else None, model="chosen-model")
                    for host in installation.HOSTS}
        codex = Path(receipts["codex"]["native_plugin_dir"])
        self.assertEqual(json.loads((codex / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["blueprint-workflow"]["env"]["BLUEPRINT_WORKFLOW_HOST"], "codex")
        claude = Path(receipts["claude-code"]["native_plugin_dir"])
        self.assertTrue((claude / ".claude-plugin" / "plugin.json").is_file())
        server = json.loads((claude / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["blueprint-workflow"]
        self.assertEqual(server["env"]["BLUEPRINT_WORKFLOW_PROVIDER"], "claude-code")
        self.assertEqual(server["env"]["BLUEPRINT_WORKFLOW_MODEL"], "chosen-model")
        hook = json.loads((claude / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn("claude-code", hook)
        self.assertIn("--host", hook)
        cursor = Path(receipts["cursor"]["native_plugin_dir"])
        self.assertEqual(json.loads((cursor / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["blueprint-workflow"]["env"]["BLUEPRINT_WORKFLOW_PROVIDER"], "claude-code")
        gemini = Path(receipts["gemini-cli"]["native_plugin_dir"])
        self.assertTrue((gemini / "gemini-extension.json").is_file())
        self.assertTrue((gemini / "GEMINI.md").is_file())
        self.assertNotIn("---\nname:", (gemini / "GEMINI.md").read_text(encoding="utf-8"))

    def test_codex_interface_icons_are_installed_with_their_manifest(self):
        receipt = self.install("codex", stage_only=True)
        wrapper = Path(receipt["native_plugin_dir"])
        manifest = json.loads((wrapper / "plugin.json").read_text(encoding="utf-8"))
        interface = manifest["extensions"]["com.openai"]["interface"]
        self.assertEqual(interface["displayName"], "Blueprint Workflow")
        self.assertLessEqual(len(interface["shortDescription"]), 30)
        for field in ("logo", "composerIcon"):
            relative = interface[field].removeprefix("./")
            self.assertEqual((wrapper / relative).read_bytes(), (installation.SOURCE_ROOT / relative).read_bytes())
        self.assertEqual(interface, json.loads((installation.SOURCE_ROOT / "plugin.json").read_text(encoding="utf-8"))["extensions"]["com.openai"]["interface"])

    def test_snapshot_prunes_private_and_dependency_directories_before_descent(self):
        source = self.root / "source"
        scripts = source / "scripts"
        (scripts / "nested").mkdir(parents=True)
        manifest = {"name": installation.PLUGIN_NAME, "version": "0.3.0"}
        (source / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        (scripts / "nested" / "launch.py").write_text("print('synthetic')", encoding="utf-8")
        for directory in (source / ".venv", scripts / ".venv", scripts / "__pycache__"):
            directory.mkdir(parents=True)
            (directory / "private.txt").write_text("synthetic private content", encoding="utf-8")
        (scripts / "local-settings.json").write_text("{}", encoding="utf-8")
        visited = []
        original_walk = os.walk

        def audited_walk(*args, **kwargs):
            for current, directories, filenames in original_walk(*args, **kwargs):
                visited.append(Path(current))
                yield current, directories, filenames

        with patch.object(installation, "SOURCE_ROOT", source), patch.object(installation.os, "walk", side_effect=audited_walk):
            actual_manifest, files, _ = installation._snapshot()
        self.assertEqual(actual_manifest, manifest)
        self.assertEqual(set(files), {"plugin.json", "scripts/nested/launch.py"})
        self.assertTrue(visited)
        self.assertFalse(any(part in installation._EXCLUDED for path in visited for part in path.relative_to(source).parts))

    def test_snapshot_rejects_a_link_inside_included_source(self):
        source = self.root / "source"
        scripts = source / "scripts"
        scripts.mkdir(parents=True)
        (source / "plugin.json").write_text(json.dumps({"name": installation.PLUGIN_NAME}), encoding="utf-8")
        outside = self.root / "outside.py"
        outside.write_text("synthetic outside content", encoding="utf-8")
        try:
            (scripts / "linked.py").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Creating a symlink is unavailable in this environment")
        with patch.object(installation, "SOURCE_ROOT", source):
            with self.assertRaisesRegex(ValueError, "links/junctions"):
                installation._snapshot()

    def test_cursor_requires_an_explicit_background_provider(self):
        result = self.install("cursor", stage_only=True)
        self.assertEqual(result["status"], "analysis_configuration_required")
        self.assertFalse(result["mutated"])
        self.assertFalse(self.data.exists())

    def test_an_explicit_unsupported_provider_does_not_register_any_host(self):
        result = self.install("codex", provider="cursor", stage_only=True)
        self.assertEqual(result["status"], "analysis_configuration_required")
        self.assertFalse(result["registered"])
        self.assertFalse(self.data.exists())

    def test_cursor_native_import_preserves_unrelated_plugin(self):
        other = self.home / ".cursor" / "plugins" / "local" / "other-plugin" / "plugin.json"
        other.parent.mkdir(parents=True)
        other.write_text('{"name":"other"}')
        receipt = self.install("cursor", provider="codex")
        self.assertTrue(receipt["registered"])
        self.assertFalse(receipt["native_verified"])
        self.assertTrue(Path(receipt["native_plugin_dir"]).is_relative_to(self.home))
        self.assertEqual(other.read_text(encoding="utf-8"), '{"name":"other"}')
        second = self.install("cursor", provider="codex")
        self.assertEqual(receipt["core_dir"], second["core_dir"])
        self.assertEqual(other.read_text(encoding="utf-8"), '{"name":"other"}')

    def test_unmanaged_cursor_collision_is_not_overwritten(self):
        native = self.home / ".cursor" / "plugins" / "local" / "blueprint-workflow"
        native.mkdir(parents=True)
        manifest = native / "plugin.json"
        manifest.write_text('{"name":"somebody-elses-work"}')
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            self.install("cursor", provider="codex")
        self.assertEqual(manifest.read_text(encoding="utf-8"), '{"name":"somebody-elses-work"}')

    def test_native_registration_uses_isolated_config_environment(self):
        results = []
        def native(command, environment, timeout):
            results.append((command, environment))
            return {"command": command, "returncode": 0}
        with patch.object(installation, "_native_prefix", return_value=["fake-claude.exe"]), patch.object(installation, "_run_native", side_effect=native):
            receipt = self.install("claude-code")
        self.assertTrue(receipt["registered"])
        self.assertEqual(len(results), 2)
        for command, environment in results:
            self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.home / ".claude"))
            self.assertEqual(environment["CODEX_HOME"], str(self.home / ".codex"))
            self.assertEqual(environment["GEMINI_CLI_HOME"], str(self.home))
            self.assertNotIn("--yes", command)
            self.assertNotIn("--consent", command)

    def test_failed_registration_restores_our_catalog(self):
        first = self.install("claude-code", stage_only=True)
        catalog = Path(first["marketplace_dir"]) / ".claude-plugin" / "marketplace.json"
        original = json.loads(catalog.read_text(encoding="utf-8"))
        original["plugins"].append({"name": "other-entry", "source": "./other"})
        catalog.write_text(json.dumps(original))
        before = catalog.read_bytes()
        with patch.object(installation, "_native_prefix", return_value=["fake-claude.exe"]), patch.object(installation, "_run_native", return_value={"returncode": 1, "stderr": "rejected"}):
            with self.assertRaisesRegex(RuntimeError, "registration"):
                self.install("claude-code")
        self.assertEqual(catalog.read_bytes(), before)

    def test_codex_upgrade_preserves_legacy_marketplace_root_and_other_entries(self):
        legacy = self.data / 'marketplaces' / installation.MARKETPLACE_NAME
        catalog = legacy / '.agents' / 'plugins' / 'marketplace.json'
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({'name': installation.MARKETPLACE_NAME, 'plugins': [
            {'name': 'blueprint-workflow', 'source': {'source': 'local', 'path': './plugins/old-version'}},
            {'name': 'other-entry', 'source': {'source': 'local', 'path': './plugins/other'}}]}))
        receipt = self.install('codex', stage_only=True)
        self.assertEqual(receipt['marketplace_dir'], str(legacy))
        entries = json.loads(catalog.read_text(encoding="utf-8"))['plugins']
        self.assertEqual(len([entry for entry in entries if entry['name'] == 'blueprint-workflow']), 1)
        self.assertEqual(next(entry for entry in entries if entry['name'] == 'other-entry')['source']['path'], './plugins/other')

    def test_uninstall_keeps_private_records_exports_and_other_entries(self):
        receipt = self.install("cursor", provider="codex")
        recording = self.data / "workflow.sqlite3"
        recording.write_bytes(b"private-recordings")
        export = self.data / "exports" / "important.txt"
        export.parent.mkdir()
        export.write_text("a real output")
        result = installation.uninstall_host("cursor", self.data, self.home, register_native=False)
        self.assertEqual(result["status"], "uninstalled")
        self.assertEqual(recording.read_bytes(), b"private-recordings")
        self.assertEqual(export.read_text(encoding="utf-8"), "a real output")
        self.assertTrue(Path(receipt["core_dir"]).is_dir())
        self.assertFalse((Path(receipt["native_plugin_dir"]) / "plugin.json").exists())

    def test_upgrade_waits_for_existing_worker_before_writes(self):
        with patch.object(installation, "_quiesce", return_value=False):
            result = installation.install_host("codex", **self.kwargs())
        self.assertEqual(result["status"], "waiting_for_worker")
        self.assertFalse((self.data / 'installations').exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_starting_worker_blocks_runtime_and_native_changes(self):
        import time
        self.data.mkdir()
        (self.data / 'worker-starting.json').write_text(json.dumps({'pid': os.getpid(), 'started_at': time.time()}))
        with patch.object(installation, '_runtime', side_effect=AssertionError('must not update runtime')):
            result = installation.install_host('codex', **self.kwargs())
        self.assertEqual(result['status'], 'waiting_for_worker')
        self.assertFalse((self.data / 'installations').exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_diagnose_does_not_call_models_or_register_hosts(self):
        with patch.object(installation, "_run_native", side_effect=AssertionError("must not run commands")):
            result = installation.diagnose_host("codex", self.data, self.home)
        self.assertFalse(result["mutated"])
        self.assertFalse(result["model_called"])
        self.assertFalse(self.data.exists())

    def test_model_identifier_is_validated_before_planning_or_diagnosis(self):
        for model in ('sk-proj-' + 'a' * 30, 'model; run a command'):
            with self.assertRaises(ValueError):
                installation.plan_install('codex', self.data, self.home, model=model)
        self.assertFalse(self.data.exists())

    def test_raw_windows_shell_shims_are_not_executed(self):
        with patch.object(installation.subprocess, 'run', side_effect=AssertionError('must not run a shell shim')):
            for suffix in ('.cmd', '.bat', '.ps1'):
                with self.assertRaisesRegex(ValueError, 'shell shim'):
                    installation._run_native(['C:/tool & files/tool' + suffix, 'install'], {})

    @unittest.skipUnless(sys.platform == 'win32', 'Windows npm shim mapping')
    def test_gemini_install_and_uninstall_use_verified_node_prefix(self):
        tools = self.root / 'npm host & tools'
        package = tools / 'node_modules' / '@google' / 'gemini-cli'
        package.mkdir(parents=True)
        (package / 'package.json').write_text(json.dumps({'name': '@google/gemini-cli', 'bin': {'gemini': 'cli.js'}}))
        (package / 'cli.js').write_text('/* verified package entrypoint */')
        (tools / 'node.exe').write_bytes(b'fake-native-test-only')
        shim = tools / 'gemini.cmd'
        shim.write_text('@echo should never execute this shim')
        called = []
        def native(command, environment, timeout):
            called.append(command)
            return {'command': command, 'returncode': 0}
        with patch.object(installation.shutil, 'which', side_effect=lambda name: str(shim) if name == 'gemini' else None), patch.object(installation, '_run_native', side_effect=native):
            receipt = self.install('gemini-cli')
            installation.uninstall_host('gemini-cli', self.data, self.home)
        self.assertTrue(receipt['registered'])
        self.assertEqual(len(called), 2)
        self.assertTrue(all(command[:2] == [str((tools / 'node.exe').resolve()), str((package / 'cli.js').resolve())] for command in called))
        self.assertFalse(any(str(shim) in command for command in called))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows hook shell paths')
    def test_windows_hooks_reject_unverified_shell_metacharacter_paths(self):
        with self.assertRaisesRegex(ValueError, 'shell metacharacters'):
            installation._hook_config('claude-code', Path('C:/python.exe'), Path('C:/unsafe&path/plugin'), Path('C:/private'))

    def test_claude_failure_hook_does_not_leak_into_codex_schema(self):
        arguments = (Path(sys.executable), self.root / 'wrapper', self.data)
        claude = installation._hook_config('claude-code', *arguments)['hooks']
        codex = installation._hook_config('codex', *arguments)['hooks']
        self.assertIn('PostToolUseFailure', claude)
        self.assertNotIn('PostToolUseFailure', codex)

    def test_wrapper_selection_overrides_contaminating_parent_environment(self):
        core = self.root / 'fake-core'
        (core / 'scripts').mkdir(parents=True)
        (core / 'scripts' / 'launch.py').write_text(
            'import json, os\nprint(json.dumps({key: os.environ.get(key) for key in '
            '["BLUEPRINT_WORKFLOW_HOST", "BLUEPRINT_WORKFLOW_PROVIDER", "BLUEPRINT_WORKFLOW_MODEL"]}))\n', encoding='utf-8')
        wrapper = self.root / 'native-wrapper'
        wrapper.mkdir()
        (wrapper / 'launch.py').write_text(installation._WRAPPER_LAUNCH, encoding='utf-8')
        for model in (None, 'explicit-model'):
            (wrapper / 'local-settings.json').write_text(json.dumps({'data_dir': str(self.data), 'runtime_python': sys.executable,
                'core_root': str(core), 'host': 'claude-code', 'provider': 'gemini-cli', 'model': model}), encoding='utf-8')
            environment = {**os.environ, 'BLUEPRINT_WORKFLOW_HOST': 'codex', 'BLUEPRINT_WORKFLOW_PROVIDER': 'codex',
                           'BLUEPRINT_WORKFLOW_MODEL': 'stale-parent-model'}
            result = subprocess.run([sys.executable, str(wrapper / 'launch.py')], env=environment,
                                    capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            selected = json.loads(result.stdout)
            self.assertEqual(selected['BLUEPRINT_WORKFLOW_HOST'], 'claude-code')
            self.assertEqual(selected['BLUEPRINT_WORKFLOW_PROVIDER'], 'gemini-cli')
            self.assertEqual(selected['BLUEPRINT_WORKFLOW_MODEL'], model)


if __name__ == "__main__":
    unittest.main()
