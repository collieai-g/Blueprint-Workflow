"""Transactional task evidence, incremental drafts, immutable versions and runs."""
from __future__ import annotations

from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import re
import time
from typing import Any
import uuid

from .model import json_text, nonempty, render_markdown, resolve_inputs, validate_analysis
from .privacy import sanitize
from .capture import CaptureMixin
from .learning import LearningMixin
from .intelligence import IntelligenceMixin


MAX_EVENT_BYTES = 128 * 1024
_INHERIT_MODEL = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def _load(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value is not None else deepcopy(default)


class Store(CaptureMixin, LearningMixin, IntelligenceMixin):
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        source = Path(__file__).resolve().parent.parent
        if self.data_dir == source or self.data_dir.is_relative_to(source):
            raise ValueError("private data_dir must be outside plugin source")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "workflow.sqlite3"
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
              host_session_id TEXT, status TEXT NOT NULL,
              latest_seq INTEGER NOT NULL DEFAULT 0, processed_seq INTEGER NOT NULL DEFAULT 0,
              draft_revision INTEGER NOT NULL DEFAULT 0, draft TEXT,
              final_seq INTEGER, final_request TEXT,
              workflow_id TEXT NOT NULL, workflow_version INTEGER NOT NULL,
              retry_attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
              last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              last_event_at REAL NOT NULL DEFAULT 0,
              analysis_provider TEXT NOT NULL DEFAULT 'codex', analysis_model TEXT
              ,capture_state TEXT NOT NULL DEFAULT 'recording', analysis_state TEXT NOT NULL DEFAULT 'queued',
              constraint_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS sessions_host ON sessions(host_session_id, created_at);
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
              client_id TEXT, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
              UNIQUE(session_id,seq), UNIQUE(session_id,client_id)
            );
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, worker_id TEXT NOT NULL,
              state TEXT NOT NULL, from_seq INTEGER NOT NULL, through_seq INTEGER NOT NULL,
              base_revision INTEGER NOT NULL, lease_until REAL NOT NULL, created_at TEXT NOT NULL,
              analysis_provider TEXT NOT NULL DEFAULT 'codex', analysis_model TEXT
              ,constraint_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS jobs_session ON jobs(session_id,state);
            CREATE TABLE IF NOT EXISTS revisions (
              session_id TEXT NOT NULL, revision INTEGER NOT NULL, through_seq INTEGER NOT NULL,
              workflow TEXT NOT NULL, change_summary TEXT NOT NULL, created_at TEXT NOT NULL,
              analysis_provider TEXT NOT NULL DEFAULT 'codex', analysis_model TEXT,
              PRIMARY KEY(session_id,revision)
            );
            CREATE TABLE IF NOT EXISTS workflows (
              id TEXT NOT NULL, version INTEGER NOT NULL, session_id TEXT NOT NULL,
              through_seq INTEGER NOT NULL, workflow TEXT NOT NULL, source_hash TEXT NOT NULL,
              created_at TEXT NOT NULL, PRIMARY KEY(id,version)
            );
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, version INTEGER NOT NULL,
              payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS confirmations (
              session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL,
              proposal_id TEXT NOT NULL, decision TEXT NOT NULL,
              PRIMARY KEY(session_id,event_id)
            );
            CREATE INDEX IF NOT EXISTS confirmations_task ON confirmations(session_id,seq);
            CREATE TABLE IF NOT EXISTS internal_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS provider_changes (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created_at TEXT NOT NULL,
              from_provider TEXT NOT NULL, from_model TEXT,
              to_provider TEXT NOT NULL, to_model TEXT
            );
            COMMIT;
            """)
            # One-time compatibility for private data created before the small
            # confirmation index existed. Ordinary analysis never rereads all
            # historical payloads to find a human decision.
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("SELECT 1 FROM internal_metadata WHERE key='confirmations_index_v1'").fetchone() is None:
                    for row in db.execute("SELECT payload FROM events"):
                        self._record_confirmation(db, _load(row["payload"]))
                    db.execute("INSERT INTO internal_metadata VALUES('confirmations_index_v1','1')")
                db.commit()
            except BaseException:
                db.rollback()
                raise
        self.migration_backup = self._migrate_provider_columns()
        self._migrate_learning_v3()

    def _migrate_learning_v3(self) -> None:
        """Additive, backed-up migration; never run alongside a legacy worker."""
        with self._schema_lock(), self._connection() as db:
            columns = {r['name'] for r in db.execute('PRAGMA table_info(sessions)')}
            needed = 'capture_state' not in columns
            if needed:
                from .worker import WorkerLock
                lock = WorkerLock(self.data_dir / 'worker.lock')
                if not lock.acquire():
                    raise RuntimeError('Stop the previous background worker before upgrading private data to 0.3.0')
                try:
                    if db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]:
                        backup = self.data_dir / 'backups' / ('workflow-pre-v3-' + uuid.uuid4().hex + '.sqlite3')
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        with closing(sqlite3.connect(backup)) as destination:
                            db.backup(destination)
                        self.migration_backup = str(backup)
                    db.execute('BEGIN IMMEDIATE')
                    for name, definition in {'capture_state': "TEXT NOT NULL DEFAULT 'recording'", 'analysis_state': "TEXT NOT NULL DEFAULT 'queued'", 'constraint_revision': 'INTEGER NOT NULL DEFAULT 0'}.items():
                        if name not in columns:
                            db.execute(f'ALTER TABLE sessions ADD COLUMN {name} {definition}')
                    db.execute("UPDATE sessions SET capture_state=CASE WHEN status IN ('recording','analysis_failed') THEN 'recording' WHEN status='paused' THEN 'paused' ELSE 'closed' END, analysis_state=CASE WHEN status='analysis_failed' THEN 'failed' WHEN status='blocked' THEN 'blocked' WHEN latest_seq=processed_seq THEN 'caught_up' ELSE 'queued' END")
                    for legacy in db.execute('SELECT id,latest_seq FROM sessions WHERE latest_seq>0').fetchall():
                        db.execute('INSERT OR IGNORE INTO internal_metadata VALUES(?,?)', ('capture-v3-legacy:' + legacy['id'],
                            json_text({'through_seq': legacy['latest_seq'], 'completeness': 'legacy_saved_preview',
                                'original_unsaved_content_recoverable': False})))
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    lock.release()
            db.execute('BEGIN IMMEDIATE')
            try:
                job_columns = {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}
                if 'constraint_revision' not in job_columns:
                    db.execute('ALTER TABLE jobs ADD COLUMN constraint_revision INTEGER NOT NULL DEFAULT 0')
                self.init_capture(db)
                self.init_learning(db)
                self.init_intelligence(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def _schema_lock(self):
        """Serialize backup/ALTER operations across local hosts and workers."""
        path = self.data_dir / "schema-migration.lock"
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 30
            while True:
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("timed out waiting for private schema migration")
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _migrate_provider_columns(self) -> str | None:
        """Back up an existing 0.1 database before its additive profile migration.

        SQLite backup happens outside a transaction; ALTER and backfill are one
        transaction. A fresh schema already has these columns and needs no backup.
        """
        definitions = {"analysis_provider": "TEXT NOT NULL DEFAULT 'codex'", "analysis_model": "TEXT"}
        with self._schema_lock(), self._connection() as db:
            missing = {table: [name for name in definitions if name not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}]
                       for table in ("sessions", "jobs", "revisions")}
            if not any(missing.values()):
                return None
            backup_dir = self.data_dir / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            name = "workflow-pre-provider-v2-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".sqlite3"
            backup_path = backup_dir / name
            destination = sqlite3.connect(backup_path)
            try:
                db.backup(destination)
            finally:
                destination.close()
            db.execute("BEGIN IMMEDIATE")
            try:
                for table, columns in missing.items():
                    for column in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definitions[column]}")
                for table in ("jobs", "revisions"):
                    for column in missing[table]:
                        fallback = "'codex'" if column == "analysis_provider" else "NULL"
                        db.execute(f"UPDATE {table} SET {column}=COALESCE((SELECT {column} FROM sessions WHERE sessions.id={table}.session_id),{fallback})")
                db.execute("INSERT OR REPLACE INTO internal_metadata VALUES('provider_schema_v2_backup',?)", (str(backup_path),))
                db.commit()
            except BaseException:
                db.rollback()
                raise
            return str(backup_path)

    @staticmethod
    def _analysis_profile(provider: Any, model: Any) -> tuple[str, str | None]:
        if not isinstance(provider, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", provider):
            raise ValueError("analysis_provider must be a canonical provider identifier")
        if model is not None:
            nonempty(model, "analysis_model")
            if len(model) > 200 or any(ord(char) < 32 for char in model):
                raise ValueError("analysis_model must be a short printable model identifier")
            if sanitize(model) != model:
                raise ValueError("analysis_model must not contain credentials")
        return provider, model

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _session(db, session_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError("unknown session: " + session_id)
        return row

    @staticmethod
    def _status(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["session_id"] = result.pop("id")
        result["draft"] = _load(result["draft"])
        final_request = _load(result.pop("final_request", None), {})
        if result["final_seq"] is not None:
            result["finalization_mode"] = "reviewed_draft" if final_request.get("draft_review") is not None else "background_analysis"
        draft = result["draft"] or {}
        result["questions"] = draft.get("questions", [])
        result["proposals"] = draft.get("proposals", [])
        result["pending_event_count"] = result["latest_seq"] - result["processed_seq"]
        result['capture_state'] = result.get('capture_state', 'recording' if result['status'] in {'recording', 'analysis_failed'} else 'closed')
        result['analysis_state'] = result.get('analysis_state', 'queued')
        return result

    def start_session(self, title: str, goal: str, host_session_id: str | None = None,
                      analysis_provider: str = "codex", analysis_model: str | None = None) -> dict[str, Any]:
        title, goal = nonempty(title, "title"), nonempty(goal, "goal")
        analysis_provider, analysis_model = self._analysis_profile(analysis_provider, analysis_model)
        if host_session_id is not None:
            nonempty(host_session_id, "host_session_id")
        with self._transaction() as db:
            if host_session_id:
                active = db.execute("SELECT id FROM sessions WHERE host_session_id=? AND status IN ('recording','paused','finalizing','blocked','analysis_failed')", (host_session_id,)).fetchone()
                if active:
                    raise ValueError("this host already has an active task; resume it or use another host session")
            session_id = _id("task")
            now = _now()
            db.execute("INSERT INTO sessions(id,title,goal,host_session_id,status,workflow_id,workflow_version,created_at,updated_at,analysis_provider,analysis_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (session_id, sanitize(title), sanitize(goal), host_session_id, "recording", _id("workflow"), 1, now, now, analysis_provider, analysis_model))
            self._append(db, session_id, [{"kind": "task_started", "actor": "human", "summary": goal,
                                        "details": {"title": title, "source": "explicit_task_request"}}])
            return self._status(self._session(db, session_id))

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            return [self._status(row) for row in db.execute("SELECT * FROM sessions ORDER BY created_at DESC")]

    def find_session_by_host(self, host_session_id: str) -> dict[str, Any] | None:
        nonempty(host_session_id, "host_session_id")
        with self._connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE host_session_id=? ORDER BY created_at DESC LIMIT 1", (host_session_id,)).fetchone()
            return self._status(row) if row else None

    def status(self, session_id: str) -> dict[str, Any]:
        with self._connection() as db:
            result = self._status(self._session(db, session_id))
            oldest = db.execute('SELECT payload FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 1', (session_id, result['processed_seq'])).fetchone()
            last_analysis = db.execute('SELECT created_at FROM revisions WHERE session_id=? ORDER BY revision DESC LIMIT 1', (session_id,)).fetchone()
            result['last_analysis_success_at'] = last_analysis[0] if last_analysis else None
            result['effective_decisions'] = self.intelligence_context(db, session_id)
            result['memory_summary'] = [dict(row) for row in db.execute("SELECT id,kind,text,status FROM learning_memories WHERE session_id=? AND status IN ('active','pending') ORDER BY revision DESC LIMIT 20", (session_id,))]
        result['workspace'] = str(self.data_dir / 'tasks' / session_id)
        result['capture'] = self.capture_status(session_id)
        result['analysis_lag_seconds'] = max(0, time.time() - datetime.fromisoformat(_load(oldest[0])['created_at']).timestamp()) if oldest else 0
        result['updates'] = self.session_updates(session_id, limit=2)
        return result

    def get_events(self, session_id: str, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        if not isinstance(after_seq, int) or after_seq < 0 or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("invalid event page")
        with self._connection() as db:
            self._session(db, session_id)
            return [_load(row[0]) for row in db.execute("SELECT payload FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?", (session_id, after_seq, limit))]

    def _append(self, db, session_id: str, events: list[dict[str, Any]], *, allow_closed: bool = False) -> list[dict[str, Any]]:
        session = self._session(db, session_id)
        latest = session["latest_seq"]
        added = []
        for raw in events:
            if not isinstance(raw, dict):
                raise ValueError("each event must be an object")
            clean = sanitize(self.externalize_event(session_id, deepcopy(raw), db=db))
            # IDs and ordering are controlled by the store, not recorded content.
            for key in ("seq", "session_id", "created_at", "id"):
                clean.pop(key, None)
            actor = clean.setdefault("actor", "agent")
            if actor not in {"human", "agent", "host", "system", "tool"}:
                raise ValueError("event.actor must be human, agent, host, system, or tool")
            nonempty(clean.get("kind"), "event.kind")
            nonempty(clean.get("summary"), "event.summary")
            if "details" in clean and not isinstance(clean["details"], dict):
                raise ValueError("event.details must be an object")
            client_id = clean.pop("client_event_id", None)
            event_alias = clean.pop("event_id", None)
            if event_alias is not None:
                nonempty(event_alias, "event_id")
            if client_id is not None and event_alias is not None and client_id != event_alias:
                raise ValueError("event_id and client_event_id must agree when both are supplied")
            if client_id is None:
                client_id = event_alias
            if client_id is not None:
                nonempty(client_id, "client_event_id")
                if len(client_id) > 255:
                    raise ValueError("client_event_id exceeds 255 characters")
            fingerprint = hashlib.sha256(json_text(clean).encode()).hexdigest()
            if client_id:
                existing = db.execute("SELECT fingerprint,payload FROM events WHERE session_id=? AND client_id=?", (session_id, client_id)).fetchone()
                if existing:
                    if existing["fingerprint"] != fingerprint:
                        raise ValueError("duplicate event ID has different content")
                    added.append(_load(existing["payload"]))
                    continue
            if not allow_closed and session["capture_state"] != "recording":
                raise ValueError("session is not recording; resume a paused/blocked task or create a revision")
            if clean.get("corrects_event_id"):
                found = db.execute("SELECT 1 FROM events WHERE session_id=? AND id=?", (session_id, clean["corrects_event_id"])).fetchone()
                if not found:
                    raise ValueError("corrected event is outside this task")
            latest += 1
            event = {**clean, "id": _id("event"), "session_id": session_id, "seq": latest, "created_at": _now()}
            if client_id:
                event["client_event_id"] = client_id
            encoded = json_text(event)
            if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
                raise ValueError("event exceeds 128 KiB; submit a concise semantic summary and artifact references")
            db.execute("INSERT INTO events(id,session_id,seq,client_id,fingerprint,payload) VALUES(?,?,?,?,?,?)",
                       (event["id"], session_id, latest, client_id, fingerprint, encoded))
            self._record_confirmation(db, event)
            if actor == 'human' and clean['kind'] in {'instruction', 'correction'}:
                db.execute('UPDATE sessions SET constraint_revision=constraint_revision+1 WHERE id=?', (session_id,))
            added.append(event)
        if latest != session["latest_seq"]:
            db.execute("UPDATE sessions SET latest_seq=?,updated_at=?,last_event_at=? WHERE id=?", (latest, _now(), time.time(), session_id))
            db.execute("UPDATE sessions SET analysis_state=CASE WHEN analysis_state='failed' THEN 'failed' ELSE 'queued' END WHERE id=?", (session_id,))
        return added

    def append_events(self, session_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(events, list) or not 1 <= len(events) <= 1000:
            raise ValueError("events must contain 1 to 1000 objects")
        with self.capture_boundary(session_id):
            drained = self.drain_capture(session_id)
            if not drained.get('complete', True):
                raise RuntimeError('Capture inbox has unresolved receipts; inspect capture_status before appending')
            with self._transaction() as db:
                saved = self._append(db, session_id, events)
                return {"session_id": session_id, "events": saved, "latest_seq": self._session(db, session_id)["latest_seq"]}

    def pause_session(self, session_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            row = self._session(db, session_id)
            if row["capture_state"] not in {"recording", "paused"}:
                raise ValueError("only a recording task can be paused")
            db.execute("UPDATE sessions SET status='paused',capture_state='paused',updated_at=? WHERE id=?", (_now(), session_id))
            return self._status(self._session(db, session_id))

    def resume_session(self, session_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            row = self._session(db, session_id)
            if row["status"] not in {"recording", "paused", "blocked", "analysis_failed"}:
                raise ValueError("task cannot be resumed; retry failed analysis or create a revision")
            db.execute("UPDATE sessions SET status='recording',capture_state='recording',analysis_state='queued',final_seq=NULL,final_request=NULL,updated_at=? WHERE id=?", (_now(), session_id))
            return self._status(self._session(db, session_id))

    def finish_session(self, session_id: str, final_summary: str, artifacts: list | None = None,
                       draft_review: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.capture_boundary(session_id):
            drained = self.drain_capture(session_id)
            if not drained.get('complete', True):
                raise RuntimeError('Capture inbox has unresolved receipts; cannot finalize acknowledged work')
            result = self._finish_session(session_id, final_summary, artifacts, draft_review)
        self.project_task(session_id)
        return result

    def _finish_session(self, session_id: str, final_summary: str, artifacts: list | None = None,
                        draft_review: dict[str, Any] | None = None) -> dict[str, Any]:
        nonempty(final_summary, "final_summary")
        if artifacts is None:
            artifacts = []
        if not isinstance(artifacts, list):
            raise ValueError("artifacts must be an array")
        request = sanitize({"summary": final_summary, "artifacts": artifacts})
        if draft_review is not None:
            if not isinstance(draft_review, dict):
                raise ValueError("draft_review must be an object")
            request["draft_review"] = sanitize(draft_review)
        encoded = json_text(request)
        with self._transaction() as db:
            row = self._session(db, session_id)
            if row["final_seq"] is not None:
                if row["final_request"] != encoded:
                    raise ValueError("finalization already has another final result; create a revision")
                return self._status(row)
            if row["status"] not in {"recording", "paused", "analysis_failed"}:
                raise ValueError("task cannot be finalized in its current state")
            reviewed_workflow = None
            if draft_review is not None:
                review = request["draft_review"]
                if (isinstance(review.get("draft_revision"), bool) or not isinstance(review.get("draft_revision"), int)
                        or isinstance(review.get("through_seq"), bool) or not isinstance(review.get("through_seq"), int)
                        or review["draft_revision"] != row["draft_revision"] or row["draft_revision"] == 0
                        or review["through_seq"] != row["processed_seq"] or row["processed_seq"] != row["latest_seq"]):
                    raise ValueError("draft review is stale or background analysis has pending events; review the latest complete draft")
                existing = _load(row["draft"])
                parsed = validate_analysis({"workflow": existing, "change_summary": "Final review of the current draft"},
                    evidence_ids=self._evidence_ids(db, session_id, row["processed_seq"], existing),
                    confirmation_decisions=self._confirmation_decisions(db, session_id, row["processed_seq"]))
                reviewed_workflow = parsed["workflow"]
                if any(item["blocking"] for item in reviewed_workflow["questions"]):
                    raise ValueError("the reviewed draft still has blocking questions")
                checks = review.get("checks")
                if not isinstance(checks, list):
                    raise ValueError("draft_review.checks must be an array")
                criteria = set(reviewed_workflow["completion_criteria"])
                seen = set()
                for check in checks:
                    if not isinstance(check, dict) or check.get("criterion") not in criteria or check["criterion"] in seen:
                        raise ValueError("draft review must use exact, unique completion criteria")
                    seen.add(check["criterion"])
                    self._evidence(check.get("evidence"))
                if seen != criteria:
                    raise ValueError("draft review must check every completion criterion")
            completion_details = {"artifacts": artifacts, "source": "explicit_completion_request"}
            if draft_review is not None:
                completion_details["draft_review"] = request["draft_review"]
                completion_details["review_provenance"] = "foreground_agent_result_review"
            saved = self._append(db, session_id, [{"kind": "demonstration_finished", "actor": "human", "summary": final_summary,
                "details": completion_details}], allow_closed=True)
            final_seq = saved[0]["seq"]
            if reviewed_workflow is not None:
                revision = row["draft_revision"] + 1
                encoded_workflow = json_text(reviewed_workflow)
                db.execute("INSERT INTO revisions(session_id,revision,through_seq,workflow,change_summary,created_at,analysis_provider,analysis_model) VALUES(?,?,?,?,?,?,?,?)", (session_id, revision, final_seq, encoded_workflow,
                    "Finalized the up-to-date draft after foreground Agent result review; no background model request.", _now(), row["analysis_provider"], row["analysis_model"]))
                db.execute("INSERT INTO workflows VALUES(?,?,?,?,?,?,?)", (row["workflow_id"], row["workflow_version"], session_id,
                    final_seq, encoded_workflow, self._source_hash(db, session_id, final_seq), _now()))
                db.execute("UPDATE sessions SET status='ready_to_test',final_seq=?,final_request=?,processed_seq=?,draft_revision=?,draft=?,next_attempt_at=0,last_error=NULL,updated_at=? WHERE id=?",
                    (final_seq, encoded, final_seq, revision, encoded_workflow, _now(), session_id))
            else:
                db.execute("UPDATE sessions SET status='finalizing',final_seq=?,final_request=?,next_attempt_at=0,updated_at=? WHERE id=?", (final_seq, encoded, _now(), session_id))
            db.execute("UPDATE sessions SET capture_state='closed',analysis_state=? WHERE id=?", ('caught_up' if reviewed_workflow is not None else 'queued', session_id))
            self.close_research(db, session_id)
            return self._status(self._session(db, session_id))

    def pending_sessions(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM sessions WHERE latest_seq>processed_seq AND status IN ('recording','finalizing') ORDER BY CASE status WHEN 'finalizing' THEN 0 ELSE 1 END,created_at")
            return [self._status(row) for row in rows]

    def claim_job(self, worker_id: str, lease_seconds: float = 300) -> dict[str, Any] | None:
        nonempty(worker_id, "worker_id")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.drain_capture()
        now = time.time()
        with self._transaction() as db:
            db.execute("UPDATE jobs SET state='expired' WHERE state='claimed' AND lease_until<=?", (now,))
            row = db.execute("""SELECT s.* FROM sessions s WHERE s.status IN ('recording','finalizing')
                AND s.latest_seq>s.processed_seq AND s.next_attempt_at<=?
                AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.session_id=s.id AND j.state='claimed')
                ORDER BY CASE s.status WHEN 'finalizing' THEN 0 ELSE 1 END,s.updated_at LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            candidates = db.execute("SELECT payload FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 100", (row["id"], row["processed_seq"])).fetchall()
            events = []
            size = 0
            for candidate in candidates:
                encoded = candidate["payload"]
                encoded_size = len(encoded.encode("utf-8"))
                if events and size + encoded_size > 150000:
                    break
                events.append(_load(encoded))
                size += encoded_size
            through = events[-1]["seq"]
            job_id = _id("job")
            db.execute("INSERT INTO jobs(id,session_id,worker_id,state,from_seq,through_seq,base_revision,lease_until,created_at,analysis_provider,analysis_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (job_id, row["id"], worker_id, "claimed", row["processed_seq"] + 1,
                       through, row["draft_revision"], now + lease_seconds, _now(), row["analysis_provider"], row["analysis_model"]))
            db.execute('UPDATE jobs SET constraint_revision=? WHERE id=?', (row['constraint_revision'], job_id))
            db.execute("UPDATE sessions SET analysis_state='running' WHERE id=?", (row['id'],))
            job = {"job_id": job_id, "session_id": row["id"], "base_revision": row["draft_revision"],
                    "from_seq": row["processed_seq"] + 1, "through_seq": through, "events": events,
                    "previous_draft": _load(row["draft"]),
                    "finalizing": row["final_seq"] is not None and through >= row["final_seq"],
                    "at_final_boundary": row["final_seq"] is not None and through >= row["final_seq"],
                    "final_seq": row["final_seq"], "goal": row["goal"], "title": row["title"],
                    "retry_attempts": row["retry_attempts"], "analysis_provider": row["analysis_provider"],
                    "analysis_model": row["analysis_model"], 'constraint_revision': row['constraint_revision'],
                    'validation_feedback': row['last_error']}
            return self.prepare_learning_job(db, job)

    @staticmethod
    def _job(db, job_id: str, worker_id: str) -> sqlite3.Row:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise KeyError("unknown analysis job")
        if job["worker_id"] != worker_id or job["state"] != "claimed" or job["lease_until"] <= time.time():
            raise ValueError("analysis lease is stale or belongs to another worker")
        return job

    def heartbeat_job(self, job_id: str, worker_id: str, lease_seconds: float = 300) -> bool:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._transaction() as db:
            try:
                self._job(db, job_id, worker_id)
            except (ValueError, KeyError):
                return False
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() + lease_seconds, job_id))
            return True

    @staticmethod
    def _decisions(events: list[dict[str, Any]]) -> dict[str, str]:
        decisions = {}
        for event in events:
            details = event.get("details", {})
            if event.get("actor") == "human" and event.get("kind") == "confirmation" and details.get("source") in {"host_user_prompt", "explicit_user_confirmation"}:
                if details.get("decision") in {"approved", "rejected"} and isinstance(details.get("proposal_id"), str):
                    decisions[details["proposal_id"]] = details["decision"]
        return decisions

    @staticmethod
    def _record_confirmation(db, event: dict[str, Any]) -> None:
        for proposal_id, decision in Store._decisions([event]).items():
            db.execute("INSERT OR IGNORE INTO confirmations VALUES(?,?,?,?,?)", (event["session_id"], event["seq"], event["id"], proposal_id, decision))

    @staticmethod
    def _confirmation_decisions(db, session_id: str, through_seq: int) -> dict[str, str]:
        return {row["proposal_id"]: row["decision"] for row in db.execute(
            "SELECT proposal_id,decision FROM confirmations WHERE session_id=? AND seq<=? ORDER BY seq", (session_id, through_seq))}

    @staticmethod
    def _evidence_ids(db, session_id: str, through_seq: int, workflow: Any) -> set[str]:
        requested = set()
        if isinstance(workflow, dict):
            for collection in ("steps", "questions", "proposals", "helpers"):
                entries = workflow.get(collection, [])
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    ids = entry.get("evidence_event_ids", []) if isinstance(entry, dict) else []
                    if isinstance(ids, list):
                        requested.update(item for item in ids if isinstance(item, str))
        found = set()
        names = sorted(requested)
        for start in range(0, len(names), 800):
            group = names[start:start + 800]
            placeholders = ",".join("?" for _ in group)
            rows = db.execute(f"SELECT id FROM events WHERE session_id=? AND seq<=? AND id IN ({placeholders})", [session_id, through_seq, *group])
            found.update(row["id"] for row in rows)
        return found

    @staticmethod
    def _source_hash(db, session_id: str, through_seq: int) -> str:
        digest = hashlib.sha256()
        digest.update(b"[")
        first = True
        for row in db.execute("SELECT payload FROM events WHERE session_id=? AND seq<=? ORDER BY seq", (session_id, through_seq)):
            if not first:
                digest.update(b",")
            digest.update(row["payload"].encode("utf-8"))
            first = False
        digest.update(b"]")
        return digest.hexdigest()

    def commit_job(self, job_id: str, worker_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
        result = self._commit_job(job_id, worker_id, analysis)
        try:
            result['projection'] = self.project_task(result['session_id'])
        except (OSError, ValueError) as exc:
            result['projection_warning'] = str(sanitize(str(exc)))
        return result

    def _commit_job(self, job_id: str, worker_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
        with self._transaction() as db:
            job = self._job(db, job_id, worker_id)
            session = self._session(db, job["session_id"])
            if (session["processed_seq"] != job["from_seq"] - 1 or session["draft_revision"] != job["base_revision"]
                    or session['constraint_revision'] != job['constraint_revision']):
                raise ValueError("analysis snapshot is stale")
            job = dict(job)
            job['job_id'] = job['id']
            raw_analysis = sanitize(analysis)
            cleaned = sanitize(self.assemble_learning_analysis(db, job, raw_analysis))
            parsed = validate_analysis(cleaned,
                evidence_ids=self._evidence_ids(db, session["id"], job["through_seq"], cleaned.get("workflow") if isinstance(cleaned, dict) else None),
                confirmation_decisions=self._confirmation_decisions(db, session["id"], job["through_seq"]))
            workflow = parsed["workflow"]
            revision = session["draft_revision"] + 1
            self.commit_learning(db, session['id'], job, raw_analysis, workflow, revision)
            insight_job = {**job, 'insights': raw_analysis.get('insights', [])}
            self.commit_insights(db, session['id'], workflow, insight_job, revision)
            self.enqueue_research_requests(db, session['id'], raw_analysis.get('research_requests', []), job, revision)
            db.execute("INSERT INTO revisions(session_id,revision,through_seq,workflow,change_summary,created_at,analysis_provider,analysis_model) VALUES(?,?,?,?,?,?,?,?)", (session["id"], revision, job["through_seq"], json_text(workflow), parsed["change_summary"], _now(), job["analysis_provider"], job["analysis_model"]))
            status = session["status"]
            if session["final_seq"] is not None and job["through_seq"] >= session["final_seq"]:
                if any(item["blocking"] for item in workflow["questions"]):
                    status = "blocked"
                else:
                    status = "ready_to_test"
                    source_hash = self._source_hash(db, session["id"], job["through_seq"])
                    db.execute("INSERT INTO workflows VALUES(?,?,?,?,?,?,?)", (session["workflow_id"], session["workflow_version"], session["id"], job["through_seq"], json_text(workflow), source_hash, _now()))
            db.execute("UPDATE sessions SET processed_seq=?,draft_revision=?,draft=?,status=?,retry_attempts=0,next_attempt_at=0,last_error=NULL,updated_at=? WHERE id=?",
                       (job["through_seq"], revision, json_text(workflow), status, _now(), session["id"]))
            db.execute("UPDATE jobs SET state='committed' WHERE id=?", (job_id,))
            db.execute('UPDATE sessions SET analysis_state=? WHERE id=?', ('caught_up' if session['latest_seq'] == job['through_seq'] else 'queued', session['id']))
            return self._status(self._session(db, session["id"]))

    def fail_job(self, job_id: str, worker_id: str, error: str, retry_delay: float | None = 5) -> dict[str, Any]:
        nonempty(error, "error")
        with self._transaction() as db:
            job = self._job(db, job_id, worker_id)
            session = self._session(db, job["session_id"])
            if 'analysis snapshot is stale' in error:
                db.execute("UPDATE jobs SET state='superseded' WHERE id=?", (job_id,))
                db.execute("UPDATE sessions SET analysis_state='queued',retry_attempts=0,last_error=NULL,next_attempt_at=0,updated_at=? WHERE id=?", (_now(), session['id']))
                return self._status(self._session(db, session['id']))
            status = "analysis_failed" if retry_delay is None and session['capture_state'] != 'paused' else session["status"]
            db.execute("UPDATE jobs SET state='failed' WHERE id=?", (job_id,))
            db.execute("UPDATE sessions SET status=?,retry_attempts=retry_attempts+1,last_error=?,next_attempt_at=?,updated_at=? WHERE id=?",
                       (status, sanitize(error[:4000]), time.time() + (retry_delay or 0), _now(), session["id"]))
            db.execute('UPDATE sessions SET analysis_state=? WHERE id=?', ('failed' if retry_delay is None else 'queued', session['id']))
            return self._status(self._session(db, session["id"]))

    def retry_analysis(self, session_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            session = self._session(db, session_id)
            if session["status"] not in {"analysis_failed", "recording", "finalizing", "paused"}:
                raise ValueError("there is no retryable analysis")
            status = "finalizing" if session["final_seq"] is not None else ('paused' if session['capture_state'] == 'paused' else 'recording')
            db.execute("UPDATE sessions SET status=?,analysis_state='queued',retry_attempts=0,next_attempt_at=0,last_error=NULL,updated_at=? WHERE id=?", (status, _now(), session_id))
            return self._status(self._session(db, session_id))

    def set_analysis_provider(self, session_id: str, analysis_provider: str,
                              analysis_model: str | None = None) -> dict[str, Any]:
        """Change a selected engine without rewriting evidence or claiming work.

        Provider changes are configuration audit records, not semantic task
        events, so an already-declared final evidence boundary stays intact.
        """
        provider, model = self._analysis_profile(analysis_provider, analysis_model)
        with self._transaction() as db:
            session = self._session(db, session_id)
            if session["status"] == "ready_to_test":
                raise ValueError("a finalized workflow keeps its engine provenance; create a new revision")
            if db.execute("SELECT 1 FROM jobs WHERE session_id=? AND state='claimed' AND lease_until>?", (session_id, time.time())).fetchone():
                raise ValueError("an analysis job is still running; pause the task and wait for its lease owner before changing the engine")
            status = session["status"]
            if status == "analysis_failed":
                status = "finalizing" if session["final_seq"] is not None else "recording"
            if (provider, model) != (session["analysis_provider"], session["analysis_model"]):
                db.execute("INSERT INTO provider_changes VALUES(?,?,?,?,?,?,?)", (_id("provider_change"), session_id,
                    _now(), session["analysis_provider"], session["analysis_model"], provider, model))
            db.execute("UPDATE sessions SET analysis_provider=?,analysis_model=?,status=?,retry_attempts=0,next_attempt_at=0,last_error=NULL,updated_at=? WHERE id=?",
                (provider, model, status, _now(), session_id))
            return self._status(self._session(db, session_id))

    def get_workflow(self, workflow_id: str, version: int | None = None) -> dict[str, Any]:
        with self._connection() as db:
            if version is None:
                row = db.execute("SELECT * FROM workflows WHERE id=? ORDER BY version DESC LIMIT 1", (workflow_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM workflows WHERE id=? AND version=?", (workflow_id, version)).fetchone()
            if row is None:
                raise KeyError("unknown workflow version")
            result = dict(row)
            result["workflow_id"] = result.pop("id")
            result["workflow"] = _load(result["workflow"])
            source_session = self._session(db, row["session_id"])
            result["analysis_provider"] = source_session["analysis_provider"]
            result["analysis_model"] = source_session["analysis_model"]
            runs = [_load(item[0]) for item in db.execute("SELECT payload FROM runs WHERE workflow_id=? AND version=? ORDER BY created_at DESC", (workflow_id, row["version"]))]
            passed = [run["id"] for run in runs if run["status"] == "verified"]
            terminal = next((run for run in runs if run["status"] in {"verified", "verification_failed", "verification_unknown"}), None)
            if passed:
                result["test_status"] = "passed_agent_reported"
            elif terminal:
                result["test_status"] = {"verification_failed": "failed_agent_reported", "verification_unknown": "unknown_agent_reported"}[terminal["status"]]
            else:
                result["test_status"] = "not_tested"
            result["verified_run_ids"] = passed
            result["latest_run"] = ({"run_id": runs[0]["id"], "status": runs[0]["status"],
                                     "verification_provenance": runs[0].get("verification_provenance")}
                                    if runs else None)
            return result

    def workflow_export(self, workflow_id: str, version: int | None = None, format: str = "json") -> str:
        record = self.get_workflow(workflow_id, version)
        if format == "json":
            return json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
        if format in {"markdown", "md"}:
            return render_markdown(record)
        raise ValueError("export format must be json or markdown")

    def revise_session(self, workflow_id: str, goal: str | None = None, host_session_id: str | None = None,
                       analysis_provider: str | None = None, analysis_model: Any = _INHERIT_MODEL) -> dict[str, Any]:
        record = self.get_workflow(workflow_id)
        workflow = record["workflow"]
        selected_provider = record["analysis_provider"] if analysis_provider is None else analysis_provider
        if analysis_model is _INHERIT_MODEL:
            selected_model = record["analysis_model"] if selected_provider == record["analysis_provider"] else None
        else:
            selected_model = analysis_model
        selected_provider, selected_model = self._analysis_profile(selected_provider, selected_model)
        # New evidence has its own IDs; the base version remains separately referenced.
        with self._transaction() as db:
            if host_session_id:
                active = db.execute("SELECT id FROM sessions WHERE host_session_id=? AND status IN ('recording','paused','finalizing','blocked','analysis_failed')", (host_session_id,)).fetchone()
                if active:
                    raise ValueError("this host already has an active task")
            next_version = db.execute("SELECT MAX(workflow_version)+1 FROM sessions WHERE workflow_id=?", (workflow_id,)).fetchone()[0]
            session_id, now = _id("task"), _now()
            db.execute("INSERT INTO sessions(id,title,goal,host_session_id,status,workflow_id,workflow_version,created_at,updated_at,analysis_provider,analysis_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (session_id, workflow["title"], sanitize(goal or workflow["goal"]), host_session_id, "recording", workflow_id, next_version, now, now, selected_provider, selected_model))
            self._append(db, session_id, [{"actor": "human", "kind": "revision_started", "summary": "Revise the confirmed workflow using new requirements.",
                "details": {"base_workflow_id": workflow_id, "base_version": record["version"], "base_workflow": workflow}}])
            return self._status(self._session(db, session_id))

    def _run(self, db, run_id: str) -> dict[str, Any]:
        row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("unknown run")
        return _load(row[0])

    @staticmethod
    def _save_run(db, run: dict[str, Any]) -> dict[str, Any]:
        run["updated_at"] = _now()
        db.execute("UPDATE runs SET payload=? WHERE id=?", (json_text(run), run["id"]))
        return deepcopy(run)

    def run_start(self, workflow_id: str, inputs: dict[str, Any], version: int | None = None) -> dict[str, Any]:
        record = self.get_workflow(workflow_id, version)
        supplied = resolve_inputs(record["workflow"], sanitize(inputs))
        now = _now()
        run = {"id": _id("run"), "workflow_id": workflow_id, "version": record["version"], "status": "running",
               "inputs": supplied, "workflow": record["workflow"], "steps": {}, "verification": None,
               "created_at": now, "updated_at": now}
        with self._transaction() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (run["id"], workflow_id, record["version"], json_text(run), now))
        return run

    def run_status(self, run_id: str) -> dict[str, Any]:
        with self._connection() as db:
            return self._run(db, run_id)

    @staticmethod
    def _condition(run: dict[str, Any], step: dict[str, Any]) -> bool | None:
        condition = step.get("when")
        if condition is None:
            return True
        if condition.get("input"):
            if condition["input"] not in run["inputs"]:
                return None
            actual = run["inputs"][condition["input"]]
        else:
            source = run["steps"].get(condition["step"], {})
            if condition["output"] not in source.get("outputs", {}):
                return None
            actual = source["outputs"][condition["output"]]
        return json_text(actual) == json_text(condition["equals"])

    def run_next(self, run_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            run = self._run(db, run_id)
            if run["status"] != "running":
                return {"run_id": run_id, "status": run["status"], "run": run}
            for step in run["workflow"]["steps"]:
                state = run["steps"].get(step["id"])
                if state:
                    if state["status"] in {"failed", "unknown", "claimed"}:
                        return {"run_id": run_id, "status": "awaiting_report" if state["status"] == "claimed" else "blocked", "step": step, "run": run}
                    continue
                dependencies = [run["steps"].get(item, {}).get("status") for item in step["depends_on"]]
                if not all(item in {"succeeded", "skipped"} for item in dependencies):
                    continue
                condition = self._condition(run, step)
                if condition is None:
                    return {"run_id": run_id, "status": "blocked_missing_condition", "step": step, "run": run}
                if condition is False or any(item == "skipped" for item in dependencies) and step.get("when") is None:
                    run["steps"][step["id"]] = {"status": "skipped", "observation": "Confirmed condition or dependency does not apply.", "outputs": {}, "evidence": []}
                    continue
                run["steps"][step["id"]] = {"status": "claimed", "claimed_at": _now(), "attempt": 1}
                self._save_run(db, run)
                return {"run_id": run_id, "status": "step_ready", "step": step, "run": run}
            if all(run["steps"].get(step["id"], {}).get("status") in {"succeeded", "skipped"} for step in run["workflow"]["steps"]):
                run["status"] = "awaiting_verification"
                self._save_run(db, run)
                return {"run_id": run_id, "status": "awaiting_verification", "run": run}
            return {"run_id": run_id, "status": "blocked_dependencies", "run": run}

    @staticmethod
    def _evidence(value: Any) -> None:
        if not isinstance(value, list) or not value:
            raise ValueError("observed evidence must be a nonempty array")
        for entry in value:
            if isinstance(entry, str):
                nonempty(entry, "evidence entry")
            elif isinstance(entry, dict):
                references = [entry.get(key) for key in ("description", "path", "artifact_id", "event_id", "url")]
                if not any(isinstance(item, str) and item.strip() for item in references):
                    raise ValueError("evidence objects need a description or result reference")
            else:
                raise ValueError("evidence entries must be descriptions or reference objects")

    @staticmethod
    def _result(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict) or result.get("status") not in {"succeeded", "failed", "unknown"}:
            raise ValueError("result requires succeeded, failed, or unknown status")
        nonempty(result.get("observation"), "result.observation")
        if not isinstance(result.get("evidence", []), list) or not isinstance(result.get("outputs", {}), dict):
            raise ValueError("evidence must be an array and outputs must be an object")
        if result["status"] == "succeeded":
            Store._evidence(result.get("evidence"))
        return result

    def run_report(self, run_id: str, step_id: str, result: dict[str, Any]) -> dict[str, Any]:
        parsed = self._result(sanitize(result))
        with self._transaction() as db:
            run = self._run(db, run_id)
            step = next((step for step in run["workflow"]["steps"] if step["id"] == step_id), None)
            if step is None:
                raise ValueError("unknown step")
            state = run["steps"].get(step_id)
            if state is None:
                raise ValueError("step must be obtained from run_next before reporting")
            if state["status"] != "claimed":
                if state.get("result_fingerprint") == hashlib.sha256(json_text(parsed).encode()).hexdigest():
                    return run
                raise ValueError("step already has a different result; explicitly retry a failed step")
            if run["status"] != "running":
                raise ValueError("run no longer accepts step reports")
            if parsed["status"] == "succeeded" and not set(step["outputs"]).issubset(parsed.get("outputs", {})):
                raise ValueError("successful step is missing declared outputs")
            run["steps"][step_id] = {**parsed, "attempt": state.get("attempt", 1), "reported_at": _now(),
                                    "result_fingerprint": hashlib.sha256(json_text(parsed).encode()).hexdigest()}
            return self._save_run(db, run)

    def run_retry(self, run_id: str, step_id: str, observation: str = "Explicit retry requested; inspect the current result before acting again.") -> dict[str, Any]:
        nonempty(observation, "observation")
        with self._transaction() as db:
            run = self._run(db, run_id)
            state = run["steps"].get(step_id)
            if run["status"] != "running" or not state or state["status"] not in {"failed", "unknown", "claimed"}:
                raise ValueError("only an incomplete or failed step can be explicitly retried")
            if state.get("attempt", 1) >= 10:
                raise ValueError("step retry limit reached; review the workflow before another run")
            history = state.get("history", []) + [{key: value for key, value in state.items() if key != "history"}]
            run["steps"][step_id] = {"status": "claimed", "attempt": state.get("attempt", 1) + 1,
                "claimed_at": _now(), "retry_observation": sanitize(observation), "history": history}
            return self._save_run(db, run)

    def run_verify(self, run_id: str, verification: dict[str, Any]) -> dict[str, Any]:
        verification = sanitize(verification)
        if not isinstance(verification, dict) or verification.get("status") not in {"passed", "failed", "unknown"}:
            raise ValueError("verification requires passed, failed, or unknown status")
        nonempty(verification.get("observation"), "verification.observation")
        if not isinstance(verification.get("checks"), list):
            raise ValueError("verification.checks must be an array")
        with self._transaction() as db:
            run = self._run(db, run_id)
            if run["status"] in {"verified", "verification_failed", "verification_unknown"}:
                if run["verification"] == verification:
                    return run
                raise ValueError("verification is immutable; start another run")
            if run["status"] != "awaiting_verification":
                raise ValueError("all applicable steps must finish before verification")
            criteria = set(run["workflow"]["completion_criteria"])
            seen = set()
            for check in verification["checks"]:
                if not isinstance(check, dict) or check.get("criterion") not in criteria or check["criterion"] in seen:
                    raise ValueError("verification criteria must be exact and unique")
                seen.add(check["criterion"])
                if check.get("status") not in {"passed", "failed", "unknown"}:
                    raise ValueError("check status is invalid")
                if check["status"] == "passed":
                    self._evidence(check.get("evidence"))
            if verification["status"] == "passed" and (seen != criteria or any(check["status"] != "passed" for check in verification["checks"])):
                raise ValueError("passing verification must cover every completion criterion")
            run["verification"] = verification
            run["status"] = {"passed": "verified", "failed": "verification_failed", "unknown": "verification_unknown"}[verification["status"]]
            run["verification_provenance"] = "agent_reported_result_checks"
            return self._save_run(db, run)
