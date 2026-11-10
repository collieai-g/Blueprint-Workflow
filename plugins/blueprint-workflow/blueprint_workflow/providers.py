"""Replaceable, local CLI background analysis engines.

Providers receive the same immutable draft-and-delta snapshot. They do not
execute business work, share another provider's credentials, or silently fall
back to Codex. CLI authentication remains with the user's original CLI.

Primary CLI contracts:
https://code.claude.com/docs/en/cli-reference
https://geminicli.com/docs/cli/headless/
https://geminicli.com/docs/reference/configuration/
https://geminicli.com/docs/reference/policy-engine/
https://cursor.com/docs/cli/reference/parameters
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .analyzer import (
    AnalysisError, AnalysisProtocolError, CodexAnalyzer, analysis_environment,
    analysis_prompt, run_analysis_process,
)


PROVIDER_NAMES = ("codex", "claude-code", "gemini-cli", "cursor")
_ALIASES = {"claude": "claude-code", "claudecode": "claude-code", "gemini": "gemini-cli", "geminicli": "gemini-cli", "cursor-cli": "cursor"}
_DISPLAY = {"codex": "Codex", "claude-code": "Claude Code", "gemini-cli": "Gemini CLI", "cursor": "Cursor CLI"}
_COMMANDS = {"codex": ("codex",), "claude-code": ("claude",), "gemini-cli": ("gemini",), "cursor": ("agent", "cursor-agent")}
_NPM = {
    "codex": ("@openai/codex", "codex"),
    "claude-code": ("@anthropic-ai/claude-code", "claude"),
    "gemini-cli": ("@google/gemini-cli", "gemini"),
}
_CURSOR_REASON = (
    "Cursor supports headless Ask mode, but this adapter has not established a "
    "per-invocation route that also disables inherited MCP servers, hooks and "
    "other effectful customizations. Select codex, claude-code or gemini-cli "
    "explicitly as the background engine; the Cursor host adapter is separate."
)


class ProviderUnavailable(AnalysisError):
    """The selected engine cannot be used; another engine is never substituted."""


@runtime_checkable
class AnalysisProvider(Protocol):
    timeout: float

    def analyze(self, job: dict[str, Any]) -> dict[str, Any]: ...


def canonical_provider(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Analysis provider must be a nonempty name")
    canonical = _ALIASES.get(name.strip().lower(), name.strip().lower())
    if canonical not in PROVIDER_NAMES:
        raise ValueError("Unknown analysis provider; choose " + ", ".join(PROVIDER_NAMES))
    return canonical


normalize_provider = canonical_provider


def _configured_executable(provider: str) -> str | None:
    value = os.environ.get("BLUEPRINT_WORKFLOW_" + provider.upper().replace("-", "_") + "_EXECUTABLE")
    if value is None:
        return None
    if not value.strip() or any(char in value for char in ("\x00", "\r", "\n")):
        raise ProviderUnavailable("Configured CLI executable must be a path, not a shell command")
    return os.path.expandvars(os.path.expanduser(value.strip()))


def _npm_entrypoint(provider: str, shim: Path) -> list[str]:
    """Resolve only a verified adjacent npm package; never execute a .cmd file."""
    if provider not in _NPM:
        raise ProviderUnavailable("This Windows CLI shim has no verified native entrypoint mapping")
    package_name, binary_name = _NPM[provider]
    package_dir = shim.parent / "node_modules" / Path(package_name)
    try:
        manifest = json.loads((package_dir / "package.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProviderUnavailable("Cannot resolve Windows CLI shim to its installed npm package entrypoint") from exc
    if not isinstance(manifest, dict) or manifest.get("name") != package_name:
        raise ProviderUnavailable("Windows CLI shim package identity does not match the selected provider")
    binary = manifest.get("bin")
    relative = binary.get(binary_name) if isinstance(binary, dict) else binary
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ProviderUnavailable("Installed CLI package has no valid matching bin entry")
    package_root = package_dir.resolve()
    entry = (package_root / relative).resolve()
    if not entry.is_relative_to(package_root) or not entry.is_file() or entry.suffix.lower() not in {".js", ".mjs", ".cjs"}:
        raise ProviderUnavailable("Installed CLI bin entry must be a contained JavaScript file")
    adjacent_node = shim.parent / "node.exe"
    node = str(adjacent_node) if adjacent_node.is_file() else shutil.which("node")
    if not node or not Path(node).is_file() or Path(node).suffix.lower() not in {".exe", ".com"}:
        raise ProviderUnavailable("A native Node executable is required for this Windows npm CLI installation")
    return [str(Path(node).resolve()), str(entry)]


def resolve_cli_command(provider: str, executable: str | None = None, *, check_exists: bool = True) -> list[str]:
    provider = canonical_provider(provider)
    selected = executable or _configured_executable(provider)
    if selected is None:
        selected = next((candidate for name in _COMMANDS[provider] if (candidate := shutil.which(name))), None)
    elif not Path(selected).is_file():
        selected = shutil.which(selected) or selected
    if not selected:
        raise ProviderUnavailable(f"{_DISPLAY[provider]} CLI was not found. Install and authenticate that CLI, then retry.")
    if any(char in selected for char in ("\x00", "\r", "\n")):
        raise ProviderUnavailable("CLI executable must be a path, not a shell command")
    path = Path(selected)
    if check_exists and not path.is_file():
        raise ProviderUnavailable(f"{_DISPLAY[provider]} executable does not exist")
    if os.name == "nt":
        if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            return _npm_entrypoint(provider, path)
        if path.suffix.lower() not in {".exe", ".com"}:
            raise ProviderUnavailable("Use a native Windows executable or a verified npm CLI shim")
    elif check_exists and not os.access(path, os.X_OK):
        raise ProviderUnavailable("Selected CLI file is not executable")
    return [str(path.resolve()) if path.is_file() else str(path)]


def _gemini_system_settings_path() -> Path:
    override = os.environ.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "gemini-cli" / "settings.json"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/GeminiCli/settings.json")
    return Path("/etc/gemini-cli/settings.json")


def _check_gemini_system_settings() -> None:
    """Respect admin configuration; never replace it with our own system file."""
    path = _gemini_system_settings_path()
    if not path.exists():
        return
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict):
            raise ValueError("not an object")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProviderUnavailable("Cannot verify Gemini system settings for background analysis isolation") from exc
    tools = settings.get("tools") or {}
    hooks = settings.get("hooksConfig") or {}
    skills = settings.get("skills") or {}
    mcp = settings.get("mcp") or {}
    if not all(isinstance(section, dict) for section in (tools, hooks, skills, mcp)):
        raise ProviderUnavailable("Gemini system settings conflict with controlled background analysis")
    conflicting = (
        ("core" in tools and tools["core"] != [])
        or bool(tools.get("discoveryCommand")) or bool(tools.get("callCommand"))
        or bool(mcp.get("serverCommand"))
        or ("enabled" in hooks and hooks["enabled"] is not False)
        or ("enabled" in skills and skills["enabled"] is not False)
    )
    if conflicting:
        raise ProviderUnavailable("Gemini system policy overrides the no-tools/no-hooks background configuration; no request was started")


def describe_provider(provider: str, model: str | None = None) -> dict[str, Any]:
    provider = canonical_provider(provider)
    result = {
        "provider": provider, "name": _DISPLAY[provider], "model": model,
        "available": False, "executable": None, "reason": "",
        "supports_background": provider != "cursor", "supports_background_analysis": provider != "cursor",
        "authentication": "not_checked", "model_request_sent": False,
    }
    if provider == "cursor":
        result["reason"] = _CURSOR_REASON
        return result
    try:
        command = resolve_cli_command(provider)
        if provider == "gemini-cli":
            _check_gemini_system_settings()
    except (ProviderUnavailable, OSError, ValueError) as exc:
        result["reason"] = str(exc)
        return result
    result.update(available=True, executable=command[0], reason="CLI entrypoint available; authentication and model access have not been tested")
    return result


def _analysis_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("change_summary"), str) or not (
        isinstance(value.get("workflow"), dict) or value.get("protocol_version") == 2 and isinstance(value.get("patch"), dict)
    ):
        raise AnalysisProtocolError("Analysis response must contain a workflow or a versioned learning patch and change_summary")
    return value


def _json_envelope(stdout: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout)
    except (TypeError, ValueError) as exc:
        raise AnalysisProtocolError(f"{label} did not return a JSON response envelope") from exc
    if not isinstance(value, dict):
        raise AnalysisProtocolError(f"{label} response envelope must be an object")
    if value.get("is_error") or value.get("error"):
        raise AnalysisProtocolError(f"{label} reported that analysis failed")
    return value


class _CLIAnalysisProvider:
    provider: str

    def __init__(self, data_dir: Path | str, *, model: str | None = None, timeout: float = 240,
                 executable: str | None = None, command_prefix: list[str] | None = None) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.model = model
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("Analysis timeout must be positive")
        if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200 or any(char in model for char in ("\x00", "\r", "\n"))):
            raise ValueError("Analysis model must be a single model identifier")
        self.command_prefix = list(command_prefix) if command_prefix else resolve_cli_command(self.provider, executable)

    def _prepare(self, work_dir: Path, schema: dict[str, Any], environment: dict[str, str]) -> list[str]:
        raise NotImplementedError

    def _decode(self, stdout: str) -> dict[str, Any]:
        raise NotImplementedError

    def analyze(self, job: dict[str, Any]) -> dict[str, Any]:
        from .analyzer import analyze_with_context
        return analyze_with_context(self._analyze_once, job)

    def _analyze_once(self, job: dict[str, Any]) -> dict[str, Any]:
        from .model import analysis_json_schema

        root = self.data_dir / "analysis-work"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=self.provider + "-", dir=root) as temporary:
            work_dir = Path(temporary)
            environment = analysis_environment()
            command = self._prepare(work_dir, analysis_json_schema(int(job.get("protocol_version", 1))), environment)
            stdout, _stderr = run_analysis_process(command, analysis_prompt(job, include_schema=True), work_dir, environment,
                                                 timeout=self.timeout, label=_DISPLAY[self.provider])
            return self._decode(stdout)


class ClaudeCodeAnalyzer(_CLIAnalysisProvider):
    provider = "claude-code"

    def _prepare(self, work_dir: Path, schema: dict[str, Any], environment: dict[str, str]) -> list[str]:
        settings = work_dir / "claude-settings.json"
        settings.write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
        mcp = work_dir / "empty-mcp.json"
        mcp.write_text('{"mcpServers":{}}', encoding="utf-8")
        command = self.command_prefix + [
            "-p", "--safe-mode", "--no-session-persistence", "--no-chrome",
            "--tools", "", "--disallowedTools", "*", "--permission-mode", "dontAsk",
            "--strict-mcp-config", "--mcp-config", str(mcp), "--settings", str(settings),
            "--output-format", "json", "--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        ]
        if self.model:
            command += ["--model", self.model]
        return command

    def _decode(self, stdout: str) -> dict[str, Any]:
        envelope = _json_envelope(stdout, "Claude Code")
        if envelope.get("subtype") not in {None, "success"}:
            raise AnalysisProtocolError("Claude Code did not complete a successful analysis turn")
        if envelope.get("permission_denials"):
            raise AnalysisProtocolError("Claude Code attempted a tool operation during background analysis")
        return _analysis_object(envelope.get("structured_output"))


class GeminiCLIAnalyzer(_CLIAnalysisProvider):
    provider = "gemini-cli"

    def _prepare(self, work_dir: Path, schema: dict[str, Any], environment: dict[str, str]) -> list[str]:
        _check_gemini_system_settings()
        configuration = work_dir / ".gemini"
        configuration.mkdir()
        # tools.core=[] is an actual empty allowlist, not auto-approval. See
        # official Gemini Config.createToolRegistry/maybeRegister implementation.
        settings = {
            "tools": {"core": [], "discoveryCommand": "", "callCommand": ""},
            "mcp": {"allowed": ["__blueprint_analysis_no_mcp__"], "serverCommand": ""},
            "hooksConfig": {"enabled": False}, "skills": {"enabled": False},
            "advanced": {"ignoreLocalEnv": True},
        }
        (configuration / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        policy = work_dir / "deny-all-tools.toml"
        policy.write_text('[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n\n[[rule]]\ntoolName = "*"\nmcpName = "*"\ndecision = "deny"\npriority = 999\n', encoding="utf-8")
        # Trust only this newly generated private snapshot folder for this
        # process, so its restrictive project settings are actually loaded.
        environment["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        command = self.command_prefix + [
            "-p", "Transform the provided workflow snapshot into the required JSON object. Return JSON only.",
            "--output-format", "json", "--extensions", "none", "--approval-mode", "default",
            "--allowed-mcp-server-names", "__blueprint_analysis_no_mcp__", "--policy", str(policy),
        ]
        if self.model:
            command += ["--model", self.model]
        return command

    def _decode(self, stdout: str) -> dict[str, Any]:
        envelope = _json_envelope(stdout, "Gemini CLI")
        stats = envelope.get("stats") or {}
        tools = (stats.get("tools") or {}) if isinstance(stats, dict) else {}
        if isinstance(tools, dict) and any(isinstance(tools.get(key), (int, float)) and tools[key] > 0 for key in ("totalCalls", "total_calls", "successful", "total")):
            raise AnalysisProtocolError("Gemini CLI attempted a tool operation during background analysis")
        response = envelope.get("response")
        if not isinstance(response, str):
            raise AnalysisProtocolError("Gemini CLI response must contain JSON text in response")
        try:
            return _analysis_object(json.loads(response))
        except ValueError as exc:
            raise AnalysisProtocolError("Gemini CLI model response did not conform to JSON") from exc


def create_provider(data_dir: Path | str, provider: str = "codex", model: str | None = None,
                    timeout: float = 240) -> AnalysisProvider:
    provider = canonical_provider(provider)
    if provider == "cursor":
        raise ProviderUnavailable(_CURSOR_REASON)
    command = resolve_cli_command(provider)
    if provider == "codex":
        return CodexAnalyzer(data_dir, command_prefix=command, model=model, timeout=timeout)
    if provider == "claude-code":
        return ClaudeCodeAnalyzer(data_dir, command_prefix=command, model=model, timeout=timeout)
    _check_gemini_system_settings()
    return GeminiCLIAnalyzer(data_dir, command_prefix=command, model=model, timeout=timeout)
