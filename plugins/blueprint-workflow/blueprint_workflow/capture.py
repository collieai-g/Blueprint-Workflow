"""Durable, task-scoped capture receipts and explicitly registered snapshots.

The inbox contains already scrubbed JSON. It can be acknowledged without waiting
for the analysis worker or the main SQLite write lock. Only the consumer appends
events; its retry is idempotent even after a crash between commit and receipt.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from typing import Any
import uuid

from . import privacy

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()
_TEXT_EXTENSIONS = {".txt", ".md", ".json", ".jsonl", ".csv", ".tsv", ".xml", ".html", ".css", ".js", ".mjs", ".ts", ".py", ".ps1", ".sh", ".yaml", ".yml", ".toml", ".log", ".svg"}
_PRIVATE_FILE = re.compile(r"^(?:\.env(?:\..*)?|id_(?:rsa|ed25519)(?:\.pub)?|(?:cookies?|credentials?|tokens?|auth|login)(?:\.[^.]+)?|(?:qr|qrcode|qr-code)(?:[-_].*)?\.(?:png|jpe?g|svg|webp))$", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _redact_full(value: str) -> str:
    """Use the shared redaction rules without its event-preview text limit."""
    value = privacy._MEDIA.sub(privacy.MEDIA_OMITTED, value)
    if privacy._BASE64.fullmatch(value):
        return privacy.MEDIA_OMITTED
    value = privacy._URL.sub(lambda match: privacy.clean_url(match.group(0)), value)
    value = privacy._BEARER.sub("Bearer " + privacy.REDACTED, value)
    value = privacy._TOKEN.sub(privacy.REDACTED, value)
    return privacy._ASSIGNMENT.sub(lambda match: match.group(1) + "=" + privacy.REDACTED, value)


def clean_capture(value: Any, *, depth: int = 0) -> Any:
    """Keep available text, never media payloads, before making bounded previews."""
    if depth > 24:
        return "[nested content omitted: capture depth limit]"
    if isinstance(value, dict):
        media_type = str(value.get("type", "")).lower()
        mime = str(value.get("mimeType", value.get("mime_type", ""))).lower()
        if media_type in {"image", "image_url", "input_image", "audio", "input_audio", "video"} or mime.startswith(("image/", "audio/", "video/")):
            return {"type": "omitted_media", "description": privacy.MEDIA_OMITTED}
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Capture keys must be strings")
            if privacy.sensitive_key(key):
                result[key] = privacy.REDACTED
            elif key.lower() in {"image_url", "image_data", "image_base64", "audio_url", "audio_data", "video_data", "base64", "blob", "bytes", "screenshot"}:
                result[key] = privacy.MEDIA_OMITTED
            else:
                result[key] = clean_capture(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [clean_capture(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_full(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError("Capture records must contain JSON values")


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def record_ingress_failure(data_dir: str | Path | None, reason: str) -> None:
    """Record a bounded, unattributed health marker without saving hook input."""
    from .paths import private_data_dir
    root = private_data_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / "capture-health"
    if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
        raise ValueError("Capture health path contains a link")
    marker = {"at": _now(), "reason": reason, "task_attribution": "unknown", "recoverable": False}
    _atomic(directory / (uuid.uuid4().hex + ".json"), _json(marker).encode("utf-8"))


class CaptureMixin:
    def init_capture(self, db) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS capture_sources (
          id TEXT NOT NULL,session_id TEXT NOT NULL,sha256 TEXT NOT NULL,
          relative_path TEXT NOT NULL,characters INTEGER NOT NULL,byte_count INTEGER NOT NULL,
          created_at TEXT NOT NULL,metadata TEXT NOT NULL,PRIMARY KEY(session_id,id)
        );
        CREATE TABLE IF NOT EXISTS capture_artifacts (
          id TEXT PRIMARY KEY,session_id TEXT NOT NULL,original_path TEXT NOT NULL,
          relative_path TEXT NOT NULL,source_hash TEXT NOT NULL,snapshot_hash TEXT NOT NULL,
          byte_count INTEGER NOT NULL,description TEXT NOT NULL,role TEXT NOT NULL,
          created_at TEXT NOT NULL,metadata TEXT NOT NULL
        );
        """
        for statement in schema.split(";"):
            if statement.strip():
                db.execute(statement)

    def _capture_path(self, relative: str | Path) -> Path:
        root = Path(self.data_dir).resolve()
        path = root / relative
        if not path.resolve().is_relative_to(root):
            raise ValueError("Capture path must remain in private data")
        for ancestor in (path, *path.parents):
            if ancestor == root.parent:
                break
            if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
                raise ValueError("Capture path contains a symbolic link or junction")
        return path

    def _task_root(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
            raise ValueError("Invalid capture task identifier")
        return self._capture_path(Path("tasks") / session_id)

    def capture_boundary(self, session_id: str):
        """Reentrant process/thread boundary shared by enqueue, drain and finish."""
        return self._capture_file_boundary(session_id, "capture.lock")

    @contextmanager
    def _capture_file_boundary(self, session_id: str, filename: str):
        root = self._task_root(session_id)
        root.mkdir(parents=True, exist_ok=True)
        path = self._capture_path(root / filename)
        key = str(path)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        with lock:
            held = getattr(_HELD, "locks", None)
            if held is None:
                held = _HELD.locks = {}
            if key in held:
                yield
                return
            with path.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if not handle.tell():
                    handle.write(b"0")
                    handle.flush()
                deadline = time.monotonic() + 5
                while True:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except (BlockingIOError, OSError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Capture boundary is busy; retry with the same event IDs")
                        time.sleep(0.02)
                held[key] = True
                try:
                    yield
                finally:
                    held.pop(key, None)
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _capture_accepting(self, session_id: str) -> None:
        # A WAL read does not wait for the main writer; do not inherit its 30s
        # write-lock timeout in the foreground hook's durable inbox operation.
        with closing(sqlite3.connect(self.db_path, timeout=0.2)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError("unknown session: " + session_id)
            state = row["capture_state"] if "capture_state" in row.keys() else row["status"]
            if state != "recording":
                raise ValueError("session is not accepting capture")

    def _source_save(self, session_id: str, text: str, *, metadata: dict | None = None, db=None) -> dict:
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        source_id = "source_" + digest
        relative = Path("tasks") / session_id / "sources" / digest / "source.txt"
        path = self._capture_path(relative)
        if not path.exists():
            _atomic(path, data)
            for index, offset in enumerate(range(0, len(text), 16_000)):
                chunk = self._capture_path(relative.parent / "chunks" / (f"{index:06d}.txt"))
                _atomic(chunk, text[offset:offset + 16_000].encode("utf-8"))
        values = (source_id, session_id, digest, str(relative), len(text), len(data), _now(), _json(metadata or {}))
        if db is not None:
            db.execute("INSERT OR IGNORE INTO capture_sources VALUES(?,?,?,?,?,?,?,?)", values)
        else:
            with self._transaction() as connection:
                connection.execute("INSERT OR IGNORE INTO capture_sources VALUES(?,?,?,?,?,?,?,?)", values)
        return {"source_id": source_id, "sha256": digest, "characters": len(text), "byte_count": len(data), "content_type": "application/json"}

    def source_excerpt(self, db, session_id: str, source_id: str, max_chars: int = 16_000, *, offset: int = 0) -> dict:
        return self._source_read(db, session_id, source_id, offset, max_chars)

    def _source_read(self, db, session_id: str, source_id: str, offset: int, limit: int) -> dict:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 32_000:
            raise ValueError("Invalid source range")
        row = db.execute("SELECT * FROM capture_sources WHERE session_id=? AND id=?", (session_id, source_id)).fetchone()
        if row is None:
            raise KeyError("unknown source for this task")
        path = self._capture_path(row["relative_path"])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("Source snapshot integrity check failed")
        text = data.decode("utf-8")
        return {"source_id": source_id, "sha256": row["sha256"], "text": text[offset:offset + limit], "offset": offset,
                "characters": len(text), "has_more": offset + limit < len(text), "next_offset": min(len(text), offset + limit)}

    def source_read(self, session_id: str, source_id: str, offset: int = 0, limit: int = 16_000) -> dict:
        with self._connection() as db:
            return self._source_read(db, session_id, source_id, offset, limit)

    def externalize_event(self, session_id: str, raw: dict, *, db=None) -> dict:
        """Preserve full scrubbed input and attach a bounded analysis preview."""
        clean = clean_capture(raw)
        encoded = _json(clean)
        def has_depth_loss(value):
            if isinstance(value, str):
                return "[nested content omitted" in value
            if isinstance(value, dict):
                return any(has_depth_loss(item) for item in value.values())
            if isinstance(value, list):
                return any(has_depth_loss(item) for item in value)
            return False
        depth_loss = has_depth_loss(clean)
        # Most small events retain their original shape and dedup fingerprint.
        needs_source = len(encoded.encode("utf-8")) > 48_000
        def is_large(value):
            if isinstance(value, str):
                return len(value) > 8_000
            if isinstance(value, dict):
                return len(value) > 80 or any(is_large(item) for item in value.values())
            if isinstance(value, list):
                return len(value) > 40 or any(is_large(item) for item in value)
            return False
        if not needs_source and not is_large(clean):
            if depth_loss:
                clean["capture_gaps"] = [{"reason": "depth_limit", "recoverable": False}]
            return clean
        source = self._source_save(session_id, encoded, metadata={"kind": "captured_event"}, db=db)
        budget = [24_000]
        def preview(value, depth=0):
            if depth > 12 or budget[0] <= 0:
                return "[preview omitted; see source_refs]"
            if isinstance(value, str):
                take = min(len(value), 4_000, budget[0])
                budget[0] -= take
                return value[:take] + (" [preview; see source_refs]" if take < len(value) else "")
            if isinstance(value, dict):
                result = {}
                for key, item in list(value.items())[:40]:
                    if budget[0] <= 0:
                        break
                    result[key] = preview(item, depth + 1)
                return result
            if isinstance(value, list):
                return [preview(item, depth + 1) for item in value[:20] if budget[0] > 0]
            return value
        # Preserve event identity/control fields even when the details are large.
        result = {key: clean[key] for key in ("kind", "actor", "client_event_id", "event_id", "corrects_event_id", "source") if key in clean}
        result["summary"] = preview(str(clean.get("summary", "Captured host event")))
        result["details"] = preview(clean.get("details", {}))
        result["source_refs"] = [source]
        result["capture_gaps"] = [{"reason": "bounded_preview", "recoverable": True, "source_id": source["source_id"]}]
        if depth_loss:
            result["capture_gaps"].append({"reason": "depth_limit", "recoverable": False})
        return result

    def capture_enqueue(self, session_id: str, events: list[dict]) -> dict:
        if not isinstance(events, list) or not 1 <= len(events) <= 1000:
            raise ValueError("capture events must contain 1 to 1000 objects")
        with self.capture_boundary(session_id):
            self._capture_accepting(session_id)
            receipts = []
            for raw in events:
                if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str) or not raw["kind"].strip() or not isinstance(raw.get("summary"), str) or not raw["summary"].strip():
                    raise ValueError("capture event requires kind and summary")
                event = clean_capture(raw)
                client_id = event.get("client_event_id", event.get("event_id")) or "capture_" + uuid.uuid4().hex
                if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 255:
                    raise ValueError("Invalid capture event identity")
                event["client_event_id"] = client_id
                fingerprint = hashlib.sha256(_json(event).encode("utf-8")).hexdigest()
                receipt_id = "capture_" + hashlib.sha256(client_id.encode("utf-8")).hexdigest()
                path = self._capture_path(Path("tasks") / session_id / "inbox" / (receipt_id + ".json"))
                archived = self._capture_path(Path("tasks") / session_id / "receipts" / (receipt_id + ".json"))
                existing = archived if archived.exists() else path
                if existing.exists():
                    receipt = json.loads(existing.read_text(encoding="utf-8"))
                    if receipt["fingerprint"] != fingerprint:
                        raise ValueError("duplicate capture event ID has different content")
                else:
                    receipt = {"receipt_id": receipt_id, "session_id": session_id, "client_event_id": client_id,
                               "fingerprint": fingerprint, "received_at": _now(), "status": "queued", "event": event}
                    _atomic(path, _json(receipt).encode("utf-8"))
                receipts.append({key: receipt[key] for key in ("receipt_id", "session_id", "client_event_id", "received_at", "status")})
            return {"session_id": session_id, "durable": True, "receipts": receipts, "queued_count": sum(item["status"] == "queued" for item in receipts)}

    def drain_capture(self, session_id: str | None = None) -> dict:
        if session_id is None:
            with self._connection() as db:
                session_ids = [row[0] for row in db.execute("SELECT id FROM sessions ORDER BY created_at")]
        else:
            session_ids = [session_id]
        drained, failures = 0, []
        for task_id in session_ids:
            # Never acquire capture.lock while holding drain.lock. Ordinary
            # consumers must not make enqueue wait for SQLite/model work;
            # finalization holds capture.lock outside this method, then waits
            # for the one consumer before closing the accepted event boundary.
            with self._capture_file_boundary(task_id, "drain.lock"):
                inbox = self._capture_path(Path("tasks") / task_id / "inbox")
                if not inbox.exists():
                    continue
                # File mtime is not a source ordering guarantee. Receipts retain
                # received_at; stable sorting preserves each accepted chronology.
                queued = [(path, json.loads(self._capture_path(path).read_text(encoding="utf-8"))) for path in inbox.glob("capture_*.json")]
                queued.sort(key=lambda pair: (pair[1]["received_at"], pair[1]["receipt_id"]))
                for path, receipt in queued:
                    archived = self._capture_path(Path("tasks") / task_id / "receipts" / (receipt["receipt_id"] + ".json"))
                    try:
                        if archived.exists():
                            acknowledged = json.loads(archived.read_text(encoding="utf-8"))
                            if acknowledged["fingerprint"] != receipt["fingerprint"]:
                                raise ValueError("Capture receipt archive integrity mismatch")
                            # A crash after durable acknowledgement but before
                            # removing the queue copy needs no database replay.
                            self._capture_path(path).unlink()
                            continue
                        if receipt["status"] == "drained":
                            # Migrate earlier v3 receipt files without retaining
                            # another full event copy in the live inbox forever.
                            acknowledged = {key: item for key, item in receipt.items() if key != "event"}
                            _atomic(archived, _json(acknowledged).encode("utf-8"))
                            self._capture_path(path).unlink()
                            continue
                        with self._transaction() as db:
                            event = self.externalize_event(task_id, receipt["event"], db=db)
                            saved = self._append(db, task_id, [event], allow_closed=True)
                        acknowledged = {key: item for key, item in receipt.items() if key not in {"event", "last_error"}}
                        acknowledged.update(status="drained", drained_at=_now(), event_ids=[item["id"] for item in saved])
                        _atomic(archived, _json(acknowledged).encode("utf-8"))
                        self._capture_path(path).unlink()
                        drained += 1
                    except Exception as exc:
                        receipt["last_error"] = privacy.redact_text(str(exc))[:1000]
                        _atomic(self._capture_path(path), _json(receipt).encode("utf-8"))
                        failures.append({"receipt_id": receipt["receipt_id"], "session_id": task_id, "error": receipt["last_error"]})
                        break  # Do not analyze later observations past a gap.
        return {"drained_count": drained, "failures": failures, "complete": not failures}

    def capture_status(self, session_id: str) -> dict:
        with self._connection() as db:
            session = self._session(db, session_id)
            sources = db.execute("SELECT COUNT(*),COALESCE(SUM(byte_count),0) FROM capture_sources WHERE session_id=?", (session_id,)).fetchone()
            artifacts = db.execute("SELECT COUNT(*),COALESCE(SUM(byte_count),0) FROM capture_artifacts WHERE session_id=?", (session_id,)).fetchone()
            legacy = db.execute('SELECT value FROM internal_metadata WHERE key=?', ('capture-v3-legacy:' + session_id,)).fetchone()
        inbox = self._capture_path(Path("tasks") / session_id / "inbox")
        archive = self._capture_path(Path("tasks") / session_id / "receipts")
        archived_names = {path.name for path in archive.glob("capture_*.json")} if archive.exists() else set()
        receipts = [json.loads(self._capture_path(path).read_text(encoding="utf-8")) for path in inbox.glob("capture_*.json") if path.name not in archived_names] if inbox.exists() else []
        queued = [item for item in receipts if item["status"] != "drained"]
        gaps_path = self._capture_path(Path("tasks") / session_id / "capture-gaps.jsonl")
        gaps = [json.loads(line) for line in gaps_path.read_text(encoding="utf-8").splitlines() if line.strip()] if gaps_path.exists() else []
        health = self._capture_path("capture-health")
        return {"session_id": session_id, "capture_state": session["capture_state"] if "capture_state" in session.keys() else session["status"],
                "legacy_capture": json.loads(legacy[0]) if legacy else None,
                "received_count": len(receipts) + len(archived_names), "queued_count": len(queued), "drained_count": len(receipts) - len(queued) + len(archived_names),
                "oldest_queued_at": min((item["received_at"] for item in queued), default=None),
                "failed_receipt_count": sum(bool(item.get("last_error")) for item in queued),
                "capture_gap_count": len(gaps), "latest_capture_gap": gaps[-1] if gaps else None,
                "unattributed_ingress_gap_count": sum(1 for _ in health.glob("*.json")) if health.exists() else 0,
                "source_count": sources[0], "source_bytes": sources[1], "artifact_count": artifacts[0], "artifact_bytes": artifacts[1]}

    def record_capture_gap(self, session_id: str, reason: str) -> dict:
        if reason not in {"enqueue_failed", "host_input_limit", "host_input_invalid", "depth_limit", "upstream_truncated"}:
            raise ValueError("Unsupported capture gap reason")
        record = {"at": _now(), "reason": reason, "recoverable": False}
        with self.capture_boundary(session_id):
            path = self._capture_path(Path("tasks") / session_id / "capture-gaps.jsonl")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def artifact_register(self, session_id: str, path: str, description: str, role: str = "output") -> dict:
        """Snapshot only a caller-selected file, never discover or execute files."""
        if not isinstance(description, str) or not description.strip() or role not in {"output", "reference", "input", "supporting"}:
            raise ValueError("Artifact requires a description and a supported role")
        supplied = Path(path).expanduser().absolute()
        for ancestor in (supplied, *supplied.parents):
            if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
                raise ValueError("Artifact path contains a symbolic link or junction")
        if (not supplied.is_file() or _PRIVATE_FILE.fullmatch(supplied.name)
                or supplied.suffix.lower() in {".pem", ".pfx", ".p12", ".key", ".kdbx", ".psafe3"}
                or supplied.name.lower() in {"login data", "login data for account", "local state", "web data"}):
            raise ValueError("Artifact must be a selected regular file, not a credential or login file")
        with self.capture_boundary(session_id):
            self._capture_accepting(session_id)
            before = supplied.stat()
            data = supplied.read_bytes()
            after = supplied.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != before.st_size:
                raise ValueError("Artifact changed during snapshot; select a saved version")
            source_hash = hashlib.sha256(data).hexdigest()
            text_snapshot = False
            if supplied.suffix.lower() in _TEXT_EXTENSIONS:
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    pass
                else:
                    if supplied.suffix.lower() in {".json", ".jsonl"}:
                        try:
                            if supplied.suffix.lower() == ".jsonl":
                                text = "\n".join(_json(clean_capture(json.loads(line))) for line in text.splitlines() if line.strip())
                            else:
                                text = _json(clean_capture(json.loads(text)))
                        except (ValueError, RecursionError):
                            text = _redact_full(text)
                    else:
                        text = _redact_full(text)
                    data = text.encode("utf-8")
                    text_snapshot = True
            snapshot_hash = hashlib.sha256(data).hexdigest()
            artifact_id = "artifact_" + uuid.uuid4().hex
            relative = Path("tasks") / session_id / "artifacts" / artifact_id / supplied.name
            snapshot = self._capture_path(relative)
            _atomic(snapshot, data)
            metadata = {"text_redacted": text_snapshot, "model_content_allowed": text_snapshot, "explicitly_selected": True,
                        "source_byte_count": before.st_size, "source_modified_ns": before.st_mtime_ns}
            with self._transaction() as db:
                db.execute("INSERT INTO capture_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?)", (artifact_id, session_id, str(supplied), str(relative), source_hash, snapshot_hash,
                           len(data), privacy.redact_text(description), role, _now(), _json(metadata)))
                self._append(db, session_id, [{
                    "actor": "host", "kind": "artifact_registered",
                    "summary": "Saved a selected artifact snapshot: " + privacy.redact_text(description),
                    "details": {"artifact_id": artifact_id, "role": role, "snapshot_path": str(snapshot),
                                "source_hash": source_hash, "snapshot_hash": snapshot_hash,
                                "byte_count": len(data), "model_content_allowed": text_snapshot},
                }])
            return {"artifact_id": artifact_id, "session_id": session_id, "snapshot_path": str(snapshot), "source_hash": source_hash,
                    "snapshot_hash": snapshot_hash, "byte_count": len(data), "role": role, **metadata}
