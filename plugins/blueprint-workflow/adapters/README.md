# Local host adapters

The common installer generates each host's native metadata and a small launch
wrapper. Every wrapper uses one versioned shared core and the same private
Python runtime. Recordings, workflow versions and exports stay outside source
and plugin caches. Each host/provider/model selection is explicit; another host
does not silently switch its analysis to Codex.

## One installation entry point

Run these commands from the release/source directory with Python 3.12+. Global
`--data-dir` goes **before** the subcommand. `install-plan` and `doctor` do not
call a model or register/change a host.

```powershell
python scripts/launch.py install-plan --host claude-code
python scripts/launch.py install --host claude-code
python scripts/launch.py doctor --host claude-code
python scripts/launch.py uninstall --host claude-code
```

Choose another host with `--host codex`, `--host cursor` or `--host gemini-cli`.
Cursor requires an explicit supported background engine, for example
`--provider claude-code`, `--provider gemini-cli` or `--provider codex`. Its own
CLI background isolation is not yet verified. An engine must be installed and
authenticated on the user's computer; installation does not sign in or copy
credentials. `--model <identifier>` optionally chooses that engine's model.

To select a different private data domain:

```powershell
python scripts/launch.py --data-dir C:/Private/BlueprintWorkflow install --host codex
```

Windows users may use `scripts/install-local.ps1 -Host claude-code`. On macOS
or Linux use `sh scripts/install-local.sh --host claude-code`; set
`BLUEPRINT_WORKFLOW_SETUP_PYTHON` if the Python command is not `python3`, and
`BLUEPRINT_WORKFLOW_DATA_DIR` for a non-default private data location. These
wrappers call the same installer. They do not download a replacement CLI.

`install --dry-run` prints the plan without writing. `--stage-only` prepares an
adapter without registering it or copying it into Cursor's native local-import
directory. `--skip-dependencies --runtime-python <existing-python>` is intended
for an already prepared MCP runtime. Normal setup creates a private virtual
environment and installs the pinned MCP SDK there, not into global Python.

## Native routes and status

| Host | Adapter route | Automatic capture |
| --- | --- | --- |
| Codex | Dedicated local marketplace; existing `blueprint-workflow@blueprint-workflow-local` identity retained | Task-scoped hooks after native trust review |
| Claude Code | Native `.claude-plugin/plugin.json` + `.mcp.json`; dedicated local marketplace | Native task hooks, subject to host support/trust |
| Cursor | Portable plugin imported under `~/.cursor/plugins/local/blueprint-workflow` | Semantic MCP checkpoints; automatic hooks not enabled in this release |
| Gemini CLI | `gemini-extension.json` + `GEMINI.md`; native `gemini extensions install <path>` | Semantic MCP checkpoints; automatic hooks not enabled in this release |

- `staged`: files are prepared; native registration has not completed. A missing
  host executable leaves registration pending and is not a successful live test.
- `installed_local`: native registration succeeded, or Cursor's local-import
  files were installed. `native_verified` remains false until actual tool
  discovery, recording and analysis are tested in that host.
- `analysis_configuration_required`: Cursor needs an explicit supported engine.
- `waiting_for_worker`: an existing analysis is finishing; retry setup after the
  worker stops. The installer does not kill the current inference mid-request.

Native review/consent remains with the host. Codex users review hooks through
Settings → Hooks or `/hooks`. The Gemini installer does not pass `--consent` or
other approval-bypass flags. Organization policy can prevent local imports or
extension installation. No custom product interface is required.

## Updating and uninstalling

Run the same install command from the new release. The installer asks the
existing worker to stop gracefully before switching native configuration or
updating the shared runtime. Core snapshots are versioned and never overwrite
the prior installed core. Existing recordings/exports remain in place.

`uninstall --host <host>` removes only this adapter's native entry and unchanged
managed wrapper files. Other plugins/settings and modified user files are
preserved. Shared core/runtime, SQLite records and exports remain available to
other installed hosts. Deleting private data is a separate explicit operation.

Installers restore their own wrapper/catalog files if registration fails. They
never back up or overwrite whole account settings/authentication stores. A
failed native command is reported and must be checked before claiming the host
is usable. Public-directory publication is a separate future delivery step.
