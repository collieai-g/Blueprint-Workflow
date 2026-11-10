"""Real stdio for all four staged adapters, not native host UI acceptance."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters
from blueprint_workflow.installation import HOSTS, install_host
from blueprint_workflow.service import TOOL_NAMES


class HostLauncherTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_generated_adapters_connect_to_the_same_core(self):
        with tempfile.TemporaryDirectory(prefix="blueprint-host-launch-") as directory:
            root = Path(directory)
            data = root / "private"
            home = root / "fake-home"
            receipts = [install_host(host, data_dir=data, config_root=home, provider="codex",
                                     runtime_python=sys.executable, stage_only=True, skip_dependencies=True)
                        for host in HOSTS]
            self.assertEqual(len({r["core_dir"] for r in receipts}), 1)
            for receipt in receipts:
                host = receipt["host"]
                wrapper = Path(receipt["native_plugin_dir"])
                environment = dict(os.environ, BLUEPRINT_WORKFLOW_AUTOSTART="0", PYTHONUTF8="1")
                for key in list(environment):
                    if key.startswith("BLUEPRINT_WORKFLOW_") and key != "BLUEPRINT_WORKFLOW_AUTOSTART":
                        environment.pop(key)
                params = StdioServerParameters(command=sys._base_executable,
                                               args=[str(wrapper / "launch.py"), "mcp"], env=environment)
                async with Client(params, read_timeout_seconds=30) as client:
                    self.assertEqual({t.name for t in (await client.list_tools()).tools}, set(TOOL_NAMES))
                    result = await client.call_tool("session_start", {"title": host + " real stdio",
                        "goal": "Verify generated wrapper identity and common persisted tools", "host_session_id": "identical-id"})
                    self.assertFalse(result.is_error, str(result.content))
                    task = result.structured_content
                    self.assertEqual(task["analysis_provider"], "codex")
                    self.assertEqual(task["host_session_id"], host + ":identical-id")
                    state = await client.call_tool("analysis_providers", {})
                    self.assertFalse(state.is_error)
                    self.assertEqual(state.structured_content["host"], host)
                    self.assertFalse(state.structured_content["model_called"])
            self.assertFalse((home / ".cursor").exists())


if __name__ == "__main__":
    unittest.main()
