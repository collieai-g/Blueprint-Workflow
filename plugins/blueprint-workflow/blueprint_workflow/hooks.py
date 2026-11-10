"""Native host event adapters. Capture only an explicitly active, exact-host task.

Hooks never analyze, start recording, scrape transcripts, capture screens, change
tool arguments, or block an operation. Semantic checkpoints supplied by the
foreground agent remain the primary record of intent and interpretation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
from typing import Any, Callable
from .runtime import canonical_host, current_host, scope_host_id

MAX_HOOK_BYTES = 8_388_608
MAX_TEXT = 8_000
_BINARY_FIELDS = {"image_url", "image_data", "audio_url", "audio_data", "base64", "blob", "bytes"}
_MEDIA_TYPES = {"image", "audio", "video", "image_url", "input_image", "input_audio"}


def without_media(value: Any, depth: int = 0, *, bounded: bool = True) -> Any:
    """Remove media; durable capture preserves available text before previewing."""
    if depth > (12 if bounded else 32):
        return "[nested content omitted]"
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() in _MEDIA_TYPES:
            return {"type": "omitted_media"}
        clean = {}
        items = list(value.items())[:80] if bounded else value.items()
        for key, item in items:
            key = str(key)
            if key.lower() in _BINARY_FIELDS:
                continue
            if key.lower() == "data" and str(value.get("mimeType", value.get("mime_type", ""))).startswith(("image/", "audio/", "video/")):
                continue
            clean[key] = without_media(item, depth + 1, bounded=bounded)
        return clean
    if isinstance(value, (list, tuple)):
        clean = [without_media(item, depth + 1, bounded=bounded) for item in (value[:40] if bounded else value)]
        if bounded and len(value) > 40:
            clean.append("[additional items omitted]")
        return clean
    if isinstance(value, str):
        # Cursor serializes tool results as JSON text. Scrub its structured
        # contents before they become an opaque string containing media blobs.
        if value.lstrip().startswith(("{", "[")) and len(value) <= MAX_HOOK_BYTES:
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return without_media(decoded, depth + 1, bounded=bounded)
        if value.lstrip().lower().startswith(("data:image/", "data:audio/", "data:video/")):
            return "[media omitted]"
        if len(value) > 2_048 and re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", value):
            return "[binary content omitted]"
        return value if not bounded or len(value) <= MAX_TEXT else value[:MAX_TEXT] + " [truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_TEXT]


def _own_tool(name: str, tool_input: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    if "blueprintworkflow" in normalized:
        return True
    # CLI fallback is the same plugin operation, not another business action.
    if name.lower() in {"bash", "exec_command", "shell", "shell_command"}:
        text = json.dumps(tool_input, ensure_ascii=False).replace("\\", "/").lower()
        return "blueprint_workflow" in text or ("scripts/launch.py" in text and "blueprint-workflow" in text)
    return False


def _context(event_name: str, host_id: str, active: dict | None) -> dict:
    text = (
        f"Blueprint Workflow host_session_id={host_id}. "
        "Use this exact host_session_id in this plugin's session tools. "
        "Start a session only when the user explicitly asks to record/learn a workflow."
    )
    if active:
        session_id = active.get("session_id") or active.get("id")
        capture_state = active.get("capture_state", active["status"])
        text += f" Current workflow session_id={session_id}, state={active['status']}, capture_state={capture_state}."
        if active.get("analysis_state"):
            text += f" Background analysis_state={active['analysis_state']}."
        if capture_state == "recording":
            text += (
                " Keep working normally. After meaningful phases, decisions, corrections and results, "
                "use event_append to save concise semantic checkpoints with their actual evidence. "
                "The independent background worker updates the draft. Do not invent results, "
                "read hidden reasoning, or save images. Check session_status when recovering context."
            )
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}


def _event_key(payload: dict, event_name: str, summary: str) -> str:
    stable = payload.get("tool_use_id") or payload.get("tool_call_id")
    if event_name not in {"PreToolUse", "PostToolUse"}:
        stable = stable or payload.get("turn_id")
    # A turn can call the same tool several times. Without a call ID, a fresh
    # invocation identity is safer than falsely merging different operations.
    # No stable identity: distinct invocations/instructions must remain distinct.
    stable = stable or uuid.uuid4().hex
    text = json.dumps([payload.get("session_id"), event_name, stable, payload.get("tool_name"), summary], ensure_ascii=False)
    return str(payload.get("_host", "codex")).replace("-", "_") + "_" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _handle_canonical_hook(
    payload: dict,
    data_dir: Path | str | None = None,
    *,
    store: Any = None,
    worker_launcher: Callable | None = None,
) -> dict:
    """Return Codex hook output; dependency injection keeps tests fully local."""
    if os.environ.get("BLUEPRINT_WORKFLOW_ANALYZER") == "1":
        return {}
    host_id = payload.get("session_id")
    event_name = payload.get("hook_event_name")
    if not isinstance(host_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,280}", host_id):
        return {}
    if event_name not in {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"}:
        return {}
    if data_dir is None:
        from .paths import default_data_dir
        data_dir = default_data_dir()
    if store is None:
        from .store import Store
        store = Store(data_dir)
    active = store.find_session_by_host(host_id)
    # Only pre-0.2 Codex used unscoped bindings. Never fall back across hosts.
    if not active and payload.get("_host") == "codex" and host_id.startswith("codex:"):
        active = store.find_session_by_host(host_id[len("codex:"):])
    output = _context(event_name, host_id, active) if event_name in {"SessionStart", "UserPromptSubmit", "PreCompact"} else {}
    if not active or active.get("capture_state", active.get("status")) != "recording":
        return output
    durable_capture = hasattr(store, "capture_enqueue")
    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input", {})
    if event_name in {"PreToolUse", "PostToolUse"} and _own_tool(tool_name, tool_input):
        return output
    event: dict | None = None
    if event_name == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            event = {"kind": "instruction", "actor": "human", "summary": prompt[:4_000], "source": "host_user_prompt", "details": {"text": without_media(prompt, bounded=not durable_capture), "source": "host_user_prompt", "capture": "codex_hook"}}
    elif event_name == "PreToolUse":
        clean_input = without_media(tool_input, bounded=not durable_capture)
        event = {"kind": "tool_call", "actor": "agent", "summary": f"Called {tool_name}", "details": {"tool": tool_name, "input": clean_input, "source": "codex_hook"}}
    elif event_name == "PostToolUse":
        result = payload.get("tool_response", payload.get("tool_output", payload.get("result")))
        event = {"kind": "tool_result", "actor": "tool", "summary": f"Result from {tool_name}", "details": {"tool": tool_name, "result": without_media(result, bounded=not durable_capture), "source": "codex_hook"}}
    elif event_name == "PreCompact":
        event = {"kind": "checkpoint", "actor": "agent", "summary": "Codex context compaction started; recover the durable workflow draft before continuing.", "details": {"source": "codex_hook", "trigger": str(payload.get("trigger", "unknown"))}}
    if event is None:
        return output
    capture = str(payload.get("_host", "codex")) + "_hook"
    if event["details"].get("source") == "codex_hook":
        event["details"]["source"] = capture
    if "capture" in event["details"]:
        event["details"]["capture"] = capture
    if event_name == "PreCompact":
        event["summary"] = f"{payload.get('_host', 'codex')} context compaction started; recover the durable workflow draft before continuing."
    if payload.get("agent_id"):
        event["details"]["agent_id"] = str(payload["agent_id"])[:256]
    if payload.get("tool_use_id"):
        event["details"]["tool_use_id"] = str(payload["tool_use_id"])[:256]
    if payload.get("tool_call_id"):
        event["details"]["tool_call_id"] = str(payload["tool_call_id"])[:256]
    if payload.get("turn_id"):
        event["details"]["turn_id"] = str(payload["turn_id"])[:256]
    event["client_event_id"] = _event_key(payload, str(event_name), event["summary"])
    session_id = active.get("session_id") or active.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Host binding does not contain a workflow session_id")
    if durable_capture:
        try:
            store.capture_enqueue(session_id, [event])
        except Exception:
            if hasattr(store, "record_capture_gap"):
                store.record_capture_gap(session_id, "enqueue_failed")
            raise
    else:
        store.append_events(session_id, [event])
    if worker_launcher is None:
        from .worker import ensure_worker
        worker_launcher = ensure_worker
    try:
        worker_launcher(data_dir)
    except Exception:
        output["systemMessage"] = "藍圖工作流記錄已保存，但背景分析未能啟動；請查看 session_status 以重試。"
    return output


_HOST_EVENTS = {
    "codex": {name: name for name in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact")},
    "claude-code": {**{name: name for name in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact")}, "PostToolUseFailure": "PostToolUse"},
    "gemini-cli": {"SessionStart": "SessionStart", "BeforeAgent": "UserPromptSubmit", "BeforeTool": "PreToolUse", "AfterTool": "PostToolUse", "PreCompress": "PreCompact"},
    "cursor": {"sessionStart": "SessionStart", "beforeSubmitPrompt": "UserPromptSubmit", "preToolUse": "PreToolUse", "postToolUse": "PostToolUse", "postToolUseFailure": "PostToolUse", "preCompact": "PreCompact"},
}


def handle_hook(payload: dict, data_dir: Path | str | None = None, *, host: str | None = None,
                store: Any = None, worker_launcher: Callable | None = None) -> dict:
    """Normalize documented payloads; ignore transcripts and model-thought hooks."""
    selected_host = canonical_host(host) if host else current_host()
    name = selected_host or "codex"
    native_event = payload.get("hook_event_name")
    event = _HOST_EVENTS[name].get(native_event)
    if not event:
        return {}
    raw_id = payload.get("conversation_id", payload.get("session_id")) if name == "cursor" else payload.get("session_id")
    try:
        scoped_id = scope_host_id(raw_id, selected_host)
    except ValueError:
        return {}
    # Copy only supported textual fields, never transcript_path, email or thoughts.
    normalized = {key: payload[key] for key in ("prompt", "tool_name", "tool_input", "tool_response", "tool_output", "result", "tool_use_id", "tool_call_id", "turn_id", "agent_id", "trigger") if key in payload}
    normalized.update(session_id=scoped_id, hook_event_name=event, _host=name)
    if name == "cursor" and payload.get("generation_id"):
        normalized["turn_id"] = str(payload["generation_id"])
    if name == "gemini-cli" and payload.get("timestamp"):
        normalized.setdefault("turn_id", str(payload["timestamp"]))
    if native_event == "postToolUseFailure":
        normalized["tool_response"] = {key: payload[key] for key in ("error_message", "failure_type", "is_interrupt") if key in payload}
    elif name == "claude-code" and native_event == "PostToolUseFailure":
        normalized["tool_response"] = {key: payload[key] for key in ("error", "is_interrupt") if key in payload}
    result = _handle_canonical_hook(normalized, data_dir, store=store, worker_launcher=worker_launcher)
    if name == "cursor":
        context = result.get("hookSpecificOutput", {}).get("additionalContext")
        output = {}
        if context and native_event == "sessionStart":
            output["additional_context"] = context
        # Other Cursor events have distinct output schemas. Never change permissions.
        if result.get("systemMessage"):
            sys.stderr.write(result["systemMessage"] + "\n")
        return output
    if "hookSpecificOutput" in result:
        result["hookSpecificOutput"]["hookEventName"] = native_event
        if name == "gemini-cli" and native_event == "PreCompress":
            result.pop("hookSpecificOutput", None)  # Advisory event has no context-injection contract.
    return result


def main(data_dir: Path | str | None = None, *, host: str | None = None) -> int:
    """Fail visibly but non-blockingly; never print hook input or credentials."""
    failure_reason = "host_input_invalid"
    try:
        raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
        if len(raw) > MAX_HOOK_BYTES:
            failure_reason = "host_input_limit"
            raise ValueError("Hook input exceeds limit")
        payload = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("Hook input must be an object")
        failure_reason = "host_capture_failed"
        result = handle_hook(payload, data_dir, host=host)
    except Exception:
        try:
            from .capture import record_ingress_failure
            record_ingress_failure(data_dir, failure_reason)
        except Exception:
            pass  # Never let health reporting block the user's host operation.
        message = "藍圖工作流自動記錄未能寫入；請查看 session_status，並用 event_append 補充重要事件。"
        if host == "cursor" or (host is None and os.environ.get("BLUEPRINT_WORKFLOW_HOST") == "cursor"):
            sys.stderr.write(message + "\n")
            result = {}
        else:
            result = {"systemMessage": message}
    if result:
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0
