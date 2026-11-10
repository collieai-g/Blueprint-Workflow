"""Private runtime locations, kept outside the distributable plugin."""
from __future__ import annotations

import os
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    override = os.environ.get("BLUEPRINT_WORKFLOW_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "BlueprintWorkflowPlugin"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "blueprint-workflow-plugin"


def private_data_dir(value: str | Path | None = None) -> Path:
    path = Path(value or default_data_dir()).expanduser().resolve()
    if path == PLUGIN_ROOT or PLUGIN_ROOT in path.parents:
        raise ValueError("Runtime data must be outside the plugin source or installation directory")
    return path
