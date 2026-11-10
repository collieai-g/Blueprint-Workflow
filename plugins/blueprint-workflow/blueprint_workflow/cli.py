"""Thin headless entrypoints. Protocol stdout is reserved for JSON/MCP."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

from . import __version__
from .paths import default_data_dir, private_data_dir
from .privacy import sanitize


def _json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="blueprint-workflow")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("mcp", help="Serve MCP over stdio")
    hook = commands.add_parser("hook", help="Consume a native host lifecycle hook on stdin")
    hook.add_argument("--host", choices=("codex", "claude-code", "cursor", "gemini-cli"))
    worker = commands.add_parser("worker", help="Run the private background analysis loop")
    worker.add_argument("--data-dir", dest="worker_data_dir", default=None)
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--idle-seconds", type=float, default=120.0)
    call = commands.add_parser("call", help="Call the same tool service using JSON")
    call.add_argument("tool")
    call.add_argument("--file", default="-", help="JSON arguments file, or - for stdin")
    commands.add_parser("schema", help="Print the analysis result JSON Schema")
    doctor = commands.add_parser("doctor", help="Inspect local prerequisites without running a model")
    doctor.add_argument("--host", choices=("codex", "claude-code", "cursor", "gemini-cli"))
    doctor.add_argument("--config-root")
    doctor.add_argument("--provider")
    doctor.add_argument("--model")
    doctor.add_argument("--runtime-python")
    for name in ("install", "install-plan", "uninstall"):
        installer = commands.add_parser(name, help="Manage one host's local adapter; preserve private data")
        installer.add_argument("--host", required=True, choices=("codex", "claude-code", "cursor", "gemini-cli"))
        installer.add_argument("--config-root")
        if name != "uninstall":
            installer.add_argument("--provider")
            installer.add_argument("--model")
            installer.add_argument("--runtime-python")
        if name == "install":
            installer.add_argument("--stage-only", action="store_true")
            installer.add_argument("--dry-run", action="store_true", help="Print the installation plan without writing")
            installer.add_argument("--skip-dependencies", action="store_true", help="Use an already prepared MCP runtime")
    args = parser.parse_args(argv)
    try:
        if args.command == "mcp":
            from .mcp_server import serve
            serve(args.data_dir)
        elif args.command == "hook":
            from .hooks import main as hook_main
            return hook_main(data_dir=args.data_dir, host=args.host)
        elif args.command == "worker":
            from .worker import main as worker_main
            forwarded = ["--data-dir", str(args.worker_data_dir or args.data_dir or default_data_dir())]
            if args.once:
                forwarded.append("--once")
            forwarded.extend(["--idle-seconds", str(args.idle_seconds)])
            return worker_main(forwarded)
        elif args.command == "call":
            from .service import WorkflowService
            raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8-sig")
            arguments = json.loads(raw or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
            _json(WorkflowService(args.data_dir).call(args.tool, arguments))
        elif args.command == "schema":
            from .model import analysis_json_schema
            _json(analysis_json_schema())
        elif args.command == "doctor":
            if args.host:
                from .installation import diagnose_host
                _json(diagnose_host(args.host, data_dir=args.data_dir, config_root=args.config_root,
                                    provider=args.provider, model=args.model, runtime_python=args.runtime_python))
                return 0
            try:
                mcp_version = importlib.metadata.version("mcp")
            except importlib.metadata.PackageNotFoundError:
                mcp_version = None
            from .providers import PROVIDER_NAMES, describe_provider
            _json({"version": __version__, "python": sys.version.split()[0], "python_executable": sys.executable,
                   "codex": shutil.which("codex"), "mcp_sdk": mcp_version,
                   "providers": [describe_provider(name) for name in PROVIDER_NAMES],
                   "data_dir": str(private_data_dir(args.data_dir)), "screen_recording": False,
                   "model_called": False, "analyzer_process": os.environ.get("BLUEPRINT_WORKFLOW_ANALYZER") == "1"})
        elif args.command in {"install", "install-plan", "uninstall"}:
            from .installation import install_host, plan_install, uninstall_host
            options = {"data_dir": args.data_dir, "config_root": args.config_root}
            if args.command == "uninstall":
                _json(uninstall_host(args.host, **options))
            else:
                options.update(provider=args.provider, model=args.model, runtime_python=args.runtime_python)
                if args.command == "install" and not args.dry_run:
                    _json(install_host(args.host, **options, stage_only=args.stage_only, skip_dependencies=args.skip_dependencies))
                else:
                    _json(plan_install(args.host, **options))
        return 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, sqlite3.Error) as exc:
        _json({"error": type(exc).__name__, "message": sanitize(str(exc))})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
