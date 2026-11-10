"""Bounded, source-linked learning over the durable recording.

The model edits selected sections; SQLite retains every other section.  This
module never performs the recorded work or opens an unregistered source path.
All ``*_learning`` mutations use the caller's transaction.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
from typing import Any

from .model import json_text, nonempty, render_markdown


COLLECTIONS = ("inputs", "environment", "steps", "completion_criteria", "questions", "proposals", "artifacts", "helpers")
SCALARS = ("title", "goal", "summary")
SECTIONS = SCALARS + COLLECTIONS
MAX_CONTEXT_BYTES = 96_000
MAX_PATCH_BYTES = 80_000
MAX_UPDATES = 64
MAX_MEMORIES = 64


def _load(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value else default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_id(job: Any) -> str:
    return str(dict(job).get("job_id") or dict(job).get("id") or "")


def _bounded(value: Any, max_bytes: int) -> Any:
    """An explicitly incomplete preview, never a disguised full source."""
    encoded = json_text(value)
    if len(encoded.encode("utf-8")) <= max_bytes:
        return deepcopy(value)
    result = {"context_complete": False, "original_bytes": len(encoded.encode("utf-8")),
              "note": "Retrieve the registered source before relying on omitted text."}
    if isinstance(value, dict):
        for key in ("id", "seq", "source_id"):
            if key in value:
                result[key] = value[key]
    overhead = len(json_text(result).encode()) + 40
    result["preview_json"] = encoded.encode("utf-8")[:max(0, max_bytes - overhead)].decode("utf-8", errors="ignore")
    return result


def _key(section: str, value: Any) -> str:
    if section in SCALARS:
        return "value"
    if isinstance(value, dict):
        candidate = value.get("id") if section in {"steps", "questions", "proposals"} else value.get("name")
        if candidate:
            return str(candidate)
    return "item_" + hashlib.sha256(json_text(value).encode()).hexdigest()[:20]


def _entries(workflow: dict[str, Any] | None) -> list[dict[str, Any]]:
    result = []
    for section in SECTIONS:
        values = [workflow[section]] if workflow and section in SCALARS and section in workflow else (workflow or {}).get(section, [])
        if not isinstance(values, list):
            continue
        for position, value in enumerate(values):
            result.append({"section": section, "entry_id": _key(section, value), "position": position, "value": value})
    return result


def _workflow(entries: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {name: [] for name in COLLECTIONS}
    for entry in sorted(entries, key=lambda item: (SECTIONS.index(item["section"]), item["position"])):
        if entry["section"] in SCALARS:
            result[entry["section"]] = deepcopy(entry["value"])
        else:
            result[entry["section"]].append(deepcopy(entry["value"]))
    return result


def _terms(text: str) -> set[str]:
    # Character pairs also work for languages without spaces. This is a bounded
    # ranking aid, not an inferred business taxonomy or an embedding service.
    words = set(re.findall(r"[\w-]{3,40}", text.lower()))
    chars = "".join(re.findall(r"[^\x00-\x7f]", text))[:2000]
    words.update(chars[i:i + 2] for i in range(max(0, len(chars) - 1)))
    return set(sorted(words)[:512])


def _score(text: str, terms: set[str]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term in lowered)


class LearningMixin:
    def init_learning(self, db: Any) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS learning_memories (session_id TEXT NOT NULL,id TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,evidence_json TEXT NOT NULL,supersedes_json TEXT NOT NULL,revision INTEGER NOT NULL,through_seq INTEGER NOT NULL,PRIMARY KEY(session_id,id))",
            "CREATE TABLE IF NOT EXISTS learning_sections (session_id TEXT NOT NULL,section TEXT NOT NULL,entry_id TEXT NOT NULL,position INTEGER NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(session_id,section,entry_id))",
            "CREATE TABLE IF NOT EXISTS learning_job_contexts (job_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,context_json TEXT NOT NULL,sha256 TEXT NOT NULL,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS learning_snapshots (session_id TEXT NOT NULL,revision INTEGER NOT NULL,through_seq INTEGER NOT NULL,constraint_revision INTEGER NOT NULL,context_hash TEXT NOT NULL,constraints_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(session_id,revision))",
            "CREATE TABLE IF NOT EXISTS learning_memory_revisions (session_id TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(session_id,id,revision))",
            "CREATE INDEX IF NOT EXISTS learning_memory_scope ON learning_memories(session_id,status,through_seq)",
        )
        for statement in statements:
            db.execute(statement)

    def _learning_entries(self, db: Any, session_id: str) -> list[dict[str, Any]]:
        rows = db.execute("SELECT section,entry_id,position,payload FROM learning_sections WHERE session_id=? ORDER BY section,position,entry_id", (session_id,)).fetchall()
        if rows:
            return [{"section": row["section"], "entry_id": row["entry_id"], "position": row["position"], "value": _load(row["payload"])} for row in rows]
        row = db.execute("SELECT draft FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _entries(_load(row["draft"]) if row else None)

    def _learning_context(self, db: Any, job: Any) -> dict[str, Any]:
        row = db.execute("SELECT context_json FROM learning_job_contexts WHERE job_id=?", (_job_id(job),)).fetchone()
        return _load(row["context_json"], {}) if row else dict(job).get("learning_context", {})

    def prepare_learning_job(self, db: Any, job: dict[str, Any]) -> dict[str, Any]:
        job = dict(job)
        session_id = job["session_id"]
        terms = _terms(" ".join(str(event.get("summary", "")) for event in job.get("events", [])))
        entries = self._learning_entries(db, session_id)
        correction_ids = {event["corrects_event_id"] for event in job.get("events", []) if event.get("corrects_event_id")}
        ranked = sorted(entries, key=lambda entry: (
            entry["section"] not in SCALARS,
            not bool(correction_ids.intersection(entry["value"].get("evidence_event_ids", []) if isinstance(entry["value"], dict) else [])),
            -_score(json_text(entry["value"]), terms), entry["position"]))
        selected = []
        used = 0
        for entry in ranked:
            size = len(json_text(entry).encode())
            if used + size <= 32_000:
                selected.append(entry)
                used += size
        directory = []
        for entry in ranked:
            label = entry["value"].get("title", entry["value"].get("description", "")) if isinstance(entry["value"], dict) else str(entry["value"])
            item = {"section": entry["section"], "entry_id": entry["entry_id"], "label": label[:120]}
            if len(json_text(directory + [item]).encode()) > 5000:
                break
            directory.append(item)
        memories = []
        memory_rows = db.execute("SELECT * FROM learning_memories WHERE session_id=? AND through_seq<=? AND status IN ('active','pending') ORDER BY CASE kind WHEN 'constraint' THEN 0 WHEN 'decision' THEN 1 ELSE 2 END,revision DESC,id", (session_id, job["through_seq"])).fetchall()
        ordered = sorted(memory_rows, key=lambda row: (row["kind"] not in {"constraint", "decision"}, -_score(row["text"], terms), -row["revision"]))
        for row in ordered:
            item = {"id": row["id"], "kind": row["kind"], "text": row["text"], "status": row["status"], "evidence_event_ids": _load(row["evidence_json"], []), "supersedes": _load(row["supersedes_json"], []), "provenance": "model_interpretation_of_cited_events"}
            if len(json_text(memories + [item]).encode()) <= 14_000:
                memories.append(item)
        previews = []
        events = job.get("events", [])
        per_event = max(200, 24_000 // max(1, len(events)))
        for event in events:
            item = {key: deepcopy(event[key]) for key in ("id", "seq", "actor", "kind", "corrects_event_id", "source_refs") if key in event}
            item["summary"] = str(event.get("summary", ""))[:max(80, per_event // 3)]
            item["record"] = _bounded(event, max(100, per_event - len(json_text(item).encode())))
            previews.append(item)
        source_ids: set[str] = set()
        for event in events:
            for reference in event.get("source_refs", []):
                if isinstance(reference, dict) and reference.get("source_id"):
                    source_ids.add(reference["source_id"])
        snippets = []
        for event_id in sorted(correction_ids)[:8]:
            row = db.execute("SELECT payload FROM events WHERE session_id=? AND id=? AND seq<=?", (session_id, event_id, job["through_seq"])).fetchone()
            if row:
                snippets.append({"event_id": event_id, "record": _bounded(_load(row["payload"]), 1500)})
        if hasattr(self, "source_excerpt"):
            for source_id in sorted(source_ids)[:4]:
                try:
                    snippet = self.source_excerpt(db, session_id, source_id, max_chars=1200)
                except (KeyError, ValueError, OSError):
                    continue
                if len(json_text(snippets + [snippet]).encode()) <= 10_000:
                    snippets.append(snippet)
                if snippet.get("characters", 0) > 2400:
                    try:
                        tail = self.source_excerpt(db, session_id, source_id, max_chars=1200, offset=max(0, snippet["characters"] - 1200))
                    except (KeyError, ValueError, OSError, TypeError):
                        tail = None
                    if tail and len(json_text(snippets + [tail]).encode()) <= 10_000:
                        snippets.append(tail)
        decisions = self.intelligence_context(db, session_id, max_items=20, max_chars=12000, through_seq=job["through_seq"]) if hasattr(self, "intelligence_context") else {"batch_decisions": [], "latest_effective_guards": []}
        context = {"budget_bytes": MAX_CONTEXT_BYTES, "sections": selected, "section_directory": directory,
                   "memories": memories, "new_events": previews, "source_snippets": snippets,
                   "intelligence_context": decisions,
                   "omitted": {"section_count": len(entries) - len(selected), "memory_count": len(memory_rows) - len(memories), "directory_count": len(entries) - len(directory)},
                   "rule": "Omitted entries remain stored. Never remove or replace an unseen existing entry; retrieve it first. Previews are not full sources."}
        # A defensive bound includes all JSON overhead. Required identity fields
        # survive even a pathological batch of long reference identifiers.
        while len(json_text(context).encode()) > MAX_CONTEXT_BYTES:
            if context["source_snippets"]:
                context["source_snippets"].pop()
            elif context["sections"]:
                context["sections"].pop(); context["omitted"]["section_count"] += 1
            elif context["memories"]:
                context["memories"].pop(); context["omitted"]["memory_count"] += 1
            elif context["section_directory"]:
                context["section_directory"].pop(); context["omitted"]["directory_count"] += 1
            else:
                raise ValueError("event identities exceed the analysis context budget; reduce the claimed batch")
        encoded = json_text(context)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        db.execute("INSERT OR REPLACE INTO learning_job_contexts VALUES(?,?,?,?,?)", (_job_id(job), session_id, encoded, digest, _now()))
        job.update(protocol_version=2, learning_context=context, context_hash=digest,
                   constraint_revision=int(job.get("constraint_revision", 0)))
        job["_context_retriever"] = lambda requests: self.retrieve_learning_context(job, requests)
        draft = job.get("previous_draft")
        if draft is not None and len(json_text(draft).encode()) > 12_000:
            job["previous_draft"] = None
        return job

    def retrieve_learning_context(self, job: dict[str, Any], requests: list[dict[str, Any]]) -> dict[str, Any]:
        """Fulfil only bounded references inside this exact claimed snapshot."""
        if not isinstance(requests, list) or not 1 <= len(requests) <= 4:
            raise ValueError("context retrieval requires 1 to 4 requests")
        with self._transaction() as db:
            live = db.execute("SELECT state,lease_until,through_seq FROM jobs WHERE id=? AND session_id=?", (_job_id(job), job["session_id"])).fetchone()
            if live is None or live["state"] != "claimed" or live["lease_until"] <= time.time() or live["through_seq"] != job["through_seq"]:
                raise ValueError("history request belongs to an expired analysis snapshot")
            context = self._learning_context(db, job)
            results = []
            requested_entries = []
            remaining = 32_000
            for request in requests:
                if not isinstance(request, dict):
                    raise ValueError("context request must be an object")
                kind = request.get("kind")
                reference = nonempty(request.get("reference"), "context reference")
                offset, length = request.get("offset", 0), request.get("max_chars", 4000)
                query = request.get("query", "")
                if len(reference) > 200 or not isinstance(query, str) or len(query) > 500 or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(length, bool) or not isinstance(length, int) or not 100 <= length <= 32_000:
                    raise ValueError("context request exceeds its bounds")
                length = min(length, max(100, remaining - 500))
                additions = []
                if kind == "event":
                    row = db.execute("SELECT payload FROM events WHERE session_id=? AND id=? AND seq<=?", (job["session_id"], reference, job["through_seq"])).fetchone()
                    result = {"kind": kind, "reference": reference, "missing": row is None}
                    if row:
                        payload = row["payload"]
                        if query:
                            found = payload.lower().find(query.lower())
                            if found >= 0:
                                offset = max(0, found - length // 4)
                        result.update(text=payload[offset:offset + length], offset=offset, next_offset=offset + length,
                                      has_more=offset + length < len(payload), provenance="stored_sanitized_event")
                elif kind == "section":
                    section, separator, entry_id = reference.partition(":")
                    if not separator or section not in SECTIONS:
                        raise ValueError("section references use section:entry_id")
                    entry = next((item for item in self._learning_entries(db, job["session_id"]) if item["section"] == section and item["entry_id"] == entry_id), None)
                    result = {"kind": kind, "reference": reference, "missing": entry is None}
                    if entry:
                        if len(json_text(entry).encode()) <= length:
                            additions.append(entry)
                            result["full_section_supplied"] = True
                        else:
                            result["record"] = _bounded(entry, length)
                            result["full_section_supplied"] = False
                elif kind == "source":
                    rows = db.execute("SELECT payload FROM events WHERE session_id=? AND seq<=? AND instr(payload,?)>0 LIMIT 8", (job["session_id"], job["through_seq"], reference)).fetchall()
                    linked = any(any(isinstance(ref, dict) and ref.get("source_id") == reference for ref in _load(row["payload"]).get("source_refs", [])) for row in rows)
                    if not linked or not hasattr(self, "_source_read"):
                        result = {"kind": kind, "reference": reference, "missing": True}
                    else:
                        result = {"kind": kind, "reference": reference, **self._source_read(db, job["session_id"], reference, offset, length)}
                else:
                    raise ValueError("unsupported context request kind")
                cost = len(json_text(result).encode()) + sum(len(json_text(entry).encode()) for entry in additions)
                if cost > remaining:
                    result = {"kind": kind, "reference": reference, "budget_exhausted": True}
                else:
                    remaining -= cost
                    requested_entries.extend(additions)
                results.append(result)
                if remaining < 600:
                    break
            requested_keys = {(entry["section"], entry["entry_id"]) for entry in requested_entries}
            context["sections"] = [entry for entry in context["sections"] if (entry["section"], entry["entry_id"]) not in requested_keys] + requested_entries
            context["retrieval_results"] = results
            while len(json_text(context).encode()) > MAX_CONTEXT_BYTES:
                if context["source_snippets"]:
                    context["source_snippets"].pop()
                else:
                    removable = next((i for i, entry in enumerate(context["sections"]) if (entry["section"], entry["entry_id"]) not in requested_keys and entry["section"] not in SCALARS), None)
                    if removable is None:
                        raise ValueError("requested evidence exceeds the fixed analysis budget")
                    context["sections"].pop(removable)
                    context["omitted"]["section_count"] += 1
            encoded = json_text(context)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            db.execute("UPDATE learning_job_contexts SET context_json=?,sha256=? WHERE job_id=?", (encoded, digest, _job_id(job)))
            return {"learning_context": context, "context_hash": digest}

    def _apply_learning_patch(self, db: Any, job: Any, analysis: dict[str, Any]) -> list[dict[str, Any]]:
        job = dict(job)
        session_id = job["session_id"]
        if analysis.get("protocol_version") != 2:
            raise ValueError("unsupported learning protocol")
        if analysis.get("context_requests"):
            raise ValueError("history requests must be resolved before committing a patch")
        if analysis.get("base_revision") != job["base_revision"] or analysis.get("constraint_revision") != job.get("constraint_revision", 0):
            raise ValueError("learning patch base revision or constraint snapshot is stale")
        if len(json_text(analysis).encode()) > MAX_PATCH_BYTES:
            raise ValueError("learning patch exceeds the output budget")
        patch = analysis.get("patch")
        if not isinstance(patch, dict):
            raise ValueError("learning analysis requires a patch")
        updates, removals = patch.get("updates", []), patch.get("removals", [])
        if not isinstance(updates, list) or not isinstance(removals, list) or len(updates) + len(removals) > MAX_UPDATES:
            raise ValueError("learning patch operation limit exceeded")
        entries = self._learning_entries(db, session_id)
        keyed = {(entry["section"], entry["entry_id"]): entry for entry in entries}
        context = self._learning_context(db, job)
        visible = {(entry["section"], entry["entry_id"]) for entry in context.get("sections", [])}
        touched = set()
        for removal, operations in ((False, updates), (True, removals)):
            for operation in operations:
                if not isinstance(operation, dict) or operation.get("section") not in SECTIONS:
                    raise ValueError("invalid learning patch section")
                section = operation["section"]
                entry_id = nonempty(operation.get("entry_id"), "patch.entry_id")
                if len(entry_id) > 200 or section in SCALARS and entry_id != "value":
                    raise ValueError("invalid learning section identity")
                key = section, entry_id
                if key in touched:
                    raise ValueError("learning patch repeats an entry")
                touched.add(key)
                evidence = operation.get("evidence_event_ids")
                self._validate_learning_evidence(db, session_id, job["through_seq"], evidence)
                if key in keyed and key not in visible:
                    raise ValueError("retrieve the existing section before modifying or removing it")
                if removal:
                    nonempty(operation.get("reason"), "patch removal reason")
                    if key not in keyed or section in SCALARS:
                        raise ValueError("cannot remove an unknown or required scalar section")
                    del keyed[key]
                    continue
                value = json.loads(nonempty(operation.get("value_json"), "patch.value_json"))
                if section in SCALARS and not isinstance(value, str):
                    raise ValueError("scalar workflow sections require text")
                if section in {"steps", "questions", "proposals", "inputs", "helpers"} and _key(section, value) != entry_id:
                    raise ValueError("patch entry identity differs from its value")
                position = keyed[key]["position"] if key in keyed else max([entry["position"] for entry in keyed.values() if entry["section"] == section], default=-1) + 1
                keyed[key] = {"section": section, "entry_id": entry_id, "position": position, "value": value}
        return list(keyed.values())

    @staticmethod
    def _validate_learning_evidence(db: Any, session_id: str, through_seq: int, ids: Any) -> None:
        if not isinstance(ids, list) or not ids or len(ids) > 128 or any(not isinstance(item, str) for item in ids):
            raise ValueError("learning changes require bounded source event IDs")
        for event_id in set(ids):
            if db.execute("SELECT 1 FROM events WHERE session_id=? AND id=? AND seq<=?", (session_id, event_id, through_seq)).fetchone() is None:
                raise ValueError("learning evidence is outside this recording boundary")

    def assemble_learning_analysis(self, db: Any, job: Any, analysis: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(analysis, dict):
            raise ValueError("analysis must be an object")
        if "workflow" in analysis and "protocol_version" not in analysis:
            return deepcopy(analysis)
        nonempty(analysis.get("change_summary"), "change_summary")
        workflow = _workflow(self._apply_learning_patch(db, job, analysis))
        return {"workflow": workflow, "change_summary": analysis["change_summary"],
                "research_requests": analysis.get("research_requests", []), "insights": analysis.get("insights", [])}

    def commit_learning(self, db: Any, session_id: str, job: Any, analysis: dict[str, Any], workflow: dict[str, Any], revision: int) -> None:
        job = dict(job)
        entries = self._apply_learning_patch(db, job, analysis) if analysis.get("protocol_version") == 2 else _entries(workflow)
        memories = analysis.get("memory_updates", [])
        if not isinstance(memories, list) or len(memories) > MAX_MEMORIES:
            raise ValueError("learning memory update limit exceeded")
        seen = set()
        for memory in memories:
            if not isinstance(memory, dict):
                raise ValueError("memory update must be an object")
            memory_id = nonempty(memory.get("id"), "memory.id")
            if memory_id in seen or len(memory_id) > 200:
                raise ValueError("invalid or repeated memory ID")
            seen.add(memory_id)
            if memory.get("kind") not in {"constraint", "decision", "fact", "question", "output", "correction"} or memory.get("status") not in {"active", "pending", "superseded", "resolved"}:
                raise ValueError("invalid memory kind or state")
            text = nonempty(memory.get("text"), "memory.text")
            if len(text.encode()) > 4000:
                raise ValueError("memory text exceeds its budget")
            evidence = memory.get("evidence_event_ids")
            self._validate_learning_evidence(db, session_id, job["through_seq"], evidence)
            if memory["kind"] in {"constraint", "decision"}:
                human_source = any(_load(db.execute("SELECT payload FROM events WHERE session_id=? AND id=?", (session_id, event_id)).fetchone()["payload"]).get("actor") == "human" for event_id in evidence)
                if not human_source:
                    raise ValueError("a constraint or decision memory needs an actual human source")
            supersedes = memory.get("supersedes", [])
            if not isinstance(supersedes, list) or any(not isinstance(item, str) or item == memory_id for item in supersedes):
                raise ValueError("invalid memory supersession chain")
            existing = db.execute("SELECT * FROM learning_memories WHERE session_id=? AND id=?", (session_id, memory_id)).fetchone()
            if existing and (existing["text"] != text or existing["kind"] != memory["kind"] or _load(existing["supersedes_json"], []) != supersedes):
                raise ValueError("changed memory requires a new ID and an explicit supersession link")
            if existing and existing["status"] == "superseded" and memory["status"] != "superseded":
                raise ValueError("a superseded memory cannot be silently restored")
            for previous_id in supersedes:
                previous = db.execute("SELECT 1 FROM learning_memories WHERE session_id=? AND id=?", (session_id, previous_id)).fetchone()
                if previous is None:
                    raise ValueError("superseded memory does not belong to this recording")
                if existing:
                    # Only a new node may add replacement edges. This keeps the
                    # relation acyclic and preserves historical rule text.
                    if previous_id not in _load(existing["supersedes_json"], []):
                        raise ValueError("memory replacement edges are immutable")
                db.execute("UPDATE learning_memories SET status='superseded',revision=? WHERE session_id=? AND id=?", (revision, session_id, previous_id))
                replaced = dict(db.execute("SELECT * FROM learning_memories WHERE session_id=? AND id=?", (session_id, previous_id)).fetchone())
                db.execute("INSERT OR REPLACE INTO learning_memory_revisions VALUES(?,?,?,?)", (session_id, previous_id, revision, json_text(replaced)))
            if existing:
                evidence = sorted(set(_load(existing["evidence_json"], [])) | set(evidence))
            db.execute("INSERT OR REPLACE INTO learning_memories VALUES(?,?,?,?,?,?,?,?,?)", (session_id, memory_id, memory["kind"], text, memory["status"], json_text(evidence), json_text(supersedes), revision, job["through_seq"]))
            recorded = dict(db.execute("SELECT * FROM learning_memories WHERE session_id=? AND id=?", (session_id, memory_id)).fetchone())
            db.execute("INSERT INTO learning_memory_revisions VALUES(?,?,?,?)", (session_id, memory_id, revision, json_text(recorded)))
        has_sections = db.execute("SELECT 1 FROM learning_sections WHERE session_id=? LIMIT 1", (session_id,)).fetchone() is not None
        if analysis.get("protocol_version") == 2 and has_sections:
            touched = {(operation["section"], operation["entry_id"]) for operation in analysis["patch"].get("updates", [])}
            for operation in analysis["patch"].get("removals", []):
                db.execute("DELETE FROM learning_sections WHERE session_id=? AND section=? AND entry_id=?", (session_id, operation["section"], operation["entry_id"]))
            written = [entry for entry in entries if (entry["section"], entry["entry_id"]) in touched]
        else:
            db.execute("DELETE FROM learning_sections WHERE session_id=?", (session_id,))
            written = entries
        normalized = {(section, _key(section, item)): item for section in ("steps", "questions", "proposals", "inputs", "helpers") for item in workflow[section]}
        for entry in written:
            # Use validated normalized values (e.g. default_json -> default).
            value = entry["value"]
            if entry["section"] in SCALARS:
                value = workflow[entry["section"]]
            elif entry["section"] in {"steps", "questions", "proposals", "inputs", "helpers"}:
                value = normalized.get((entry["section"], entry["entry_id"]), value)
            db.execute("INSERT OR REPLACE INTO learning_sections VALUES(?,?,?,?,?,?)", (session_id, entry["section"], entry["entry_id"], entry["position"], json_text(value), revision))
        constraints = [dict(row) for row in db.execute("SELECT id,text,status,evidence_json,supersedes_json FROM learning_memories WHERE session_id=? AND kind IN ('constraint','decision') AND status IN ('active','pending') ORDER BY id", (session_id,))]
        context = db.execute("SELECT sha256 FROM learning_job_contexts WHERE job_id=?", (_job_id(job),)).fetchone()
        db.execute("INSERT INTO learning_snapshots VALUES(?,?,?,?,?,?,?)", (session_id, revision, job["through_seq"], job.get("constraint_revision", 0), context["sha256"] if context else "legacy", json_text(constraints), _now()))

    def retrieve_history(self, session_id: str, *, query: str = "", event_ids: list[str] | None = None, after_seq: int = 0, through_seq: int | None = None, limit: int = 8, max_chars: int = 16000) -> dict[str, Any]:
        if not 1 <= limit <= 32 or not 256 <= max_chars <= 64000 or not isinstance(query, str) or len(query) > 1000:
            raise ValueError("history retrieval exceeds its bounds")
        if event_ids is not None and (not isinstance(event_ids, list) or len(event_ids) > 32 or any(not isinstance(item, str) for item in event_ids)):
            raise ValueError("history event IDs must be a bounded list")
        with self._connection() as db:
            session = db.execute("SELECT latest_seq FROM sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise KeyError("unknown recording")
            boundary = session["latest_seq"] if through_seq is None else min(int(through_seq), session["latest_seq"])
            clauses = ["session_id=?", "seq>?", "seq<=?"]
            parameters: list[Any] = [session_id, max(0, after_seq), boundary]
            if event_ids:
                clauses.append("id IN (" + ",".join("?" for _ in event_ids) + ")"); parameters.extend(event_ids)
            if query:
                clauses.append("instr(lower(payload),lower(?))>0"); parameters.append(query)
            rows = db.execute("SELECT payload FROM events WHERE " + " AND ".join(clauses) + " ORDER BY seq DESC LIMIT ?", [*parameters, limit + 1]).fetchall()
            events = []
            for row in rows[:limit]:
                remaining = max_chars - len(json_text(events))
                if remaining < 160:
                    break
                events.append(_bounded(_load(row["payload"]), min(remaining - 40, max_chars // max(1, min(limit, len(rows))))))
            return {"session_id": session_id, "through_seq": boundary, "events": events, "has_more": len(rows) > len(events), "provenance": "stored_sanitized_events"}

    def project_task(self, session_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", session_id):
            raise ValueError("invalid task projection identity")
        with self._connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError("unknown recording")
            session = dict(row)
            draft = _load(session.get("draft"))
            memories = [dict(item) for item in db.execute("SELECT * FROM learning_memories WHERE session_id=? ORDER BY id", (session_id,))]
        root = Path(self.data_dir).resolve() / "tasks" / session_id
        for ancestor in (root, *root.parents):
            if ancestor == Path(self.data_dir).resolve().parent:
                break
            if ancestor.is_symlink() or getattr(ancestor, "is_junction", lambda: False)():
                raise ValueError("task projection cannot follow a link or junction")
        root.mkdir(parents=True, exist_ok=True)
        payloads = {"learning-state.json": json.dumps({key: value for key, value in session.items() if key != "draft"}, ensure_ascii=False, indent=2),
                    "memory.json": json.dumps(memories, ensure_ascii=False, indent=2)}
        if draft:
            payloads["draft.json"] = json.dumps(draft, ensure_ascii=False, indent=2)
            payloads["draft.md"] = render_markdown(draft)
        files = []
        for name, content in payloads.items():
            destination = root / name
            if destination.is_symlink() or getattr(destination, "is_junction", lambda: False)():
                raise ValueError("task projection target cannot be a link")
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False, suffix=".tmp") as handle:
                handle.write(content); temporary = Path(handle.name)
            try:
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            files.append({"path": str(destination), "sha256": hashlib.sha256(content.encode()).hexdigest()})
        return {"session_id": session_id, "files": files, "projection_only": True}
