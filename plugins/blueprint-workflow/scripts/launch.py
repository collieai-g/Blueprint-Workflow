"""Portable plugin launcher; no dependency installation happens during a tool call."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from blueprint_workflow.paths import default_data_dir


def main() -> int:
    settings_path = ROOT / "local-settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig")) if settings_path.is_file() else {}
    if settings.get("data_dir"):
        os.environ.setdefault("BLUEPRINT_WORKFLOW_DATA_DIR", settings["data_dir"])
    for key, name in (("host", "HOST"), ("analysis_provider", "PROVIDER"), ("analysis_model", "MODEL")):
        if settings.get(key):
            os.environ.setdefault("BLUEPRINT_WORKFLOW_" + name, str(settings[key]))
    runtime_root = default_data_dir() / "runtime"
    candidate = Path(os.environ.get("BLUEPRINT_WORKFLOW_PYTHON") or settings.get("runtime_python") or (runtime_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    if candidate.exists() and candidate.resolve() != Path(sys.executable).resolve():
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"
        # A connected MCP host owns stdio. Its pipe handles must remain attached.
        return subprocess.call([str(candidate), "-m", "blueprint_workflow", *sys.argv[1:]], env=env,
                               stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    from blueprint_workflow.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
