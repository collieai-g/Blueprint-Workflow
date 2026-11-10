"""Portable workflow contracts, validation, and human-readable exports.

This module is deliberately independent of any model vendor or user interface.
"""
from __future__ import annotations

from copy import deepcopy
import ast
import json
import math
import re
from typing import Any


INPUT_TYPES = {"string", "number", "integer", "boolean", "object", "array"}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _strings(value: Any, label: str, *, minimum: int = 0) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError(f"{label} must be an array with at least {minimum} entries")
    for entry in value:
        nonempty(entry, label)
    if len(set(value)) != len(value):
        raise ValueError(f"{label} contains duplicate entries")
    return value


def matches_type(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    return False


def analysis_json_schema(protocol_version: int = 1) -> dict[str, Any]:
    """A strict JSON Schema accepted by Codex structured output.

    Optional values use null so all object properties can remain required.
    """
    if protocol_version == 2:
        return learning_json_schema()
    if protocol_version != 1:
        raise ValueError("unsupported analysis protocol")
    def obj(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}

    text = {"type": "string"}
    texts = {"type": "array", "items": text}
    nullable_text = {"type": ["string", "null"]}
    scalar = {"type": ["string", "number", "boolean", "null"]}
    # Input defaults are represented as JSON text to accommodate any input type
    # without opening the strict output schema to unconstrained objects.
    field = obj({"name": text, "description": text, "type": {"type": "string", "enum": sorted(INPUT_TYPES)},
                 "required": {"type": "boolean"}, "default_json": nullable_text})
    condition = obj({"input": nullable_text, "step": nullable_text,
                     "output": nullable_text, "equals": scalar})
    condition["type"] = ["object", "null"]
    step = obj({"id": text, "title": text, "instruction": text, "depends_on": texts,
                "success_criteria": texts, "evidence_event_ids": texts,
                "outputs": texts, "when": condition, "recovery": text})
    question = obj({"id": text, "question": text, "blocking": {"type": "boolean"},
                    "evidence_event_ids": texts})
    proposal = obj({"id": text, "description": text, "reason": text,
                    "requires_confirmation": {"type": "boolean", "const": True},
                    "status": {"type": "string", "enum": ["proposed", "approved", "rejected"]},
                    "evidence_event_ids": texts})
    artifact = obj({"path": nullable_text, "description": text})
    helper = obj({"name": text,
                  "language": {"type": "string", "enum": ["python", "javascript", "powershell", "shell", "json"]},
                  "description": text, "content": text, "evidence_event_ids": texts})
    workflow = obj({"title": text, "goal": text, "inputs": {"type": "array", "items": field},
                    "environment": texts, "steps": {"type": "array", "items": step},
                    "completion_criteria": texts,
                    "questions": {"type": "array", "items": question},
                    "proposals": {"type": "array", "items": proposal},
                    "artifacts": {"type": "array", "items": artifact},
                    "helpers": {"type": "array", "items": helper}, "summary": text})
    return obj({"workflow": workflow, "change_summary": text})


def learning_json_schema() -> dict[str, Any]:
    """Strict vendor-neutral section-patch contract; legacy exports stay v1."""
    def obj(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}
    text = {"type": "string", "maxLength": 32000}
    identifier = {"type": "string", "minLength": 1, "maxLength": 200}
    ids = {"type": "array", "items": identifier, "maxItems": 128}
    section = {"type": "string", "enum": ["title", "goal", "summary", "inputs", "environment", "steps", "completion_criteria", "questions", "proposals", "artifacts", "helpers"]}
    update = obj({"section": section, "entry_id": identifier, "value_json": text, "evidence_event_ids": ids})
    removal = obj({"section": section, "entry_id": identifier, "reason": text, "evidence_event_ids": ids})
    memory = obj({"id": identifier,
                  "kind": {"type": "string", "enum": ["constraint", "decision", "fact", "question", "output", "correction"]},
                  "text": {"type": "string", "maxLength": 4000},
                  "status": {"type": "string", "enum": ["active", "pending", "superseded", "resolved"]},
                  "evidence_event_ids": ids, "supersedes": ids})
    research = obj({"id": identifier, "question": text, "reason": text, "evidence_event_ids": ids,
                    "official_domains": {"type": "array", "items": {"type": "string", "maxLength": 255}, "maxItems": 8}})
    insight = obj({"id": identifier, "kind": {"type": "string", "enum": ["question", "suggestion", "finding"]},
                   "title": text, "detail": text, "evidence_event_ids": ids})
    request = obj({"kind": {"type": "string", "enum": ["event", "section", "source"]},
                   "reference": identifier, "query": {"type": "string", "maxLength": 500},
                   "offset": {"type": "integer", "minimum": 0},
                   "max_chars": {"type": "integer", "minimum": 100, "maximum": 32000}})
    return obj({"protocol_version": {"type": "integer", "const": 2},
                "base_revision": {"type": "integer", "minimum": 0},
                "constraint_revision": {"type": "integer", "minimum": 0},
                "patch": obj({"updates": {"type": "array", "items": update, "maxItems": 64},
                              "removals": {"type": "array", "items": removal, "maxItems": 64}}),
                "memory_updates": {"type": "array", "items": memory, "maxItems": 64},
                "context_requests": {"type": "array", "items": request, "maxItems": 4},
                "research_requests": {"type": "array", "items": research, "maxItems": 8},
                "insights": {"type": "array", "items": insight, "maxItems": 16},
                "change_summary": text})


def _normalize_input(field: dict[str, Any]) -> dict[str, Any]:
    field = deepcopy(field)
    if "default_json" in field:
        encoded = field.pop("default_json")
        if encoded is not None:
            if not isinstance(encoded, str):
                raise ValueError("default_json must be JSON text or null")
            field["default"] = json.loads(encoded)
    return field


def validate_workflow(workflow: Any, *, evidence_ids: set[str] | None = None,
                      confirmation_decisions: dict[str, str] | None = None) -> dict[str, Any]:
    if not isinstance(workflow, dict):
        raise ValueError("workflow must be an object")
    result = deepcopy(workflow)
    for key in ("title", "goal", "summary"):
        nonempty(result.get(key), key)
    for key in ("environment", "completion_criteria"):
        _strings(result.get(key), key, minimum=1 if key == "completion_criteria" else 0)
    fields = result.get("inputs")
    if not isinstance(fields, list):
        raise ValueError("inputs must be an array")
    names: set[str] = set()
    normalized_fields = []
    for raw in fields:
        if not isinstance(raw, dict):
            raise ValueError("input must be an object")
        field = _normalize_input(raw)
        name = nonempty(field.get("name"), "input.name")
        if not _NAME.fullmatch(name) or name in names:
            raise ValueError("input names must be unique identifiers")
        names.add(name)
        nonempty(field.get("description"), "input.description")
        if field.get("type") not in INPUT_TYPES or not isinstance(field.get("required"), bool):
            raise ValueError("input type or required flag is invalid")
        if "default" in field and not matches_type(field["default"], field["type"]):
            raise ValueError(f"input default has wrong type: {name}")
        normalized_fields.append(field)
    result["inputs"] = normalized_fields
    steps = result.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must contain at least one step")
    ids: set[str] = set()
    by_id: dict[str, dict] = {}
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("step must be an object")
        step_id = nonempty(step.get("id"), "step.id")
        if step_id in ids:
            raise ValueError("step IDs must be unique")
        for key in ("title", "instruction"):
            nonempty(step.get(key), "step." + key)
        step.setdefault("recovery", "Inspect the current state and preserve completed work. Resolve the cause before retrying; ask for missing requirements when needed.")
        nonempty(step["recovery"], "step.recovery")
        _strings(step.get("depends_on"), "depends_on")
        _strings(step.get("success_criteria"), "success_criteria", minimum=1)
        _strings(step.get("outputs"), "outputs")
        _strings(step.get("evidence_event_ids"), "evidence_event_ids", minimum=1)
        ids.add(step_id)
        by_id[step_id] = step
    for step in steps:
        step_id = step["id"]
        if step_id in step["depends_on"] or not set(step["depends_on"]).issubset(ids):
            raise ValueError("step dependencies must refer to other existing steps")
        condition = step.get("when")
        if condition is not None:
            if not isinstance(condition, dict):
                raise ValueError("when must be an object or null")
            is_input = bool(condition.get("input"))
            is_output = bool(condition.get("step")) and bool(condition.get("output"))
            if is_input == is_output or "equals" not in condition:
                raise ValueError("when requires exactly input or step/output plus equals")
            if is_input and (condition["input"] not in names or condition.get("step") or condition.get("output")):
                raise ValueError("when refers to an unknown input")
            if is_output:
                source = condition["step"]
                if source not in step["depends_on"] or condition["output"] not in by_id[source]["outputs"]:
                    raise ValueError("when source must be a dependency with the named output")
            if not isinstance(condition["equals"], (str, int, float, bool, type(None))):
                raise ValueError("when.equals must be a scalar")
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise ValueError("step dependencies contain a cycle")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in by_id[step_id]["depends_on"]:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)
    for step_id in ids:
        visit(step_id)
    for collection, required_text in (("questions", "question"), ("proposals", "description")):
        entries = result.get(collection)
        if not isinstance(entries, list):
            raise ValueError(collection + " must be an array")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(collection + " entry must be an object")
            entry_id = nonempty(entry.get("id"), collection + ".id")
            if entry_id in seen:
                raise ValueError(collection + " IDs must be unique")
            seen.add(entry_id)
            nonempty(entry.get(required_text), required_text)
            _strings(entry.get("evidence_event_ids"), "evidence_event_ids", minimum=1)
            if collection == "questions":
                if not isinstance(entry.get("blocking"), bool):
                    raise ValueError("question.blocking must be boolean")
            else:
                nonempty(entry.get("reason"), "proposal.reason")
                if entry.get("requires_confirmation") is not True:
                    raise ValueError("proposals must require confirmation")
                decision = entry.get("status")
                if decision not in {"proposed", "approved", "rejected"}:
                    raise ValueError("proposal status is invalid")
                if decision != "proposed" and (confirmation_decisions or {}).get(entry_id) != decision:
                    raise ValueError("proposal decision requires an explicit human confirmation event")
    if not isinstance(result.get("artifacts"), list):
        raise ValueError("artifacts must be an array")
    for artifact in result["artifacts"]:
        if not isinstance(artifact, dict):
            raise ValueError("artifact must be an object")
        nonempty(artifact.get("description"), "artifact.description")
        if artifact.get("path") is not None and not isinstance(artifact["path"], str):
            raise ValueError("artifact.path must be text or null")
    helpers = result.setdefault("helpers", [])
    if not isinstance(helpers, list):
        raise ValueError("helpers must be an array")
    helper_names: set[str] = set()
    extensions = {"python": ".py", "javascript": ".js", "powershell": ".ps1", "shell": ".sh", "json": ".json"}
    reserved_names = {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}
    for helper in helpers:
        if not isinstance(helper, dict):
            raise ValueError("helper must be an object")
        name = nonempty(helper.get("name"), "helper.name")
        language = helper.get("language")
        if language not in extensions:
            raise ValueError("helper.language is unsupported")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}", name)
                or ".." in name or not name.endswith(extensions[language])
                or name.split(".", 1)[0].upper() in reserved_names):
            raise ValueError("helper name must be a safe single filename matching its language")
        if name.casefold() in helper_names:
            raise ValueError("helper filenames must be unique")
        helper_names.add(name.casefold())
        nonempty(helper.get("description"), "helper.description")
        content = nonempty(helper.get("content"), "helper.content")
        _strings(helper.get("evidence_event_ids"), "helper.evidence_event_ids", minimum=1)
        if language == "python":
            try:
                parsed_tree = ast.parse(content, filename=name, mode="exec")
                compile(parsed_tree, name, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, TypeError) as exc:
                raise ValueError("Python helper syntax is invalid: " + name) from exc
        elif language == "json":
            try:
                json.loads(content)
            except (ValueError, TypeError) as exc:
                raise ValueError("JSON helper syntax is invalid: " + name) from exc
    if evidence_ids is not None:
        for entry in steps + result["questions"] + result["proposals"] + helpers:
            if not set(entry["evidence_event_ids"]).issubset(evidence_ids):
                raise ValueError("workflow refers to evidence outside this session")
    json_text(result)
    return result


