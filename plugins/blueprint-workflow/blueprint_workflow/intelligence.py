"""Durable, task-scoped advice, explicit decisions, and research handoffs.

The mixin uses Store's existing transactions and event writer. Reading an update
does not acknowledge presentation; decisions never authorize different content.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import time
import uuid
from typing import Any

from .model import json_text, nonempty
from .privacy import sanitize


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def _hash(value: Any) -> str:
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


def init_intelligence(db) -> None:
    """Create additive tables; the owning Store migrates session columns."""
    statements = (
        """CREATE TABLE IF NOT EXISTS intelligence_items (
          session_id TEXT NOT NULL, item_id TEXT NOT NULL, content_revision INTEGER NOT NULL,
          kind TEXT NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL,
          validity TEXT NOT NULL DEFAULT 'current', decision TEXT NOT NULL DEFAULT 'pending',
          based_on_seq INTEGER NOT NULL, draft_revision INTEGER NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(session_id,item_id,content_revision))""",
        """CREATE TABLE IF NOT EXISTS intelligence_updates (
          cursor INTEGER PRIMARY KEY AUTOINCREMENT, update_id TEXT UNIQUE NOT NULL,
          session_id TEXT NOT NULL, item_id TEXT NOT NULL, content_revision INTEGER NOT NULL,
          change_kind TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS intelligence_presentations (
          session_id TEXT NOT NULL, update_id TEXT NOT NULL, reference TEXT NOT NULL,
          presented_at TEXT NOT NULL, PRIMARY KEY(session_id,update_id))""",
        """CREATE TABLE IF NOT EXISTS intelligence_decisions (
          session_id TEXT NOT NULL, item_id TEXT NOT NULL, content_revision INTEGER NOT NULL,
          content_hash TEXT NOT NULL, decision TEXT NOT NULL, user_instruction TEXT NOT NULL,
          user_event_id TEXT, confirmation_event_id TEXT NOT NULL, constraint_revision INTEGER NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(session_id,item_id,content_revision))""",
        """CREATE TABLE IF NOT EXISTS research_jobs (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_id TEXT NOT NULL,
          fingerprint TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL,
          based_on_seq INTEGER NOT NULL, draft_revision INTEGER NOT NULL,
          constraint_revision INTEGER NOT NULL, analysis_provider TEXT NOT NULL, analysis_model TEXT,
          worker_id TEXT, lease_until REAL, claim_token TEXT, result TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(session_id,fingerprint))""",
        "CREATE INDEX IF NOT EXISTS intelligence_task_updates ON intelligence_updates(session_id,cursor)",
        "CREATE INDEX IF NOT EXISTS research_task_state ON research_jobs(session_id,state)",
    )
    for statement in statements:
        db.execute(statement)


def _emit(db, sid: str, item_id: str, revision: int, change: str) -> None:
    db.execute("INSERT INTO intelligence_updates(update_id,session_id,item_id,content_revision,change_kind,created_at) VALUES(?,?,?,?,?,?)",
               (_id("update"), sid, item_id, revision, change, _now()))


def _content(kind: str, item: dict) -> dict:
    # Evidence can grow without generating another notification or voiding the
    # user's decision. A changed method, reason, scope, or question does void it.
    return {key: value for key, value in item.items()
            if key not in {"id", "evidence_event_ids", "status", "content_revision", "content_hash"}}


def _put_item(db, sid: str, kind: str, item: dict, seq: int, revision: int) -> str:
    item = sanitize(deepcopy(item))
    source_id = nonempty(item.get("id"), "insight.id")
    item_id = kind + ":" + source_id
    digest = _hash({"kind": kind, "content": _content(kind, item)})
    previous = db.execute("SELECT * FROM intelligence_items WHERE session_id=? AND item_id=? ORDER BY content_revision DESC LIMIT 1", (sid, item_id)).fetchone()
    if kind == "proposal" and item.get("status") in {"approved", "rejected"} and previous is not None and previous["content_hash"] != digest:
        exact = db.execute("SELECT 1 FROM intelligence_decisions WHERE session_id=? AND item_id=? AND content_hash=? AND decision=?", (sid, item_id, digest, item["status"])).fetchone()
        if exact is None:
            raise ValueError("an old proposal decision cannot authorize changed content")
    if previous is None:
        # Exact duplicates with a freshly invented ID retain their first identity.
        duplicate = db.execute("SELECT * FROM intelligence_items WHERE session_id=? AND kind=? AND content_hash=? AND validity='current' ORDER BY created_at LIMIT 1", (sid, kind, digest)).fetchone()
        if duplicate is not None:
            return duplicate["item_id"]
    if previous is not None and previous["content_hash"] == digest and previous["validity"] == "current":
        db.execute("UPDATE intelligence_items SET payload=?,based_on_seq=?,draft_revision=?,updated_at=? WHERE session_id=? AND item_id=? AND content_revision=?",
                   (json_text(item), seq, revision, _now(), sid, item_id, previous["content_revision"]))
        return item_id
    content_revision = (previous["content_revision"] + 1) if previous else 1
    if previous is not None:
        db.execute("UPDATE intelligence_items SET validity='superseded',updated_at=? WHERE session_id=? AND item_id=? AND validity='current'", (_now(), sid, item_id))
    db.execute("INSERT INTO intelligence_items(session_id,item_id,content_revision,kind,content_hash,payload,based_on_seq,draft_revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (sid, item_id, content_revision, kind, digest, json_text(item), seq, revision, _now(), _now()))
    _emit(db, sid, item_id, content_revision, "updated" if previous else "created")
    return item_id


def commit_insights(db, session_id: str, workflow: dict, job: dict, revision: int) -> None:
    """Persist advice in the SAME transaction as the corresponding draft."""
    present: set[str] = set()
    seq = job.get("through_seq", 0)
    ordered = [("question", item) for item in workflow.get("questions", [])]
    ordered += [("proposal", item) for item in workflow.get("proposals", [])]
    for kind, item in ordered:
        present.add(_put_item(db, session_id, kind, item, seq, revision))
    # Optional protocol-2 findings are advisory and cannot authorize changes.
    for item in job.get("insights", []):
        if item.get("kind") not in {"question", "suggestion", "finding"}:
            raise ValueError("unknown insight kind")
        if item.get("kind") == "suggestion" and "proposal:" + str(item.get("id")) in present:
            continue
        kind = "notice"
        present.add(_put_item(db, session_id, kind, item, seq, revision))
    for row in db.execute("SELECT * FROM intelligence_items WHERE session_id=? AND validity='current'", (session_id,)).fetchall():
        if row["item_id"] not in present:
            if row["kind"] == "notice":
                continue  # Protocol-2 notices are deltas, not a full active list.
            state = "resolved" if row["kind"] == "question" else "superseded"
            db.execute("UPDATE intelligence_items SET validity=?,updated_at=? WHERE session_id=? AND item_id=? AND content_revision=?",
                       (state, _now(), session_id, row["item_id"], row["content_revision"]))
            # Previously presented advice gets a retraction; invisible stale
            # advice simply disappears from the pending inbox.
            shown = db.execute("SELECT 1 FROM intelligence_presentations p JOIN intelligence_updates u ON p.update_id=u.update_id WHERE u.session_id=? AND u.item_id=? AND u.content_revision=? LIMIT 1", (session_id, row["item_id"], row["content_revision"])).fetchone()
            if shown:
                _emit(db, session_id, row["item_id"], row["content_revision"], state)


def enqueue_research_requests(db, session_id: str, requests: list, job: dict, revision: int) -> None:
    from .research import validate_research_request
    row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise KeyError("unknown session")
    if dict(row).get("capture_state", row["status"]) != "recording":
        return
    current_constraint = dict(row).get("constraint_revision", 0)
    for raw in requests:
        request = validate_research_request(raw)
        for evidence_id in request["evidence_event_ids"]:
            if not db.execute("SELECT 1 FROM events WHERE session_id=? AND id=? AND seq<=?", (session_id, evidence_id, job["through_seq"])).fetchone():
                raise ValueError("research request cites evidence outside its task snapshot")
        digest = _hash({key: value for key, value in request.items() if key not in {"id", "evidence_event_ids"}} | {"constraint_revision": current_constraint})
        db.execute("UPDATE research_jobs SET state='cancelled',updated_at=? WHERE session_id=? AND request_id=? AND fingerprint<>? AND state IN ('queued','claimed')", (_now(), session_id, request["id"], digest))
        db.execute("INSERT OR IGNORE INTO research_jobs(id,session_id,request_id,fingerprint,request,state,based_on_seq,draft_revision,constraint_revision,analysis_provider,analysis_model,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?,?)",
                   (_id("research"), session_id, request["id"], digest, json_text(request), job["through_seq"], revision, current_constraint,
                    row["analysis_provider"], row["analysis_model"], _now(), _now()))


class IntelligenceMixin:
    init_intelligence = staticmethod(init_intelligence)
    commit_insights = staticmethod(commit_insights)
    enqueue_research_requests = staticmethod(enqueue_research_requests)

    @staticmethod
    def close_research(db, session_id: str) -> None:
        db.execute("UPDATE research_jobs SET state='cancelled',updated_at=? WHERE session_id=? AND state IN ('queued','claimed')", (_now(), session_id))

    def session_updates(self, session_id: str, after_cursor: int = 0, limit: int = 2) -> dict:
        if not isinstance(after_cursor, int) or after_cursor < 0 or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("invalid update cursor or limit")
        with self._connection() as db:
            session = self._session(db, session_id)
            results, cursor = [], after_cursor
            # A client cursor never silently acknowledges unseen advice. Pending
            # unacknowledged rows survive a crash after a poll but before speech.
            for update in db.execute("SELECT u.* FROM intelligence_updates u WHERE u.session_id=? AND (u.cursor>? OR NOT EXISTS (SELECT 1 FROM intelligence_presentations p WHERE p.session_id=u.session_id AND p.update_id=u.update_id)) ORDER BY u.cursor", (session_id, after_cursor)):
                cursor = max(cursor, update["cursor"])
                if db.execute("SELECT 1 FROM intelligence_presentations WHERE session_id=? AND update_id=?", (session_id, update["update_id"])).fetchone():
                    continue
                item = db.execute("SELECT * FROM intelligence_items WHERE session_id=? AND item_id=? AND content_revision=?", (session_id, update["item_id"], update["content_revision"])).fetchone()
                if item is None:
                    continue
                retraction = update["change_kind"] in {"resolved", "superseded"}
                if not retraction and (item["validity"] != "current" or item["decision"] != "pending"):
                    continue
                results.append({**dict(update), "kind": item["kind"], "content_hash": item["content_hash"],
                                "validity": item["validity"], "decision": item["decision"], "content": json.loads(item["payload"]),
                                "based_on_seq": item["based_on_seq"], "draft_revision": item["draft_revision"]})
                if len(results) >= limit:
                    break
            return {"session_id": session_id, "updates": results, "next_cursor": cursor,
                    "processed_seq": session["processed_seq"], "latest_seq": session["latest_seq"],
                    "constraint_revision": dict(session).get("constraint_revision", 0), "presentation_acknowledged": False}

    def updates_ack(self, session_id: str, update_ids: list[str], presentation_reference: str) -> dict:
        nonempty(presentation_reference, "presentation_reference")
        if not isinstance(update_ids, list) or not update_ids or len(update_ids) != len(set(update_ids)):
            raise ValueError("update_ids must be a nonempty unique array")
        with self._transaction() as db:
            self._session(db, session_id)
            for update_id in update_ids:
                if not db.execute("SELECT 1 FROM intelligence_updates WHERE session_id=? AND update_id=?", (session_id, update_id)).fetchone():
                    raise ValueError("update belongs to another task or does not exist")
                db.execute("INSERT OR IGNORE INTO intelligence_presentations VALUES(?,?,?,?)", (session_id, update_id, str(sanitize(presentation_reference)), _now()))
            return {"session_id": session_id, "acknowledged_update_ids": update_ids, "presentation_evidence": "agent_reported"}

    def decide_proposal(self, session_id: str, proposal_id: str, decision: str, user_instruction: str,
                        content_revision: int | None = None, content_hash: str | None = None,
                        user_event_id: str | None = None) -> dict:
        if decision not in {"approved", "rejected", "deferred"}:
            raise ValueError("decision must be approved, rejected, or deferred")
        nonempty(user_instruction, "user_instruction")
        if content_revision is None and content_hash is None:
            raise ValueError("the displayed content_revision or content_hash is required")
        item_id = proposal_id if proposal_id.startswith("proposal:") else "proposal:" + proposal_id
        with self._transaction() as db:
            session = self._session(db, session_id)
            if dict(session).get("capture_state", session["status"]) != "recording":
                raise ValueError("decisions require an active recording; resume or revise first")
            item = db.execute("SELECT * FROM intelligence_items WHERE session_id=? AND item_id=? ORDER BY content_revision DESC LIMIT 1", (session_id, item_id)).fetchone()
            if item is None or item["validity"] != "current":
                raise ValueError("proposal is missing or no longer current")
            if content_revision is not None and content_revision != item["content_revision"] or content_hash is not None and content_hash != item["content_hash"]:
                raise ValueError("proposal content changed; review the current version")
            if user_event_id:
                source = db.execute("SELECT payload FROM events WHERE session_id=? AND id=?", (session_id, user_event_id)).fetchone()
                source = json.loads(source["payload"]) if source else None
                # Compare decoded user text, not an escaped JSON representation.
                if not source or source.get("actor") != "human" or user_instruction not in (source.get("summary", "") + "\n" + str(source.get("details", {}).get("text", ""))):
                    raise ValueError("user_event_id does not support this quoted instruction")
            previous = db.execute("SELECT * FROM intelligence_decisions WHERE session_id=? AND item_id=? AND content_revision=?", (session_id, item_id, item["content_revision"])).fetchone()
            if previous and previous["decision"] == decision:
                return {"session_id": session_id, "proposal_id": item_id, "decision": decision,
                        "constraint_revision": previous["constraint_revision"], "confirmation_event_id": previous["confirmation_event_id"], "idempotent": True}
            revision = dict(session).get("constraint_revision", 0) + (decision != "deferred")
            details = {"proposal_id": item_id.split(":", 1)[1], "decision": decision, "source": "explicit_user_confirmation",
                       "content_revision": item["content_revision"], "content_hash": item["content_hash"],
                       "user_event_id": user_event_id, "quote_evidence": "host_event" if user_event_id else "agent_reported_quote"}
            event = self._append(db, session_id, [{"kind": "confirmation" if decision != "deferred" else "proposal_deferred", "actor": "human", "summary": user_instruction, "details": details}])[0]
            db.execute("INSERT OR REPLACE INTO intelligence_decisions VALUES(?,?,?,?,?,?,?,?,?,?)", (session_id, item_id, item["content_revision"], item["content_hash"], decision, str(sanitize(user_instruction)), user_event_id, event["id"], revision, _now()))
            db.execute("UPDATE intelligence_items SET decision=?,updated_at=? WHERE session_id=? AND item_id=? AND content_revision=?", (decision, _now(), session_id, item_id, item["content_revision"]))
            db.execute("UPDATE sessions SET constraint_revision=? WHERE id=?", (revision, session_id))
            if decision != "deferred":
                db.execute("UPDATE research_jobs SET state='cancelled',updated_at=? WHERE session_id=? AND state IN ('queued','claimed') AND constraint_revision<>?", (_now(), session_id, revision))
            return {"session_id": session_id, "proposal_id": item_id, "decision": decision, "constraint_revision": revision,
                    "confirmation_event_id": event["id"], "draft_update_pending": decision != "deferred"}

    def intelligence_context(self, db, session_id: str, max_items: int = 20, max_chars: int = 12000,
                             through_seq: int | None = None) -> dict:
        if through_seq is not None and (not isinstance(through_seq, int) or isinstance(through_seq, bool) or through_seq < 0):
            raise ValueError("invalid decision evidence boundary")
        decisions = []
        guards = []
        size, truncated = 0, False
        count = 0
        for row in db.execute("SELECT d.*,i.payload,i.validity,e.seq AS confirmation_seq FROM intelligence_decisions d JOIN intelligence_items i ON d.session_id=i.session_id AND d.item_id=i.item_id AND d.content_revision=i.content_revision JOIN events e ON e.session_id=d.session_id AND e.id=d.confirmation_event_id WHERE d.session_id=? ORDER BY d.created_at DESC", (session_id,)):
            value = dict(row)
            value["proposal"] = json.loads(value.pop("payload"))
            if count >= max_items or size + len(json_text(value)) > max_chars:
                truncated = True
                break
            count += 1
            if through_seq is None or value["confirmation_seq"] <= through_seq:
                decisions.append(value)
            if value["decision"] in {"approved", "rejected"}:
                # The latest route guard is immediately effective. It is NOT
                # proof that a historical analysis batch consumed this event.
                guards.append({"item_id": value["item_id"], "content_revision": value["content_revision"],
                    "content_hash": value["content_hash"], "decision": value["decision"],
                    "user_instruction": value["user_instruction"], "proposal": value["proposal"],
                    "constraint_revision": value["constraint_revision"], "applies_after_seq": value["confirmation_seq"],
                    "not_batch_evidence": True, "rule": "Protect the user's route; do not cite this guard as an event or mark future events processed"})
            size += len(json_text(value))
        return {"decisions": decisions, "batch_decisions": decisions, "latest_effective_guards": guards,
                "through_seq": through_seq, "preserve_confirmed_route": True, "truncated": truncated}

    def research_next(self, session_id: str, worker_id: str = "foreground", lease_seconds: float = 300) -> dict:
        nonempty(worker_id, "worker_id")
        if not isinstance(lease_seconds, (int, float)) or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid research lease")
        with self._transaction() as db:
            session = self._session(db, session_id)
            if dict(session).get("capture_state", session["status"]) != "recording":
                return {"status": "inactive", "session_id": session_id}
            revision = dict(session).get("constraint_revision", 0)
            db.execute("UPDATE research_jobs SET state='cancelled',updated_at=? WHERE session_id=? AND state IN ('queued','claimed') AND constraint_revision<>?", (_now(), session_id, revision))
            active = db.execute("SELECT * FROM research_jobs WHERE session_id=? AND state='claimed' AND lease_until>?", (session_id, time.time())).fetchone()
            if active:
                return {"status": "busy", "session_id": session_id, "research_id": active["id"]}
            db.execute("UPDATE research_jobs SET state='queued',worker_id=NULL,claim_token=NULL,lease_until=NULL WHERE session_id=? AND state='claimed' AND lease_until<=?", (session_id, time.time()))
            item = db.execute("SELECT * FROM research_jobs WHERE session_id=? AND state='queued' ORDER BY based_on_seq DESC,created_at LIMIT 1", (session_id,)).fetchone()
            if not item:
                return {"status": "idle", "session_id": session_id}
            token = _id("claim")
            db.execute("UPDATE research_jobs SET state='claimed',worker_id=?,claim_token=?,lease_until=?,updated_at=? WHERE id=?", (worker_id, token, time.time() + lease_seconds, _now(), item["id"]))
            value = dict(item)
            value.update(status="research_ready", state="claimed", worker_id=worker_id, claim_token=token, lease_until=time.time() + lease_seconds)
            value["request"] = json.loads(value["request"])
            value["effective_constraints"] = self.intelligence_context(db, session_id)
            value["evidence_snapshot"] = []
            for event_id in value["request"]["evidence_event_ids"][:10]:
                source = db.execute("SELECT payload FROM events WHERE session_id=? AND id=?", (session_id, event_id)).fetchone()
                if source:
                    event = json.loads(source["payload"])
                    value["evidence_snapshot"].append({"event_id": event_id, "actor": event.get("actor"), "kind": event.get("kind"), "summary": event.get("summary", "")[:800]})
            return value

    def research_report(self, session_id: str, research_id: str, result: dict,
                        worker_id: str = "foreground", claim_token: str | None = None) -> dict:
        from .research import validate_research_result
        with self._transaction() as db:
            session = self._session(db, session_id)
            job = db.execute("SELECT * FROM research_jobs WHERE session_id=? AND id=?", (session_id, research_id)).fetchone()
            if not job:
                raise ValueError("research job is outside this task")
            if job["worker_id"] != worker_id or not claim_token or job["claim_token"] != claim_token:
                raise ValueError("research claim does not match")
            if job["state"] not in {"claimed", "cancelled"} or (job["state"] == "claimed" and job["lease_until"] < time.time()):
                raise ValueError("research claim expired or was already reported")
            result = validate_research_result(result, json.loads(job["request"]))
            current = dict(session).get("capture_state", session["status"]) == "recording" and job["state"] == "claimed" and job["constraint_revision"] == dict(session).get("constraint_revision", 0)
            state = result["status"] if current else "historical"
            db.execute("UPDATE research_jobs SET state=?,result=?,updated_at=? WHERE id=?", (state, json_text(result), _now(), research_id))
            event_id = None
            if current:
                event = self._append(db, session_id, [{"actor": "tool", "kind": "research_evidence", "summary": result["summary"],
                    "client_event_id": "research-result:" + research_id,
                    "details": {"origin": "research", "research_id": research_id, "request_id": job["request_id"], "result": result,
                                "authority": "agent_reported_research", "not_user_authorization": True}}])[0]
                event_id = event["id"]
            return {"session_id": session_id, "research_id": research_id, "status": state,
                    "event_id": event_id, "workflow_changed": False, "historical_only": not current}

    def research_queue(self, session_id: str) -> dict:
        with self._connection() as db:
            self._session(db, session_id)
            return {"session_id": session_id, "jobs": [{**dict(row), "request": json.loads(row["request"]), "result": json.loads(row["result"]) if row["result"] else None}
                    for row in db.execute("SELECT * FROM research_jobs WHERE session_id=? ORDER BY created_at", (session_id,))]}
