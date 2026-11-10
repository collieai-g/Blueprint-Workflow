"""Independent local queue consumer for incremental workflow analysis."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analyzer import safe_error


class WorkerLock:
    """An OS-released lock; a crash cannot leave an eternal singleton lease."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> bool:
        if self.handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "WorkerLock":
        if not self.acquire():
            raise RuntimeError("A worker already owns this data directory")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _log(data_dir: Path, event: str, **fields: Any) -> None:
    record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    if "error" in record:
        record["error"] = safe_error(record["error"])
    with (data_dir / "worker.log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def run_once(
    store: Any,
    analyzer: Any | None = None,
    *,
    worker_id: str | None = None,
    max_attempts: int = 5,
    heartbeat_interval: float = 20,
    provider_factory: Any | None = None,
    provider_cache: dict[tuple[str, str | None], Any] | None = None,
    data_dir: Path | str | None = None,
    analysis_timeout: float = 240,
) -> bool:
    """Consume at most one durable job, returning whether a job was claimed.

    Errors are committed as job failures, never as a successful cursor advance.
    A background heartbeat preserves ownership during a slow model response.
    """
    worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex}"
    lease_seconds = max(90, int(getattr(analyzer, "timeout", analysis_timeout)) + 60)
    job = store.claim_job(worker_id, lease_seconds=lease_seconds)
    if job is None:
        return False
    provider = job.get("analysis_provider") or "codex"
    model = job.get("analysis_model")
    heartbeat_stop = threading.Event()
    lease_lost = threading.Event()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(heartbeat_interval):
            try:
                if not store.heartbeat_job(job["job_id"], worker_id, lease_seconds=lease_seconds):
                    lease_lost.set()
                    return
            except Exception:
                # A transient database failure must not permit a stale commit.
                lease_lost.set()
                return

    thread = None
    analysis = None
    if hasattr(store, "heartbeat_job"):
        thread = threading.Thread(target=heartbeat, name="workflow-job-heartbeat", daemon=True)
        thread.start()
    try:
        selected = analyzer
        if selected is None:
            if provider_factory is None:
                from .providers import create_provider
                provider_factory = create_provider
            profile = (provider, model)
            cache = provider_cache if provider_cache is not None else {}
            if profile not in cache:
                root = data_dir if data_dir is not None else getattr(store, "data_dir", None)
                if root is None:
                    raise ValueError("provider routing requires a private data directory")
                cache[profile] = provider_factory(root, provider=provider, model=model, timeout=analysis_timeout)
            selected = cache[profile]
        analysis = selected.analyze(job)
        if lease_lost.is_set():
            raise RuntimeError("Analysis lease was lost; stale result discarded")
        store.commit_job(job["job_id"], worker_id, analysis)
        data_dir = getattr(store, "data_dir", None)
        if data_dir:
            _log(Path(data_dir), "analysis_committed", session_id=job["session_id"], through_seq=job["through_seq"], base_revision=job["base_revision"], analysis_provider=provider, analysis_model=model)
    except Exception as exc:
        stale_result = 'analysis snapshot is stale' in str(exc)
        if (not stale_result and isinstance(analysis, dict) and getattr(store, 'data_dir', None)
                and callable(getattr(store, '_capture_path', None))):
            try:
                from .capture import _atomic, clean_capture
                target = store._capture_path(Path('tasks') / job['session_id'] / 'analysis-failures' / (job['job_id'] + '.json'))
                _atomic(target, json.dumps({'error': safe_error(exc), 'response': clean_capture(analysis)}, ensure_ascii=False).encode('utf-8'))
            except (OSError, ValueError):
                pass
        attempts = int(job.get("retry_attempts", 0)) + 1
        delay = None if attempts >= max_attempts else min(60, 2 ** min(attempts, 6))
        try:
            store.fail_job(job["job_id"], worker_id, safe_error(exc), retry_delay=delay)
        except Exception:
            # Expired/replaced leases deliberately reject both success and
            # failure writes from the former owner. The durable job will retry.
            pass
        data_dir = getattr(store, "data_dir", None)
        if data_dir:
            _log(Path(data_dir), "analysis_superseded" if stale_result else "analysis_failed", session_id=job["session_id"], error=exc, retry_delay=delay, analysis_provider=provider, analysis_model=model)
    finally:
        heartbeat_stop.set()
        if thread:
            thread.join(timeout=1)
    return True


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _batch_ready(store: Any, first_seen: dict[str, float], debounce: float, max_batch_wait: float) -> bool:
    if not hasattr(store, "pending_sessions"):
        return True
    now = time.time()
    pending = store.pending_sessions()
    live = set()
    for session in pending:
        session_id = session.get("session_id", session.get("id", ""))
        live.add(session_id)
        first_seen.setdefault(session_id, now)
        if _epoch(session.get("next_attempt_at")) > now:
            continue
        if session.get("status") == "finalizing":
            return True
        age = now - _epoch(session.get("last_event_at"))
        if age >= debounce or now - first_seen[session_id] >= max_batch_wait:
            return True
    for session_id in set(first_seen) - live:
        first_seen.pop(session_id, None)
    return False


