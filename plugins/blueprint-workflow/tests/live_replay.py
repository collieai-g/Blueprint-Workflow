"""Opt-in fresh Codex execution of a genuinely generated JSON-report workflow."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from blueprint_workflow.store import Store
from blueprint_workflow.privacy import sanitize


def main(directory: Path) -> int:
    store = Store(directory / "recordings")
    sessions = store.list_sessions()
    finalized = next(session for session in sessions if session["status"] == "ready_to_test")
    workflow_id = finalized["workflow_id"]
    work = directory / "fresh-agent-replay"
    work.mkdir(exist_ok=True)
    source = work / "new-orders.json"
    output = work / "new-report.json"
    rows = [{"sku": "X", "quantity": 3, "unit_price": "7.25", "active": True},
            {"sku": "Y", "quantity": 2, "unit_price": "1.50", "active": False},
            {"sku": "Z", "quantity": 4, "unit_price": "2.00", "active": True}]
    source.write_text(json.dumps(rows), encoding="utf-8")
    final_path = work / "agent-result.json"
    if final_path.exists():
        history = work / "history" / str(time.time_ns())
        history.mkdir(parents=True)
        for name in ("agent-result.json", "execution-trace.json", "agent-stderr.txt"):
            if (work / name).exists():
                shutil.copy2(work / name, history / name)
    schema_path = work / "agent-result.schema.json"
    schema_path.write_text(json.dumps({"type": "object", "properties": {"run_id": {"type": "string"},
        "completed": {"type": "boolean"}, "output_path": {"type": "string"}, "observed_total": {"type": "string"}},
        "required": ["run_id", "completed", "output_path", "observed_total"], "additionalProperties": False}), encoding="utf-8")
    config = {
        "mcp_servers.blueprint-workflow.command": sys.executable,
        "mcp_servers.blueprint-workflow.args": [str(ROOT / "scripts" / "launch.py"), "--data-dir", str(directory / "recordings"), "mcp"],
        "mcp_servers.blueprint-workflow.env": {"BLUEPRINT_WORKFLOW_AUTOSTART": "0", "BLUEPRINT_WORKFLOW_PYTHON": sys.executable, "PYTHONUTF8": "1"},
        "mcp_servers.blueprint-workflow.required": True,
        "approval_policy": "never", "web_search": "disabled", "project_doc_max_bytes": 0,
    }
    def toml(value):
        if isinstance(value, dict):
            return "{" + ", ".join(json.dumps(k) + "=" + toml(v) for k, v in value.items()) + "}"
        return json.dumps(value, ensure_ascii=False)
    command = [shutil.which("codex"), "exec", "--ephemeral", "--ignore-user-config", "--sandbox", "workspace-write",
               "--skip-git-repo-check", "--json", "--color", "never", "--cd", str(work),
               "--output-schema", str(schema_path), "--output-last-message", str(final_path)]
    for key, value in config.items():
        command.extend(["-c", key + "=" + toml(value)])
    command.append("-")
    prompt = f"""This is an authorized isolated acceptance test with synthetic local data. You are a NEW foreground Codex Agent, executing an existing learned workflow, not designing one.
Use the blueprint-workflow MCP tools. First workflow_read workflow_id={workflow_id}. Read its fixed instructions and input definitions. Start a NEW replay run with this new source {source} and new output {output}; select the matching input names from the workflow. Use version 1 explicitly.
Use run_next, carry out the ACTUAL returned step using your shell/Python tools, read actual results, then run_report with the required named outputs, actual observation and evidence. Do not simply claim all steps succeeded, and do not fabricate a workflow/run. All file writes must stay inside {work}.
Use the installed standard Python through the command `python` on PATH. For exec_command use the installed system shell explicitly: shell="C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", login=false. The cached optional pwsh launcher is unavailable in this sandbox. You may create a short script in this test directory when useful. Use normal commands within the supplied sandbox and do not request escalated permissions or change its policy. Substitute current run.inputs and use this run's step outputs. You may inspect the current files. Do not access accounts, credentials, network services or unrelated files. Do not change the fixed workflow or start another learning session.
After steps reach awaiting_verification, read back the actual output, check EVERY completion_criteria from workflow_read, then run_verify with exact criteria and current evidence. This is agent-reported verification; do not call it independent.
Return the real run_id, whether completed, actual output_path and observed_total using the requested schema. If something cannot be completed, return completed=false and preserve the actual run status. Never overwrite a failed report with invented success.
"""
    env = dict(os.environ, BLUEPRINT_WORKFLOW_AUTOSTART="0", PYTHONUTF8="1")
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        env.pop(name, None)
    started = time.monotonic()
    result = subprocess.run(command, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                            timeout=480, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    # The product and its acceptance report do not save reasoning streams.
    trace = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (event.get("item") or {}).get("type") == "reasoning":
            continue
        trace.append(event)
    (work / "execution-trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    (work / "agent-stderr.txt").write_text(str(sanitize(result.stderr))[-20000:], encoding="utf-8")
    if result.returncode != 0 or not final_path.exists():
        raise RuntimeError("Replay Codex failed: " + result.stderr[-3000:])
    response = json.loads(final_path.read_text(encoding="utf-8"))
    run = store.run_status(response["run_id"])
    if not response["completed"] or not output.exists():
        report = {"completed": False, "response": response, "run_status": run["status"],
                  "reason": "The real Agent did not complete the workflow; its actual failed step report remains persisted."}
        (directory / "replay-failed-attempt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 3
    observed = json.loads(output.read_text(encoding="utf-8"))
    expected = {"total": "29.75", "counted_items": 2, "excluded_items": 1}
    if observed != expected or run["status"] != "verified" or not response["completed"]:
        raise AssertionError({"observed": observed, "expected": expected, "run_status": run["status"], "response": response})
    record = store.get_workflow(workflow_id)
    report = {"scenario": "A fresh real Codex exec Agent used the MCP workflow to process a different input file",
              "seconds": round(time.monotonic() - started, 3), "workflow_id": workflow_id, "run_id": run["id"],
              "observed": observed, "independent_harness_comparison": "passed", "test_status": record["test_status"],
              "verification_provenance": run.get("verification_provenance"), "mcp_tool_calls": [event["item"].get("tool") for event in trace if event.get("type") == "item.completed" and (event.get("item") or {}).get("type") == "mcp_tool_call"]}
    (directory / "replay-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    raise SystemExit(main(parser.parse_args().data_dir.resolve()))
