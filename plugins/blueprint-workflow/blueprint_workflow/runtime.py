"""Explicit host identity and analysis selection, independent of MCP transport."""
from __future__ import annotations

import os
import re

HOST_NAMES = ("codex", "claude-code", "cursor", "gemini-cli")


def canonical_host(value: str) -> str:
    name = {"claude": "claude-code", "gemini": "gemini-cli", "cursor-cli": "cursor"}.get(value, value)
    if name not in HOST_NAMES:
        raise ValueError(f"Unsupported host: {value}")
    return name


def current_host() -> str | None:
    value = os.environ.get("BLUEPRINT_WORKFLOW_HOST")
    return canonical_host(value) if value else None


def scope_host_id(value: str | None, host: str | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,280}", value):
        raise ValueError("host_session_id must be a bounded host identifier")
    host = canonical_host(host) if host else current_host()
    if not host:
        return value  # Legacy unscoped Codex bindings remain readable.
    for name in HOST_NAMES:
        if value.startswith(name + ":"):
            if name != host:
                raise ValueError("host_session_id belongs to a different host")
            return value
    if len(value) > 256:
        raise ValueError("Unscoped host_session_id exceeds 256 characters")
    return host + ":" + value


def validate_model(value: str | None) -> str | None:
    from .privacy import sanitize
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,199}", value)):
        raise ValueError("analysis_model must be a model identifier, not a command or credential")
    if value is not None and sanitize(value) != value:
        raise ValueError("analysis_model cannot contain a credential")
    return value


def analysis_selection(provider: str | None = None, model: str | None = None) -> tuple[str, str | None]:
    from .providers import canonical_provider
    configured = os.environ.get("BLUEPRINT_WORKFLOW_PROVIDER")
    selected = canonical_provider(provider or configured or current_host() or "codex")
    if model is None and (provider is None or (configured and selected == canonical_provider(configured))):
        model = os.environ.get("BLUEPRINT_WORKFLOW_MODEL") or None
    return selected, validate_model(model)