def run_worker(
    data_dir: Path | str,
    *,
    analyzer: Any | None = None,
    once: bool = False,
    poll_interval: float = 0.5,
    idle_timeout: float = 120,
    debounce: float = 2,
    max_batch_wait: float = 10,
    stop_event: threading.Event | None = None,
    max_jobs: int | None = None,
    provider_factory: Any | None = None,
    analysis_timeout: float = 240,
) -> dict[str, Any]:
    """Run the singleton queue consumer; no model runs inside a recorder call."""
    from .store import Store

    if analysis_timeout <= 0:
        raise ValueError("analysis_timeout must be positive")
    store = Store(data_dir)
    data_path = Path(store.data_dir)
    lock = WorkerLock(data_path / "worker.lock")
    if not lock.acquire():
        return {"status": "already_running", **worker_status(data_path)}
    worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex}"
    provider_cache: dict[tuple[str, str | None], Any] = {}
    stop_event = stop_event or threading.Event()
    stop_path = data_path / "worker.stop"
    stop_path.unlink(missing_ok=True)
    metadata = {"pid": os.getpid(), "worker_id": worker_id, "started_at": time.time(), "status": "running"}
    metadata_path = data_path / "worker.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    previous_marker = os.environ.get("BLUEPRINT_WORKFLOW_WORKER")
    os.environ["BLUEPRINT_WORKFLOW_WORKER"] = "1"
    _log(data_path, "worker_started", pid=os.getpid(), provider_routing="per_session")
    jobs = 0
    last_work = time.monotonic()
    first_seen: dict[str, float] = {}
    shutdown_lock: WorkerLock | None = None
    idle_shutdown = False
    research_stop = threading.Event()
    research_thread = None
    if not once and analyzer is None and os.environ.get('BLUEPRINT_WORKFLOW_RESEARCH', '1') != '0':
        def research_loop() -> None:
            from .research import run_research_once
            while not research_stop.wait(1):
                try:
                    # Foreground learning has priority over extra investigation.
                    if any(row['pending_event_count'] > 100 for row in store.pending_sessions()):
                        continue
                    run_research_once(store, data_path, timeout=analysis_timeout)
                except Exception as exc:
                    _log(data_path, 'research_failed', error=exc)
        research_thread = threading.Thread(target=research_loop, name='workflow-read-only-research', daemon=True)
        research_thread.start()
    try:
        while not stop_event.is_set() and not stop_path.exists():
            store.drain_capture()
            if once or _batch_ready(store, first_seen, debounce, max_batch_wait):
                if run_once(store, analyzer, worker_id=worker_id, provider_factory=provider_factory,
                            provider_cache=provider_cache, data_dir=data_path, analysis_timeout=analysis_timeout):
                    jobs += 1
                    last_work = time.monotonic()
                    first_seen.clear()
                    if once or (max_jobs is not None and jobs >= max_jobs):
                        break
                    continue
            if once:
                break
            if time.monotonic() - last_work >= idle_timeout:
                # Serialize shutdown with ensure_worker: an event submitted at
                # the idle boundary must either be seen here or start a new
                # worker after this lock is released.
                shutdown_lock = WorkerLock(data_path / "worker-launch.lock")
                if shutdown_lock.acquire():
                    if _batch_ready(store, first_seen, debounce=0, max_batch_wait=0):
                        shutdown_lock.release()
                        shutdown_lock = None
                        last_work = time.monotonic()
                        continue
                    idle_shutdown = True
                    break
                shutdown_lock = None
            stop_event.wait(poll_interval)
        return {"status": "stopped", "jobs_processed": jobs, "worker_id": worker_id}
    finally:
        research_stop.set()
        if research_thread:
            # Keep the singleton lock until the bounded native process has
            # terminated; an installer must never observe a false stopped state.
            research_thread.join()
        metadata["status"] = "stopped"
        metadata["stopped_at"] = time.time()
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        stop_path.unlink(missing_ok=True)
        _log(data_path, "worker_stopped", pid=os.getpid(), jobs_processed=jobs)
        lock.release()
        if shutdown_lock:
            shutdown_lock.release()
        if previous_marker is None:
            os.environ.pop("BLUEPRINT_WORKFLOW_WORKER", None)
        else:
            os.environ["BLUEPRINT_WORKFLOW_WORKER"] = previous_marker
        # An append may have raced the final idle check. Its caller either sees
        # the released singleton lock and starts a replacement, or the newly
        # pending durable work is detected here after that lock was released.
        if idle_shutdown and hasattr(store, "pending_sessions") and store.pending_sessions():
            ensure_worker(data_path)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def worker_status(data_dir: Path | str) -> dict[str, Any]:
    data_path = Path(data_dir).expanduser().resolve()
    if not data_path.exists():
        return {"running": False, "status": "stopped"}
    metadata: dict[str, Any] = {}
    try:
        metadata = json.loads((data_path / "worker.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    lock = WorkerLock(data_path / "worker.lock")
    running = not lock.acquire()
    lock.release()
    return {**metadata, "running": running, "status": "running" if running else "stopped"}


def ensure_worker(data_dir: Path | str) -> dict[str, Any]:
    """Start a detached, hidden worker and return without waiting for analysis."""
    data_path = Path(data_dir).expanduser().resolve()
    data_path.mkdir(parents=True, exist_ok=True)
    if os.environ.get("BLUEPRINT_WORKFLOW_AUTOSTART") == "0":
        return {"running": False, "status": "autostart_disabled"}
    if os.environ.get("BLUEPRINT_WORKFLOW_ANALYZER") == "1":
        return {"running": False, "status": "analyzer_excluded"}
    launch_lock = WorkerLock(data_path / "worker-launch.lock")
    if not launch_lock.acquire():
        return {"running": False, "status": "starting"}
    try:
        existing = worker_status(data_path)
        if existing["running"]:
            return existing
        try:
            pending = json.loads((data_path / "worker-starting.json").read_text(encoding="utf-8"))
            already_stopped = pending.get("pid") == existing.get("pid") and float(existing.get("stopped_at", 0)) >= float(pending.get("started_at", 0))
            if not already_stopped and time.time() - float(pending.get("started_at", 0)) < 30 and _pid_exists(int(pending.get("pid", 0))):
                return {**pending, "running": False, "status": "starting"}
        except (OSError, ValueError, TypeError):
            pass
        package_root = Path(__file__).resolve().parent.parent
        environment = dict(os.environ)
        # This singleton serves every host. The first host to wake it must not
        # supply defaults for a later host's persisted analysis profile.
        for name in ("BLUEPRINT_WORKFLOW_HOST", "BLUEPRINT_WORKFLOW_PROVIDER", "BLUEPRINT_WORKFLOW_MODEL"):
            environment.pop(name, None)
        environment["BLUEPRINT_WORKFLOW_WORKER"] = "1"
        environment["PYTHONPATH"] = str(package_root) + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
        command = [sys.executable, "-m", "blueprint_workflow", "worker", "--data-dir", str(data_path)]
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            options["start_new_session"] = True
        with (data_path / "worker-process.log").open("ab") as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=package_root, env=environment, close_fds=True, **options)
        metadata = {"pid": process.pid, "started_at": time.time()}
        (data_path / "worker-starting.json").write_text(json.dumps(metadata), encoding="utf-8")
        return {**metadata, "running": False, "status": "starting"}
    finally:
        launch_lock.release()


def stop_worker(data_dir: Path | str) -> dict[str, Any]:
    """Request a graceful stop after the current model request finishes."""
    data_path = Path(data_dir).expanduser().resolve()
    status = worker_status(data_path)
    if not status["running"]:
        return status
    (data_path / "worker.stop").write_text(str(time.time()), encoding="ascii")
    return {**status, "status": "stop_requested"}


def main(argv: list[str] | None = None) -> int:
    """Internal worker entrypoint, also useful for a foreground diagnosis."""
    parser = argparse.ArgumentParser(description="Run the local workflow analysis worker")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-seconds", type=float, default=120)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--debounce-seconds", type=float, default=2)
    parser.add_argument("--max-batch-wait", type=float, default=10)
    parser.add_argument("--analysis-timeout", type=float, default=240)
    options = parser.parse_args(argv)
    if options.idle_seconds < 0 or options.poll_seconds <= 0 or options.debounce_seconds < 0 or options.max_batch_wait < 0:
        parser.error("Worker time settings must be positive (idle/debounce may be zero)")
    try:
        result = run_worker(
            options.data_dir,
            analysis_timeout=options.analysis_timeout,
            once=options.once,
            idle_timeout=options.idle_seconds,
            poll_interval=options.poll_seconds,
            debounce=options.debounce_seconds,
            max_batch_wait=options.max_batch_wait,
        )
    except Exception as exc:
        print(json.dumps({"error": safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
