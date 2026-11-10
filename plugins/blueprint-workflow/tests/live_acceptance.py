"""Opt-in real Codex/background acceptance; never part of the unit-test suite.

Uses synthetic input files and real generated artifacts. The background analysis
must use Codex, not a canned workflow. Run from the source root with --data-dir
pointing outside the source tree. State is persisted so a failed provider can be
fixed and the same demonstration resumed.
"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import Client
from blueprint_workflow.mcp_server import build_server
from blueprint_workflow.worker import stop_worker


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def calculate(rows, active_only):
    included = [row for row in rows if row["active"] or not active_only]
    return {"total": str(sum((Decimal(row["unit_price"]) * row["quantity"] for row in included), Decimal("0.00"))),
            "counted_items": len(included), "excluded_items": len(rows) - len(included)}


async def main(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "demonstration-orders.json"
    output = directory / "demonstration-report.json"
    state_path = directory / "acceptance-state.json"
    rows = [{"sku": "A", "quantity": 2, "unit_price": "10.00", "active": True},
            {"sku": "B", "quantity": 1, "unit_price": "8.00", "active": False},
            {"sku": "C", "quantity": 3, "unit_price": "4.50", "active": True}]
    write_json(source, rows)
    rows = json.loads(source.read_text(encoding="utf-8"))
    assert all(isinstance(row["quantity"], int) and not isinstance(row["quantity"], bool)
               and isinstance(row["active"], bool) and Decimal(row["unit_price"]).is_finite() for row in rows)
    metrics = []
    async with Client(build_server(directory / "recordings")) as client:
        async def call(name, arguments):
            started = time.monotonic()
            response = await client.call_tool(name, arguments)
            metrics.append({"tool": name, "seconds": round(time.monotonic() - started, 4)})
            if response.is_error:
                raise RuntimeError(str(response.content))
            return response.structured_content

        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            sid = state["session_id"]
        else:
            state = await call("session_start", {"title": "真實模型增量學習驗收（合成資料）",
                "goal": "讀取 JSON 訂單資料，按照已確認規則計算總額，寫出可核對的 JSON 報告；日後換來源及輸出位置可重做。",
                "host_session_id": "live-acceptance-" + directory.name})
            sid = state["session_id"]
            write_json(state_path, {"session_id": sid, "data_dir": str(directory)})

        async def wait_for(seq, revision, final=False):
            deadline = time.monotonic() + 360
            last_print = None
            while time.monotonic() < deadline:
                result = await call("session_status", {"session_id": sid, "include_draft": True})
                marker = (result["status"], result["draft_revision"], result["processed_seq"], result.get("retry_attempts"))
                if marker != last_print:
                    print(json.dumps({"progress": marker, "latest_seq": result["latest_seq"], "last_error": result.get("last_error")}, ensure_ascii=False), flush=True)
                    last_print = marker
                if result.get("last_error"):
                    raise RuntimeError(result["last_error"])
                if result["status"] in {"blocked", "analysis_failed"}:
                    raise RuntimeError(json.dumps(result, ensure_ascii=False))
                if result["processed_seq"] >= seq and result["draft_revision"] >= revision and (not final or result["status"] == "ready_to_test"):
                    return result
                await asyncio.sleep(2)
            raise TimeoutError("Real model did not catch up within the acceptance timeout")

        current = await call("session_status", {"session_id": sid})
        if current["status"] == "ready_to_test":
            final = current
        else:
            initial = calculate(rows, False)
            events = await call("event_append", {"session_id": sid, "events": [
                {"client_event_id": "demo-rule-initial", "kind": "instruction", "actor": "human",
                 "summary": "這是明確標示的合成驗收任務。第一階段先讀 JSON 訂單，全部商品按 quantity × unit_price 加總，使用 Decimal 金額；只使用本機 Python，不需新服務或軟件。",
                 "details": {"source": "explicit_user_confirmation", "input_schema": "JSON array of {sku:string,quantity:integer,unit_price:decimal string,active:boolean}",
                             "source_path": str(source), "output_path": str(output),
                             "output_schema": "{total:decimal string with 2 places,counted_items:integer,excluded_items:integer}",
                             "runtime_inputs": "本次輸入檔案和輸出檔案路徑是可變輸入；每次讀取新檔案，不沿用示範資料或總額。"}},
                {"client_event_id": "demo-source-read", "kind": "observation", "actor": "agent", "summary": "已實際讀取來源並核對三筆商品記錄，第一階段所有商品總額為 41.50。",
                 "details": {"source_path": str(source), "observed_rows": rows, "computed_report": initial}}
            ]})
            initial_id = events["events"][0]["id"]
            phase1 = await wait_for(events["latest_seq"], 1)
            write_json(directory / "draft-before-correction.json", phase1)
            # The foreground has a genuine output while the separate designer
            # has already persisted its first revision.
            write_json(directory / "foreground-initial-report.json", initial)

            report = calculate(rows, True)
            write_json(output, report)
            assert json.loads(output.read_text(encoding="utf-8")) == report
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            corrected = await call("event_append", {"session_id": sid, "events": [
                {"client_event_id": "demo-correction", "kind": "correction", "actor": "human", "corrects_event_id": initial_id,
                 "summary": "合成驗收的第二階段明確更正：今次及以後都只計 active=true 的商品；active=false 必須排除。其餘環境、輸入格式及輸出格式不變。這是已確認固定規則，毋須再次詢問。",
                 "details": {"source": "explicit_user_confirmation", "confirmed_rule": "Only include active=true items; fixed reusable rule"}},
                {"client_event_id": "demo-corrected-result", "kind": "observation", "actor": "agent",
                 "summary": "已按更正規則重新計算、寫出並讀回報告：total=33.50、counted_items=2、excluded_items=1；與原始三筆資料逐筆核對一致。",
                 "details": {"path": str(output), "sha256": digest, "observed_report": report, "verification": "讀回實際 JSON；Decimal 計算和納入/排除筆數全部一致。輸出存在即為本任務成品，不需發布。"}}
            ]})
            corrected = await call("event_append", {"session_id": sid, "events": [{
                "client_event_id": "demo-input-scope-and-helper", "kind": "instruction", "actor": "human",
                "summary": "合成驗收補充確認：此固定工作流的unit_price輸入只接受最多兩位小數的有限Decimal字串；超出範圍要回報輸入不合規格而不產生成功報告，毋須捨入，沒有待決捨入規則。請把可重用計算及輸出做成Python輔助程式，來源與輸出路徑用命令列參數，沿用已確認Python環境。",
                "details": {"source": "explicit_user_confirmation", "answers_question": "rounding_rule", "helper_requested": True}
            }]})
            phase2 = await wait_for(corrected["latest_seq"], 2)
            write_json(directory / "draft-after-correction.json", phase2)
            started = time.monotonic()
            final_request = await call("session_finish", {"session_id": sid,
                "final_summary": "合成驗收示範已100%完成。最終確認只計active=true，以Decimal計算，來源/輸出路徑每次可換；JSON成品已寫入及讀回核對。請沿用目前草稿定稿，沒有未決業務規則。",
                "artifacts": [{"path": str(output), "description": "已實際讀回核對的JSON報告：33.50，納入2筆，排除1筆。"}],
                "draft_review": {"draft_revision": phase2["draft_revision"], "through_seq": phase2["processed_seq"],
                    "checks": [{"criterion": criterion, "evidence": [{"path": str(output), "sha256": digest,
                        "description": "前台已實際重讀來源與輸出；只計active=true，Decimal總額33.50，納入2筆排除1筆，已逐筆核對。"}]}
                        for criterion in phase2["draft"]["completion_criteria"]]}})
            final = await wait_for(final_request["latest_seq"], 3, final=True)
            final["acceptance_finalization_seconds"] = round(time.monotonic() - started, 3)
        workflow = await call("workflow_read", {"workflow_id": final["workflow_id"]})
        exported = await call("workflow_export", {"workflow_id": final["workflow_id"], "format": "markdown"})
        (directory / "generated-workflow.md").write_text(exported["content"], encoding="utf-8")
        write_json(directory / "generated-workflow.json", workflow)
        write_json(directory / "acceptance-metrics.json", {"session": final, "calls": metrics,
            "recording_call_max_seconds": max((x["seconds"] for x in metrics if x["tool"] == "event_append"), default=0),
            "replay_status": "pending_separate_agent_execution", "scenario": "synthetic JSON task, real Codex background analysis"})
        print(json.dumps({"workflow_id": final["workflow_id"], "data_dir": str(directory), "revision": final["draft_revision"],
                          "finalization_seconds": final.get("acceptance_finalization_seconds"), "test_status": workflow["test_status"]}, ensure_ascii=False), flush=True)
    stop_worker(directory / "recordings")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.data_dir.resolve()))
