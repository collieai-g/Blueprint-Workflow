"""Local host adapters over one private runtime and one versioned core snapshot.

Planning and diagnosis never register hosts or call a model. Installation writes
only managed packages/catalogs and uses each host's native registration command.
It does not inspect, copy, or edit authentication stores.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from .paths import default_data_dir, private_data_dir
from .privacy import sanitize

HOSTS = ("codex", "claude-code", "cursor", "gemini-cli")
PLUGIN_NAME = "blueprint-workflow"
MARKETPLACE_NAME = "blueprint-workflow-local"
MCP_VERSION = "2.3.0"
SOURCE_ROOT = Path(__file__).resolve().parent.parent
_ROOTS = {"plugin.json", "mcp.json", ".codex-plugin", "hooks", "blueprint_workflow", "skills", "scripts", "adapters", "assets", "docs", "pyproject.toml", "README.md", "LICENSE", "NOTICE"}
_EXCLUDED = {"__pycache__", ".venv", ".git", "tests", ".pytest_cache", "node_modules", "local-settings.json"}
_COMMANDS = {"codex": "codex", "claude-code": "claude", "cursor": "cursor", "gemini-cli": "gemini"}


def _host(value: str) -> str:
    aliases = {"claude": "claude-code", "gemini": "gemini-cli"}
    value = aliases.get(value, value)
    if value not in HOSTS:
        raise ValueError("host must be one of: " + ", ".join(HOSTS))
    return value


def _guard(path: Path, *, within: Path | None = None) -> Path:
    path = path.expanduser().absolute()
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise ValueError("Managed installation paths cannot contain links/junctions: " + str(ancestor))
    resolved = path.resolve()
    if within is not None and not resolved.is_relative_to(within.resolve()):
        raise ValueError("Managed path escaped its installation root")
    return resolved


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _write(path: Path, content: bytes, *, within: Path) -> None:
    path = _guard(path, within=within)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _snapshot() -> tuple[dict[str, Any], dict[str, bytes], str]:
    files: dict[str, bytes] = {}

    def include(path: Path) -> None:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Release source cannot contain links/junctions")
        if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
            files[path.relative_to(SOURCE_ROOT).as_posix()] = path.read_bytes()

    # Start only from release roots and prune before descent. Filtering the
    # results of rglob still scans unrelated environments and private files.
    for root in sorted(SOURCE_ROOT.iterdir()):
        if root.name not in _ROOTS or root.name in _EXCLUDED:
            continue
        include(root)
        if not root.is_dir():
            continue
        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in _EXCLUDED)
            for name in directories:
                include(Path(current) / name)
            for name in sorted(filenames):
                if name not in _EXCLUDED:
                    include(Path(current) / name)
    manifest = json.loads(files["plugin.json"])
    if manifest.get("name") != PLUGIN_NAME:
        raise ValueError("Unexpected plugin identity")
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8") + b"\0" + content + b"\0")
    return manifest, files, digest.hexdigest()


def _home(config_root: str | Path | None) -> Path:
    return _guard(Path(config_root) if config_root is not None else Path.home())


def _provider_info(host: str, provider: str | None, model: str | None) -> dict[str, Any]:
    selected = provider or host
    from .providers import canonical_provider, describe_provider
    selected = canonical_provider(selected)
    result = describe_provider(selected, model=model)
    result["explicit_selection"] = provider is not None
    result["selection_required"] = host == "cursor" and provider is None
    if result["selection_required"]:
        result["reason"] = "Choose an explicitly supported background provider for Cursor; its own isolated background channel is not verified."
    return result


def _locations(host: str, data: Path, home: Path, version: str, digest: str) -> dict[str, Path]:
    release = version + "-" + digest[:12]
    core = data / "installations" / "core" / release
    marketplace = data / "installations" / "marketplaces" / host
    if host == "codex":
        legacy = data / "marketplaces" / MARKETPLACE_NAME
        legacy_catalog = legacy / ".agents" / "plugins" / "marketplace.json"
        if legacy_catalog.exists() and _read_json(legacy_catalog, {}).get("name") == MARKETPLACE_NAME:
            # Preserve the existing 0.1.x native marketplace identity and source
            # root. The new entry points to a versioned thin wrapper in it.
            marketplace = legacy
    wrapper = (home / ".cursor" / "plugins" / "local" / PLUGIN_NAME if host == "cursor"
               else marketplace / "plugins" / (PLUGIN_NAME + "-" + release))
    return {"core": core, "wrapper": wrapper, "marketplace": marketplace,
            "receipt": data / "installations" / "receipts" / (host + ".json")}


def _host_environment(home: Path, isolated: bool) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    if isolated:
        environment.update({"HOME": str(home), "USERPROFILE": str(home),
                            "CODEX_HOME": str(home / ".codex"),
                            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                            "GEMINI_CLI_HOME": str(home),
                            "CURSOR_CONFIG_DIR": str(home / ".cursor")})
    return environment


def _native_commands(host: str, locations: dict[str, Path]) -> list[list[str]]:
    prefix = _native_prefix(host)
    if not prefix or host == "cursor":
        return []
    if host == "codex":
        return [prefix + ["plugin", "marketplace", "add", str(locations["marketplace"]), "--json"],
                prefix + ["plugin", "add", PLUGIN_NAME + "@" + MARKETPLACE_NAME, "--json"]]
    if host == "claude-code":
        return [prefix + ["plugin", "marketplace", "add", str(locations["marketplace"])],
                prefix + ["plugin", "install", PLUGIN_NAME + "@" + MARKETPLACE_NAME, "--scope", "user"]]
    return [prefix + ["extensions", "install", str(locations["wrapper"])]]


def _native_prefix(host: str) -> list[str]:
    if host == "cursor":
        return []  # Native local-import directory; no Cursor CLI is executed.
    from .providers import ProviderUnavailable, resolve_cli_command
    try:
        return resolve_cli_command(host)
    except ProviderUnavailable:
        return []


def plan_install(host: str, data_dir: str | Path | None = None, config_root: str | Path | None = None,
                 provider: str | None = None, model: str | None = None,
                 runtime_python: str | Path | None = None) -> dict[str, Any]:
    """Resolve a release and native routes without writing or calling a model."""
    host = _host(host)
    from .runtime import validate_model
    model = validate_model(model)
    data = _guard(private_data_dir(data_dir))
    home = _home(config_root)
    manifest, _, digest = _snapshot()
    version = manifest["version"]
    locations = _locations(host, data, home, version, digest)
    for location in locations.values():
        _guard(location)
    runtime = Path(runtime_python).expanduser().resolve() if runtime_python else _guard(data / "runtime" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    info = _provider_info(host, provider, model)
    background_supported = info.get("supports_background", info.get("supports_background_analysis", False))
    if not background_supported:
        info["selection_required"] = True
    return {"host": host, "plugin": PLUGIN_NAME, "version": version, "release_sha256": digest,
            "data_dir": str(data), "config_root": str(home), "runtime_python": str(runtime),
            "core_dir": str(locations["core"]), "native_plugin_dir": str(locations["wrapper"]),
            "marketplace_dir": str(locations["marketplace"]), "receipt_path": str(locations["receipt"]),
            "provider": info.get("provider", provider or host), "model": model,
            "provider_status": info, "host_executable": shutil.which(_COMMANDS[host]),
            "native_commands": _native_commands(host, locations),
            "requirements": {"python": ">=3.12", "mcp": "==" + MCP_VERSION,
                             "host_hook_review": host == "codex", "model_called": False},
            "private_data_preserved": True, "mutated": False}


_WRAPPER_LAUNCH = '''"""Generated native adapter; the shared core remains outside this wrapper."""
import json, os, runpy, sys
from pathlib import Path
root = Path(__file__).resolve().parent
settings = json.loads((root / "local-settings.json").read_text(encoding="utf-8-sig"))
for field, variable in (("data_dir", "BLUEPRINT_WORKFLOW_DATA_DIR"), ("runtime_python", "BLUEPRINT_WORKFLOW_PYTHON")):
    if settings.get(field) is not None:
        os.environ.setdefault(variable, str(settings[field]))
for field, variable in (("host", "BLUEPRINT_WORKFLOW_HOST"), ("provider", "BLUEPRINT_WORKFLOW_PROVIDER")):
    os.environ[variable] = str(settings[field])
if settings.get("model") is None:
    os.environ.pop("BLUEPRINT_WORKFLOW_MODEL", None)
else:
    os.environ["BLUEPRINT_WORKFLOW_MODEL"] = str(settings["model"])
core = Path(settings["core_root"])
sys.path.insert(0, str(core))
runpy.run_path(str(core / "scripts" / "launch.py"), run_name="__main__")
'''


def _hook_config(host: str, runtime: Path, wrapper: Path, data: Path) -> dict[str, Any] | None:
    if host not in {"codex", "claude-code"}:
        return None
    arguments = [str(runtime), str(wrapper / "launch.py"), "--data-dir", str(data), "hook", "--host", host]
    if os.name == "nt":
        # A hook is a shell command, unlike native registration's argv. Avoid
        # expansion differences between Windows cmd and Git Bash; quote every
        # argument and reject unsupported shell-metacharacter paths explicitly.
        if any(any(character in argument for character in '&|<>^%!$`\"\'()\r\n') for argument in arguments):
            raise ValueError("Windows hook paths contain unsupported shell metacharacters; choose a plain private data/runtime path")
        command = " ".join('"' + argument.replace("\\", "/") + '"' for argument in arguments)
    else:
        command = shlex.join(arguments)
    events = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact")
    if host == "claude-code":
        events += ("PostToolUseFailure",)
    return {"hooks": {event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}],
                               **({"matcher": ".*"} if "ToolUse" in event else {})}]
                      for event in events}}


def _wrapper_files(plan: dict[str, Any], source_files: dict[str, bytes]) -> dict[str, bytes]:
    host, wrapper = plan["host"], Path(plan["native_plugin_dir"])
    runtime, data = Path(plan["runtime_python"]), Path(plan["data_dir"])
    settings = {"data_dir": str(data), "runtime_python": str(runtime), "core_root": plan["core_dir"],
                "host": host, "provider": plan["provider"], "model": plan["model"]}
    environment = {"BLUEPRINT_WORKFLOW_DATA_DIR": str(data), "BLUEPRINT_WORKFLOW_PYTHON": str(runtime),
                   "BLUEPRINT_WORKFLOW_HOST": host, "BLUEPRINT_WORKFLOW_PROVIDER": plan["provider"]}
    if plan["model"] is not None:
        environment["BLUEPRINT_WORKFLOW_MODEL"] = plan["model"]
    server = {"command": str(runtime), "args": [str(wrapper / "launch.py"), "mcp"],
              "cwd": str(wrapper), "env": environment}
    manifest = {"name": PLUGIN_NAME, "version": plan["version"],
                "description": "Learn reusable workflows while completing real work."}
    files = {"launch.py": _WRAPPER_LAUNCH.encode(), "local-settings.json": _json(settings),
             ".blueprint-workflow-managed.json": _json({"owner": PLUGIN_NAME, "host": host, "version": plan["version"]})}
    skill = source_files["skills/blueprint-workflow/SKILL.md"]
    files["skills/blueprint-workflow/SKILL.md"] = skill
    hooks = _hook_config(host, runtime, wrapper, data)
    if hooks:
        files["hooks/hooks.json"] = _json(hooks)
    if host == "codex":
        interface = json.loads(source_files["plugin.json"])["extensions"]["com.openai"]["interface"]
        for field in ("logo", "logoDark", "composerIcon", "composerIconDark"):
            if reference := interface.get(field):
                relative = str(reference).removeprefix("./")
                asset = Path(relative)
                if asset.is_absolute() or ".." in asset.parts or relative not in source_files:
                    raise ValueError("Plugin interface asset must be a contained release file: " + field)
                files[relative] = source_files[relative]
        files["plugin.json"] = _json({"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                                       **manifest, "extensions": {"com.openai": {"hooks": "./hooks/hooks.json", "interface": interface}}})
        files["mcp.json"] = _json({"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                                    "mcpServers": {PLUGIN_NAME: {"type": "stdio", **server}}})
    elif host == "claude-code":
        files[".claude-plugin/plugin.json"] = _json(manifest)
        files[".mcp.json"] = _json({"mcpServers": {PLUGIN_NAME: server}})
    elif host == "cursor":
        files["plugin.json"] = _json({"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", **manifest})
        files["mcp.json"] = _json({"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                                    "mcpServers": {PLUGIN_NAME: {"type": "stdio", **server}}})
    else:
        files["gemini-extension.json"] = _json({**manifest, "mcpServers": {PLUGIN_NAME: server}, "contextFileName": "GEMINI.md"})
        body = skill.decode("utf-8")
        if body.startswith("---"):
            body = body.split("---", 2)[2].lstrip()
        files["GEMINI.md"] = body.encode("utf-8")
    return files


def _catalog(plan: dict[str, Any]) -> tuple[Path, bytes] | None:
    host = plan["host"]
    if host not in {"codex", "claude-code"}:
        return None
    root = Path(plan["marketplace_dir"])
    relative = Path(plan["native_plugin_dir"]).relative_to(root).as_posix()
    path = root / (".agents/plugins/marketplace.json" if host == "codex" else ".claude-plugin/marketplace.json")
    existing = _read_json(path, {"name": MARKETPLACE_NAME, "plugins": []})
    if existing.get("name") != MARKETPLACE_NAME:
        raise ValueError("The existing marketplace is owned by a different identity")
    entries = [entry for entry in existing.get("plugins", []) if entry.get("name") != PLUGIN_NAME]
    if host == "codex":
        entries.append({"name": PLUGIN_NAME, "source": {"source": "local", "path": "./" + relative},
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"})
    else:
        existing.setdefault("owner", {"name": "Blueprint Workflow"})
        entries.append({"name": PLUGIN_NAME, "source": "./" + relative})
    existing["plugins"] = entries
    return path, _json(existing)


def _run_native(command: list[str], environment: dict[str, str], timeout: float = 60) -> dict[str, Any]:
    if not command or Path(command[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ValueError("Native commands must use a resolved executable, not a Windows shell shim")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=environment, timeout=timeout,
                                check=False, **options)
        return {"command": command, "returncode": result.returncode,
                "stdout": sanitize(result.stdout[-4000:]), "stderr": sanitize(result.stderr[-4000:])}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "returncode": None, "error": sanitize(str(exc)), "confirmation_may_be_required": True}


def _quiesce(data: Path, seconds: float = 10) -> bool:
    from .worker import _pid_exists, stop_worker, worker_status
    status = worker_status(data)
    if not status.get("running"):
        pending = _read_json(data / "worker-starting.json", {})
        already_stopped = (pending.get("pid") == status.get("pid") and
                           float(status.get("stopped_at", 0)) >= float(pending.get("started_at", 0)))
        if (not already_stopped and time.time() - float(pending.get("started_at", 0)) < 30
                and _pid_exists(int(pending.get("pid", 0)))):
            return False
        return True
    stop_worker(data)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not worker_status(data).get("running"):
            return True
        time.sleep(0.1)
    return False


def _runtime(plan: dict[str, Any], *, skip_dependencies: bool) -> None:
    if sys.version_info < (3, 12):
        raise ValueError("Python 3.12+ is required for the shared runtime")
    runtime = Path(plan["runtime_python"])
    data = Path(plan["data_dir"])
    if not runtime.exists():
        if skip_dependencies:
            raise ValueError("Runtime Python is missing; run setup without --skip-dependencies")
        expected = data / "runtime" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if runtime != expected:
            raise ValueError("Explicit runtime Python does not exist")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run([sys.executable, "-m", "venv", str(data / "runtime")], capture_output=True, text=True, **options)
        if result.returncode:
            raise RuntimeError("Private runtime creation failed: " + sanitize(result.stderr[-1500:]))
    if not skip_dependencies:
        _guard(data / "runtime")
        if not runtime.resolve().is_relative_to((data / "runtime").resolve()):
            raise ValueError("Dependency setup only modifies the private runtime; use --skip-dependencies for an explicit existing Python")
        result = _run_native([str(runtime), "-m", "pip", "install", "--disable-pip-version-check", "mcp==" + MCP_VERSION], dict(os.environ), timeout=300)
        if result["returncode"] != 0:
            raise RuntimeError("Private runtime dependency setup failed: " + str(result.get("stderr", result.get("error", ""))))


def _install_host_impl(host: str, data_dir: str | Path | None = None, config_root: str | Path | None = None,
                 provider: str | None = None, model: str | None = None,
                 runtime_python: str | Path | None = None, *, stage_only: bool = False,
                 register_native: bool = True, skip_dependencies: bool = False,
                 worker_wait_seconds: float = 10, native_timeout: float = 60) -> dict[str, Any]:
    plan = plan_install(host, data_dir, config_root, provider, model, runtime_python)
    if plan["provider_status"].get("selection_required"):
        return {**plan, "status": "analysis_configuration_required", "provider_selection_required": True, "registered": False}
    stage_mode = stage_only or not register_native
    if plan["host"] == "cursor" and stage_mode:
        plan["native_import_dir"] = plan["native_plugin_dir"]
        plan["native_plugin_dir"] = str(Path(plan["marketplace_dir"]) / "plugins" / (PLUGIN_NAME + "-" + plan["version"] + "-" + plan["release_sha256"][:12]))
    data, wrapper, core = Path(plan["data_dir"]), Path(plan["native_plugin_dir"]), Path(plan["core_dir"])
    receipt_path = Path(plan["receipt_path"])
    if stage_mode:
        receipt_path = receipt_path.with_name(plan["host"] + ".staged.json")
        plan["receipt_path"] = str(receipt_path)
    old_receipt = _read_json(receipt_path)
    if not stage_only and not _quiesce(data, worker_wait_seconds):
        return {**plan, "status": "waiting_for_worker", "registered": False, "mutated": False,
                "reason": "Current analysis is finishing; retry installation after the worker stops."}
    _runtime(plan, skip_dependencies=skip_dependencies)
    _, source_files, fingerprint = _snapshot()
    if fingerprint != plan["release_sha256"]:
        raise RuntimeError("Source changed during installation planning; retry the stable release")
    for relative, content in source_files.items():
        path = core / relative
        if path.exists() and path.read_bytes() != content:
            raise ValueError("A versioned core snapshot has been modified; choose a clean release")
        if not path.exists():
            _write(path, content, within=data)
    marker = wrapper / ".blueprint-workflow-managed.json"
    if wrapper.exists() and any(wrapper.iterdir()) and _read_json(marker, {}).get("owner") != PLUGIN_NAME:
        raise ValueError("The native plugin location contains an unmanaged plugin; it was not overwritten")
    generated = _wrapper_files(plan, source_files)
    backups: dict[Path, bytes | None] = {}
    touched: dict[Path, str] = {}
    try:
        for relative, content in generated.items():
            path = wrapper / relative
            backups[path] = path.read_bytes() if path.exists() else None
            _write(path, content, within=wrapper)
            touched[path] = hashlib.sha256(content).hexdigest()
        catalog = _catalog(plan)
        if catalog:
            path, content = catalog
            backups[path] = path.read_bytes() if path.exists() else None
            _write(path, content, within=Path(plan["marketplace_dir"]))
            touched[path] = hashlib.sha256(content).hexdigest()
        native_results = []
        registered = False
        commands = plan["native_commands"]
        if not stage_only and register_native and commands:
            environment = _host_environment(Path(plan["config_root"]), config_root is not None)
            for command in commands:
                result = _run_native(command, environment, native_timeout)
                native_results.append(result)
                if result["returncode"] != 0:
                    raise RuntimeError("Native registration did not complete: " + json.dumps(result, ensure_ascii=False))
            registered = True
        elif not stage_only and register_native and plan["host"] == "cursor":
            registered = True  # Native local-import directory, not a tool discovery claim.
        receipt = {**plan, "managed_by": PLUGIN_NAME, "managed_files": {str(path): digest for path, digest in touched.items()},
                   "registered": registered, "native_results": native_results, "installed_at": time.time(),
                   "runtime_shared": True, "core_shared": True, "native_verified": False,
                   "analysis_ready": bool(plan["provider_status"].get("available") and plan["provider_status"].get("supports_background", plan["provider_status"].get("supports_background_analysis"))),
                   "status": "installed_local" if registered else "staged", "mutated": True}
        if not commands and plan["host"] != "cursor":
            receipt["registration_pending"] = "Host executable is missing; run native registration when the host is installed."
        _write(receipt_path, _json(receipt), within=data)
        return receipt
    except Exception:
        # Only files/catalogs owned by this adapter are restored. Never copy a
        # whole settings/auth file over concurrent changes made by another app.
        for path, content in reversed(list(backups.items())):
            if content is None:
                _guard(path)
                path.unlink(missing_ok=True)
            else:
                _write(path, content, within=path.parent)
        if old_receipt is not None:
            _write(receipt_path, _json(old_receipt), within=data)
        raise


def install_host(host: str, data_dir: str | Path | None = None, config_root: str | Path | None = None,
                 provider: str | None = None, model: str | None = None,
                 runtime_python: str | Path | None = None, *, stage_only: bool = False,
                 register_native: bool = True, skip_dependencies: bool = False,
                 worker_wait_seconds: float = 10, native_timeout: float = 60) -> dict[str, Any]:
    """Serialize runtime/config upgrades against new background worker launches."""
    arguments = (host, data_dir, config_root, provider, model, runtime_python)
    options = {"stage_only": stage_only, "register_native": register_native,
               "skip_dependencies": skip_dependencies, "worker_wait_seconds": worker_wait_seconds,
               "native_timeout": native_timeout}
    plan = plan_install(*arguments)
    if plan["provider_status"].get("selection_required") or stage_only or not register_native:
        return _install_host_impl(*arguments, **options)
    from .worker import WorkerLock
    launch_lock = WorkerLock(Path(plan["data_dir"]) / "worker-launch.lock")
    if not launch_lock.acquire():
        return {**plan, "status": "waiting_for_worker", "registered": False,
                "reason": "A background worker is starting or another installation is in progress."}
    try:
        return _install_host_impl(*arguments, **options)
    finally:
        launch_lock.release()


def diagnose_host(host: str, data_dir: str | Path | None = None, config_root: str | Path | None = None,
                  provider: str | None = None, model: str | None = None,
                  runtime_python: str | Path | None = None) -> dict[str, Any]:
    plan = plan_install(host, data_dir, config_root, provider, model, runtime_python)
    receipt = _read_json(Path(plan["receipt_path"]))
    result = {**plan, "installed": bool(receipt and receipt.get("registered")), "model_called": False, "mutated": False,
              "native_verified": False, "runtime_exists": Path(plan["runtime_python"]).is_file()}
    if receipt:
        result["installed_version"] = receipt["version"]
        result["registered"] = receipt.get("registered", False)
        result["native_plugin_dir"] = receipt["native_plugin_dir"]
        result["managed_files_intact"] = all(Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                                              for path, digest in receipt.get("managed_files", {}).items())
    return result


def uninstall_host(host: str, data_dir: str | Path | None = None, config_root: str | Path | None = None,
                   *, keep_data: bool = True, register_native: bool = True, native_timeout: float = 60) -> dict[str, Any]:
    host = _host(host)
    if not keep_data:
        raise ValueError("Uninstall preserves recordings and exports. Explicit data cleanup is a separate operation.")
    data = _guard(private_data_dir(data_dir))
    receipt_path = data / "installations" / "receipts" / (host + ".json")
    receipt = _read_json(receipt_path)
    if not receipt:
        return {"host": host, "status": "not_installed", "private_data_preserved": True, "mutated": False}
    home = _home(config_root if config_root is not None else receipt.get("config_root"))
    results = []
    prefix = _native_prefix(host)
    if register_native and receipt.get("registered") and prefix and host != "cursor":
        if host == "codex":
            command = prefix + ["plugin", "remove", PLUGIN_NAME + "@" + MARKETPLACE_NAME, "--json"]
        elif host == "claude-code":
            command = prefix + ["plugin", "uninstall", PLUGIN_NAME + "@" + MARKETPLACE_NAME, "--scope", "user"]
        else:
            command = prefix + ["extensions", "uninstall", PLUGIN_NAME]
        results.append(_run_native(command, _host_environment(home, config_root is not None), native_timeout))
        if results[-1]["returncode"] != 0:
            return {"host": host, "status": "native_removal_pending", "native_results": results,
                    "private_data_preserved": True, "mutated": False}
    elif register_native and receipt.get("registered") and not prefix and host != "cursor":
        return {"host": host, "status": "native_removal_pending", "reason": "Host executable is unavailable",
                "private_data_preserved": True, "mutated": False}
    preserved = []
    wrapper = Path(receipt["native_plugin_dir"])
    marketplace = _guard(Path(receipt["marketplace_dir"]))
    allowed_marketplaces = {data / "installations" / "marketplaces" / host}
    if host == "codex":
        allowed_marketplaces.add(data / "marketplaces" / MARKETPLACE_NAME)
    if marketplace not in allowed_marketplaces:
        raise ValueError("Receipt marketplace is outside this host's managed locations")
    wrapper = _guard(wrapper, within=home / ".cursor" / "plugins" / "local" if host == "cursor" else marketplace / "plugins")
    catalog_path = (Path(receipt["marketplace_dir"]) / (".agents/plugins/marketplace.json" if host == "codex" else ".claude-plugin/marketplace.json")) if host in {"codex", "claude-code"} else None
    for raw_path, expected in receipt.get("managed_files", {}).items():
        path = _guard(Path(raw_path))
        if catalog_path is not None and path == catalog_path.resolve():
            catalog = _read_json(path, {})
            if catalog.get("name") != MARKETPLACE_NAME:
                raise ValueError("The managed catalog identity has changed; it was not modified")
            catalog["plugins"] = [entry for entry in catalog.get("plugins", []) if entry.get("name") != PLUGIN_NAME]
            _write(path, _json(catalog), within=Path(receipt["marketplace_dir"]))
        elif path.is_file():
            if not path.is_relative_to(wrapper.resolve()):
                raise ValueError("Receipt contains a file outside its native wrapper")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                preserved.append(str(path))
            else:
                path.unlink()
    receipt_path.unlink()
    return {"host": host, "status": "modified_files_preserved" if preserved and host == "cursor" else "uninstalled", "private_data_preserved": True,
            "shared_runtime_preserved": True, "shared_core_preserved": True,
            "modified_files_preserved": preserved, "native_results": results, "mutated": True}
