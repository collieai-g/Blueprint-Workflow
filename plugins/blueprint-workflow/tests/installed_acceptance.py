"""Opt-in installed-package acceptance in the currently authorized local host.

This is separate from live_replay.py's restricted CLI sandbox. It never changes
that sandbox or its policy. The current Codex host runs the reviewed generated
helper using its already-authorized normal local execution tools.
"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from mcp import Client, StdioServerParameters


async def main(installed: Path, directory: Path):
    learned = json.loads((directory / "generated-workflow.json").read_text(encoding="utf-8"))
    work = directory / "current-host-replay"
    work.mkdir(exist_ok=True)
    source, output = work / "new-orders.json", work / "new-report.json"
    rows = [{"sku": "X", "quantity": 3, "unit_price": "7.25", "active": True},
            {"sku": "Y", "quantity": 2, "unit_price": "1.50", "active": False},
            {"sku": "Z", "quantity": 4, "unit_price": "2.00", "active": True}]
    source.write_text(json.dumps(rows), encoding="utf-8")
    environment = dict(os.environ, BLUEPRINT_WORKFLOW_AUTOSTART="0", BLUEPRINT_WORKFLOW_DATA_DIR=str(directory / "recordings"), PYTHONUTF8="1")
    environment.pop("BLUEPRINT_WORKFLOW_PYTHON", None)
    params = StdioServerParameters(command="python", args=[str(installed / "scripts" / "launch.py"), "mcp"], env=environment)
    started = time.monotonic()
    async with Client(params, read_timeout_seconds=20) as client:
        async def call(name, arguments):
            result = await client.call_tool(name, arguments)
            if result.is_error:
                raise RuntimeError(str(result.content))
            return result.structured_content

        tool_names = sorted(tool.name for tool in (await client.list_tools()).tools)
        record = await call("workflow_read", {"workflow_id": learned["workflow_id"], "version": 1})
        workflow = record["workflow"]
        bundle = await call("workflow_materialize", {"workflow_id": learned["workflow_id"], "version": 1})
        helper = Path(bundle["directory"]) / "helpers" / "order_report.py"
        # The current Codex agent reviewed this exact generated source before
        # running this test. A hash comparison prevents a different file running.
        generated = next(item for item in workflow["helpers"] if item["name"] == "order_report.py")
        assert helper.read_text(encoding="utf-8") == generated["content"]
        run = await call("workflow_run_start", {"workflow_id": record["workflow_id"], "version": 1,
            "inputs": {"source_path": str(source), "output_path": str(output)}})
        run_id = run["id"]
        computed = None
        evidence = None
        for _ in range(10):
            state = await call("run_next", {"run_id": run_id})
            if state["status"] == "awaiting_verification":
                break
            assert state["status"] == "step_ready", state["status"]
            step = state["step"]
            if step["id"] == "read_validate":
                process = subprocess.run([sys.executable, str(helper), "--source", str(source), "--output", str(output)],
                    capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=20,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                assert process.returncode == 0, process.stderr
                helper_result = json.loads(process.stdout)
                assert helper_result["verified"] is True
                observed_rows = json.loads(source.read_text(encoding="utf-8"))
                assert all(type(row["quantity"]) is int and type(row["active"]) is bool
                           and isinstance(row["sku"], str) and isinstance(row["unit_price"], str)
                           and Decimal(row["unit_price"]).is_finite() and Decimal(row["unit_price"]).as_tuple().exponent >= -2
                           for row in observed_rows)
                computed = {"total": format(sum((Decimal(row["unit_price"]) * row["quantity"] for row in observed_rows if row["active"]), Decimal("0.00")), ".2f"),
                    "counted_items": sum(row["active"] for row in observed_rows), "excluded_items": sum(not row["active"] for row in observed_rows)}
                observed = json.loads(output.read_text(encoding="utf-8"))
                assert computed == observed == {"total": "29.75", "counted_items": 2, "excluded_items": 1}
                evidence = [{"source_path": str(source), "output_path": str(output),
                    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                    "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
                    "description": "The current authorized host executed the reviewed generated helper, read both files, and independently recomputed Decimal totals and record counts."}]
                outputs = dict(zip(step["outputs"], [observed_rows, len(observed_rows)], strict=True))
            elif step["id"] == "calculate":
                assert computed is not None
                outputs = dict(zip(step["outputs"], [computed["total"], {"counted_items": 2, "excluded_items": 1}, computed], strict=True))
            elif step["id"] == "write_verify":
                observed = json.loads(output.read_text(encoding="utf-8"))
                assert observed == computed
                outputs = dict(zip(step["outputs"], [str(output), {"verified": True, "report": observed}], strict=True))
            else:
                raise RuntimeError("This opt-in scenario runner has not reviewed this generated step: " + step["id"])
            await call("run_report", {"run_id": run_id, "step_id": step["id"], "result": {
                "status": "succeeded", "observation": "Current source, helper execution and output were actually checked for this run.",
                "evidence": evidence, "outputs": outputs}})
        else:
            raise RuntimeError("Unexpected workflow length")
        verified = await call("run_verify", {"run_id": run_id, "verification": {
            "status": "passed", "observation": "New-input execution produced29.75 and correct2/1 counts, independently recomputed by the acceptance harness.",
            "checks": [{"criterion": criterion, "status": "passed", "evidence": evidence} for criterion in workflow["completion_criteria"]]}})
        assert verified["status"] == "verified"
        after = await call("workflow_read", {"workflow_id": record["workflow_id"], "version": 1})
        assert after["test_status"] == "passed_agent_reported"
        final_bundle = await call("workflow_materialize", {"workflow_id": record["workflow_id"], "version": 1})
        # Invoke the installed hook adapter against a separate exact-host task;
        # this does not claim Codex has trusted its automatic lifecycle hooks.
        hook_host = "installed-hook-smoke"
        smoke = await call("session_start", {"title": "Installed hook smoke", "goal": "Verify exact-host text capture", "host_session_id": hook_host})
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": hook_host, "turn_id": "installed-smoke-1", "prompt": "Synthetic hook test: only count active records."}
        invoked = subprocess.run(["python", str(installed / "scripts" / "launch.py"), "hook"], input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", env=environment, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        assert invoked.returncode == 0
        assert smoke["session_id"] in invoked.stdout
        events = await call("session_events", {"session_id": smoke["session_id"]})
        assert any(item.get("details", {}).get("source") == "host_user_prompt" for item in events["events"])
        await call("session_pause", {"session_id": smoke["session_id"]})
        report = {"version": json.loads((installed / "plugin.json").read_text(encoding="utf-8"))["version"],
                  "installed_root": str(installed), "mcp_tools": tool_names, "workflow_id": record["workflow_id"],
                  "run_id": run_id, "test_status": after["test_status"], "observed": computed,
                  "helper_execution": "passed in currently authorized local Codex host",
                  "independent_harness_comparison": "passed", "installed_hook_adapter": "passed direct invocation",
                  "native_hook_trust": "not changed; user/host review required",
                  "restricted_fresh_cli_replay": "failed at shell launch by its existing execution policy; policy unchanged",
                  "materialized": final_bundle, "seconds": round(time.monotonic() - started, 3)}
        (directory / "installed-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "materialized"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--validation-root", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.installed_root.resolve(), args.validation_root.resolve()))
