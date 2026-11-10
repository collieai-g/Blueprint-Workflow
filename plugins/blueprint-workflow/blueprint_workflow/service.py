"""Host-neutral tool service, shared by MCP and the command-line adapter."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .paths import private_data_dir
from .store import Store
from .runtime import analysis_selection, current_host, scope_host_id, validate_model


class WorkflowService:
    def __init__(self, data_dir: str | Path | None = None, *, autostart: bool | None = None):
        self.data_dir = private_data_dir(data_dir)
        self.store = Store(self.data_dir)
        self.autostart = os.environ.get("BLUEPRINT_WORKFLOW_AUTOSTART", "1") != "0" if autostart is None else autostart

    def _wake(self, result: dict[str, Any] | None = None) -> dict[str, Any]:
        result = dict(result or {})
        if result.get("status") in {"paused", "blocked", "analysis_failed", "ready_to_test"}:
            return result
        if self.autostart:
            try:
                from .worker import ensure_worker
                result["background_worker"] = ensure_worker(self.data_dir)
            except Exception as exc:
                from .privacy import sanitize
                result["background_warning"] = str(sanitize(str(exc)))
        return result

    def session_start(self, title: str, goal: str, host_session_id: str | None = None,
                      analysis_provider: str | None = None, analysis_model: str | None = None) -> dict[str, Any]:
        provider, model = analysis_selection(analysis_provider, analysis_model)
        scoped = scope_host_id(host_session_id)
        if scoped and current_host() == "codex" and scoped.startswith("codex:"):
            legacy = self.store.find_session_by_host(scoped[len("codex:"):])
            if legacy and legacy.get("status") not in {"ready_to_test"}:
                raise ValueError("This Codex chat already has a legacy workflow task; resume session_id=" + legacy["session_id"])
        result = self.store.start_session(title, goal, host_session_id=scoped,
                                          analysis_provider=provider, analysis_model=model)
        return self._wake(result)

    def analysis_providers(self) -> dict[str, Any]:
        from .providers import PROVIDER_NAMES, describe_provider
        provider, model = analysis_selection()
        return {"host": current_host(), "selected_provider": provider, "selected_model": model,
                "providers": [describe_provider(name) for name in PROVIDER_NAMES], "model_called": False}

    def analysis_configure(self, session_id: str, analysis_provider: str, analysis_model: str | None = None) -> dict[str, Any]:
        from .providers import canonical_provider
        result = self.store.set_analysis_provider(session_id, canonical_provider(analysis_provider), validate_model(analysis_model))
        return self._wake(result)

    def event_append(self, session_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        result = self.store.append_events(session_id, events)
        return self._wake(result)

    def session_status(self, session_id: str, include_draft: bool = False) -> dict[str, Any]:
        result = self.store.status(session_id)
        if not include_draft:
            result = dict(result)
            draft = result.pop("draft", None)
            if draft:
                result["draft_summary"] = {key: draft.get(key) for key in ("title", "summary", "questions", "proposals") if key in draft}
                result["draft_step_count"] = len(draft.get("steps", []))
        return self._wake(result)

    def session_list(self) -> dict[str, Any]:
        keys = ("session_id", "title", "goal", "status", "host_session_id", "analysis_provider", "analysis_model", "latest_seq", "processed_seq", "draft_revision", "workflow_id", "workflow_version", "updated_at")
        return {"sessions": [{key: row[key] for key in keys if key in row} for row in self.store.list_sessions()]}

    def session_events(self, session_id: str, after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        return {"session_id": session_id, "events": self.store.get_events(session_id, after_seq=after_seq, limit=limit)}

    def session_pause(self, session_id: str) -> dict[str, Any]:
        return self.store.pause_session(session_id)

    def session_resume(self, session_id: str) -> dict[str, Any]:
        result = self.store.resume_session(session_id)
        return self._wake(result)

    def analysis_retry(self, session_id: str) -> dict[str, Any]:
        result = self.store.retry_analysis(session_id)
        return self._wake(result)

    def session_finish(self, session_id: str, final_summary: str, artifacts: list[dict[str, Any]] | None = None,
                       draft_review: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.store.finish_session(session_id, final_summary, artifacts=artifacts or [], draft_review=draft_review)
        return self._wake(result)

    def proposal_decide(self, session_id: str, proposal_id: str, decision: str, user_instruction: str,
                        content_revision: int | None = None, content_hash: str | None = None,
                        user_event_id: str | None = None) -> dict[str, Any]:
        return self._wake(self.store.decide_proposal(session_id, proposal_id, decision, user_instruction,
                          content_revision=content_revision, content_hash=content_hash, user_event_id=user_event_id))

    def session_updates(self, session_id: str, after_cursor: int = 0, limit: int = 2) -> dict[str, Any]:
        return self.store.session_updates(session_id, after_cursor=after_cursor, limit=limit)

    def updates_ack(self, session_id: str, update_ids: list[str], presentation_reference: str) -> dict[str, Any]:
        return self.store.updates_ack(session_id, update_ids, presentation_reference)

    def artifact_register(self, session_id: str, path: str, description: str, role: str = 'output') -> dict[str, Any]:
        return self._wake(self.store.artifact_register(session_id, path, description, role=role))

    def source_read(self, session_id: str, source_id: str, offset: int = 0, limit: int = 16000) -> dict[str, Any]:
        return self.store.source_read(session_id, source_id, offset=offset, limit=limit)

    def history_search(self, session_id: str, query: str = '', event_ids: list[str] | None = None,
                       after_seq: int = 0, through_seq: int | None = None, limit: int = 8) -> dict[str, Any]:
        return self.store.retrieve_history(session_id, query=query, event_ids=event_ids,
                                          after_seq=after_seq, through_seq=through_seq, limit=limit)

    def research_next(self, session_id: str, worker_id: str = 'foreground', lease_seconds: float = 300) -> dict[str, Any]:
        return self.store.research_next(session_id, worker_id=worker_id, lease_seconds=lease_seconds)

    def research_report(self, session_id: str, research_id: str, claim_token: str, result: dict[str, Any],
                        worker_id: str = 'foreground') -> dict[str, Any]:
        return self._wake(self.store.research_report(session_id, research_id, result, worker_id=worker_id, claim_token=claim_token))

    def research_capabilities(self, session_id: str) -> dict[str, Any]:
        from .research import research_capabilities
        state = self.store.status(session_id)
        return research_capabilities(self.data_dir, state['analysis_provider'], state['analysis_model'])

    def workflow_read(self, workflow_id: str, version: int | None = None) -> dict[str, Any]:
        return self.store.get_workflow(workflow_id, version=version)

    def workflow_revise(self, workflow_id: str, goal: str | None = None, host_session_id: str | None = None,
                        analysis_provider: str | None = None, analysis_model: str | None = None) -> dict[str, Any]:
        profile = {}
        if analysis_provider is not None:
            from .providers import canonical_provider
            profile["analysis_provider"] = canonical_provider(analysis_provider)
        if analysis_model is not None:
            profile["analysis_model"] = validate_model(analysis_model)
        result = self.store.revise_session(workflow_id, goal=goal, host_session_id=scope_host_id(host_session_id), **profile)
        return self._wake(result)

    def workflow_export(self, workflow_id: str, version: int | None = None, format: str = "markdown") -> dict[str, Any]:
        return {"workflow_id": workflow_id, "format": format, "content": self.store.workflow_export(workflow_id, version=version, format=format)}

    def workflow_materialize(self, workflow_id: str, version: int | None = None) -> dict[str, Any]:
        """Write a portable bundle inside the private data domain, never execute it."""
        import hashlib
        import json
        import tempfile
        record = self.store.get_workflow(workflow_id, version=version)
        root = self.data_dir / "exports" / record["workflow_id"] / ("v" + str(record["version"]))
        def check_path(path: Path) -> None:
            if not path.resolve().is_relative_to(self.data_dir.resolve()):
                raise ValueError("Export target resolves outside the private data directory")
            for ancestor in (path, *path.parents):
                if ancestor == self.data_dir.parent:
                    break
                if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
                    raise ValueError("Export path must not contain symbolic links or junctions")

        check_path(root)
        for parent in [root, *root.parents]:
            if parent == self.data_dir.parent:
                break
            if parent.is_symlink():
                raise ValueError("Export path must not contain symbolic links")
        root.mkdir(parents=True, exist_ok=True)
        payloads = {
            root / "workflow.json": json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False),
            root / "workflow.md": self.store.workflow_export(workflow_id, version=record["version"], format="markdown"),
        }
        for helper in record["workflow"].get("helpers", []):
            path = root / "helpers" / helper["name"]
            check_path(path)
            if path.parent.is_symlink() or path.is_symlink():
                raise ValueError("Helper export path must not be a symbolic link")
            payloads[path] = helper["content"]
        with self.store._connection() as db:
            artifacts = [dict(row) for row in db.execute('SELECT * FROM capture_artifacts WHERE session_id=? ORDER BY created_at,id', (record['session_id'],))]
        artifact_index = []
        for artifact in artifacts:
            source = self.store._capture_path(artifact['relative_path'])
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != artifact['snapshot_hash']:
                raise ValueError('Registered artifact snapshot integrity check failed')
            relative = Path('artifacts') / artifact['id'] / source.name
            target = root / relative
            check_path(target)
            payloads[target] = content
            artifact_index.append({'artifact_id': artifact['id'], 'path': relative.as_posix(),
                'description': artifact['description'], 'role': artifact['role'],
                'snapshot_hash': artifact['snapshot_hash'], 'source_hash': artifact['source_hash'],
                'metadata': json.loads(artifact['metadata'])})
        if artifact_index:
            payloads[root / 'artifacts.json'] = json.dumps({'artifacts': artifact_index,
                'includes_registered_task_artifacts': True, 'executed': False}, ensure_ascii=False, indent=2)
        files = []
        for path, content in payloads.items():
            check_path(path)
            if path.is_symlink():
                raise ValueError("Export file must not be a symbolic link")
            path.parent.mkdir(parents=True, exist_ok=True)
            options = {'mode': 'wb'} if isinstance(content, bytes) else {'mode': 'w', 'encoding': 'utf-8'}
            with tempfile.NamedTemporaryFile(**options, dir=path.parent, delete=False, suffix=".tmp") as handle:
                handle.write(content)
                temporary = Path(handle.name)
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            files.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        return {"workflow_id": record["workflow_id"], "version": record["version"], "directory": str(root),
                "files": files, "executed": False, "test_status": record["test_status"]}

    def workflow_run_start(self, workflow_id: str, inputs: dict[str, Any], version: int | None = None) -> dict[str, Any]:
        return self.store.run_start(workflow_id, inputs, version=version)

    def run_next(self, run_id: str) -> dict[str, Any]:
        return self.store.run_next(run_id)

    def run_report(self, run_id: str, step_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return self.store.run_report(run_id, step_id, result)

    def run_status(self, run_id: str) -> dict[str, Any]:
        return self.store.run_status(run_id)

    def run_retry(self, run_id: str, step_id: str, observation: str = "Explicit retry requested; inspect the current result before acting again.") -> dict[str, Any]:
        return self.store.run_retry(run_id, step_id, observation=observation)

    def run_verify(self, run_id: str, verification: dict[str, Any]) -> dict[str, Any]:
        return self.store.run_verify(run_id, verification)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name.startswith("_") or name not in TOOL_NAMES:
            raise ValueError(f"Unknown tool: {name}")
        return getattr(self, name)(**arguments)


TOOL_NAMES = (
    "session_start", "event_append", "session_status", "session_list", "session_events",
    "session_pause", "session_resume", "analysis_retry", "analysis_providers", "analysis_configure", "session_finish", "proposal_decide",
    "workflow_read", "workflow_revise", "workflow_export", "workflow_materialize", "workflow_run_start",
    "run_next", "run_report", "run_status", "run_retry", "run_verify",
    "session_updates", "updates_ack", "artifact_register", "source_read", "history_search",
    "research_next", "research_report", "research_capabilities",
)
