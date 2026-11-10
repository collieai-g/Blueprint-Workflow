import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters

from blueprint_workflow.mcp_server import build_server
from blueprint_workflow.service import TOOL_NAMES


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_and_structured_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            server = build_server(directory, autostart=False)
            async with Client(server) as client:
                listed = await client.list_tools()
                self.assertEqual({tool.name for tool in listed.tools}, set(TOOL_NAMES))
                result = await client.call_tool("session_start", {"title": "文字任務", "goal": "整理並產生報告", "host_session_id": "isolated-test"})
                self.assertFalse(result.is_error)
                started = result.structured_content
                session_id = started["session_id"]
                append = await client.call_tool("event_append", {"session_id": session_id, "events": [{"actor": "agent", "kind": "observation", "summary": "已讀取兩份來源資料", "client_event_id": "phase-1"}]})
                self.assertFalse(append.is_error)
                state = (await client.call_tool("session_status", {"session_id": session_id})).structured_content
                self.assertGreaterEqual(state["latest_seq"], 1)
                self.assertEqual(state["processed_seq"], 0)
                unknown = await client.call_tool("event_append", {"session_id": "does-not-exist", "events": [{"actor": "agent", "kind": "observation", "summary": "x"}]})
                self.assertTrue(unknown.is_error)

    async def test_actual_stdio_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parent.parent
            environment = dict(os.environ, BLUEPRINT_WORKFLOW_AUTOSTART="0", BLUEPRINT_WORKFLOW_PYTHON=sys.executable, PYTHONUTF8="1")
            # Use the base interpreter to force the same runtime re-exec as an
            # installed plugin, rather than accidentally skipping that layer.
            params = StdioServerParameters(command=sys._base_executable, args=[str(root / "scripts" / "launch.py"), "--data-dir", directory, "mcp"], env=environment)
            async with Client(params, read_timeout_seconds=20) as client:
                tools = await client.list_tools()
                self.assertIn("session_finish", {tool.name for tool in tools.tools})
                result = await client.call_tool("session_start", {"title": "stdio smoke", "goal": "Verify an actual local tool call"})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["goal"], "Verify an actual local tool call")


if __name__ == "__main__":
    unittest.main()