def validate_analysis(value: Any, **kwargs: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("analysis must be an object")
    nonempty(value.get("change_summary"), "change_summary")
    return {"workflow": validate_workflow(value.get("workflow"), **kwargs),
            "change_summary": value["change_summary"]}


def resolve_inputs(workflow: dict[str, Any], supplied: Any) -> dict[str, Any]:
    if not isinstance(supplied, dict):
        raise ValueError("inputs must be an object")
    known = {field["name"] for field in workflow["inputs"]}
    if set(supplied) - known:
        raise ValueError("unknown inputs: " + ", ".join(sorted(set(supplied) - known)))
    result = deepcopy(supplied)
    for field in workflow["inputs"]:
        name = field["name"]
        if name not in result:
            if "default" in field:
                result[name] = deepcopy(field["default"])
            elif field["required"]:
                raise ValueError("missing required input: " + name)
            else:
                continue
        if not matches_type(result[name], field["type"]):
            raise ValueError("input has wrong type: " + name)
    json_text(result)
    return result


def render_markdown(record: dict[str, Any]) -> str:
    workflow = record.get("workflow", record)
    lines = ["# " + workflow["title"], "", workflow["goal"], "", workflow["summary"], ""]
    if "version" in record:
        lines += [f"版本：{record['version']}；測試狀態：{record.get('test_status', 'not_tested')}", ""]
    lines += ["## 本次輸入", ""]
    for field in workflow["inputs"]:
        lines.append(f"- {field['name']} ({field['type']})：{field['description']}")
    lines += ["", "## 執行環境", ""] + ["- " + item for item in workflow["environment"]]
    lines += ["", "## 固定步驟", ""]
    for step in workflow["steps"]:
        lines += [f"### {step['id']}：{step['title']}", "", step["instruction"], ""]
        if step["depends_on"]:
            lines.append("前置：" + ", ".join(step["depends_on"]))
        if step.get("when"):
            lines.append("條件：" + json_text(step["when"]))
        lines.extend("- 完成條件：" + item for item in step["success_criteria"])
        if step["outputs"]:
            lines.append("- 本次輸出：" + ", ".join(step["outputs"]))
        lines.append("- 遇阻處理：" + step.get("recovery", "Inspect the current state before retrying."))
        lines += ["- 示範依據：" + ", ".join(step["evidence_event_ids"]), ""]
    lines += ["## 整體驗收", ""] + ["- " + item for item in workflow["completion_criteria"]]
    if workflow["questions"]:
        lines += ["", "## 待確認", ""] + ["- " + item["question"] for item in workflow["questions"]]
    if workflow["proposals"]:
        lines += ["", "## 改進建議與決定", ""]
        lines.extend(f"- [{item['status']}] {item['description']}：{item['reason']}" for item in workflow["proposals"])
    if workflow.get("helpers"):
        lines += ["", "## 輔助程式", ""]
        lines.extend(f"- `{item['name']}` ({item['language']})：{item['description']}" for item in workflow["helpers"])
    lines += ["", "## 執行原則", "", "使用目前可用且符合已確認環境的 Agent 工具。每步核對當次結果，保留本次輸出；中斷後先讀取執行狀態。", ""]
    return "\n".join(lines)
