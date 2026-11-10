# Installation & Activation

[繁體中文](installation.zh-Hant.md) · [English](installation.en.md) · [Product overview](../README.en.md) · [Updates](UPDATES.md)

> **Applies to version 0.3.0.** Blueprint Workflow is open source under [Apache 2.0](../LICENSE). Source, installers and synthetic tests are in [`plugins/blueprint-workflow`](../plugins/blueprint-workflow). You can install from this repository without a Blueprint Studio subscription. Access and usage terms for the selected model service still depend on that provider.

## 1. Before you begin

Prepare the following:

- **Python 3.12 or later**, available from your terminal.
- An installed, usable target AI agent: Codex, Claude Code, Cursor or Gemini CLI.
- An installed and authenticated background analysis engine: Codex CLI, Claude Code or Gemini CLI.
- Git, and permission to write to a local private data directory.

The foreground agent works with you on the actual task. The background engine continuously develops the workflow. They can be selected separately. Cursor requires an explicitly selected supported background engine.

The plugin uses the selected CLI's existing login. It does not sign you in or copy account credentials. Access to the model service and its usage limits remain subject to the selected provider.

## 2. Get the source

Clone the public repository and enter the **plugin directory containing the `scripts` folder**:

```powershell
git clone https://github.com/collieai-g/Blueprint-Workflow.git
cd Blueprint-Workflow/plugins/blueprint-workflow
python --version
```

Run all installation commands below from `plugins/blueprint-workflow`. You can also use GitHub's source ZIP; enter the same plugin subdirectory after extraction.

## 3. Select one target agent

Run the line corresponding to your target agent.

| Target agent | Windows installation command |
| --- | --- |
| Codex | `& .\scripts\install-local.ps1 -Host codex` |
| Claude Code | `& .\scripts\install-local.ps1 -Host claude-code` |
| Cursor with a Codex background engine | `& .\scripts\install-local.ps1 -Host cursor -Provider codex` |
| Gemini CLI | `& .\scripts\install-local.ps1 -Host gemini-cli` |

`-Provider` accepts `codex`, `claude-code` or `gemini-cli`. The optional `-Model` selects a model identifier available to that engine. Install and authenticate the relevant CLI before selecting a different background engine.

The installer creates a separate private Python environment, installs the required dependencies and prepares the target host adapter. It does not silently switch to another model provider.

### Inspect a plan or prepare files first

```powershell
# Display the installation plan without writing installation settings
& .\scripts\install-local.ps1 -Host codex -DryRun

# Prepare adapter files without completing native registration
& .\scripts\install-local.ps1 -Host codex -StageOnly
```

Prepared files, completed registration, loaded tools and successful analysis are separate checks.

### macOS and Linux

The package also provides a shared command-line installation entry point, for example:

```sh
sh scripts/install-local.sh --host codex
sh scripts/install-local.sh --host cursor --provider codex
```

Native end-to-end acceptance must still be completed on the corresponding systems. An installation entry point does not establish that every platform has been tested.

## 4. Check the host and capture mode

| Host | Recording and activation in 0.3.0 |
| --- | --- |
| Codex | Native task-scoped hooks are provided. Review and trust them in **Settings → Hooks** or `/hooks`, then verify actual capture. |
| Claude Code | Native task hooks are provided, subject to host support, trust and permission requirements. |
| Cursor | The foreground AI saves semantic checkpoints through MCP. Automatic hooks are not enabled in this version. |
| Gemini CLI | The foreground AI saves semantic checkpoints through MCP. Automatic hooks are not enabled in this version. |

Inspect the local adapter from the package root:

```powershell
python scripts/launch.py doctor --host codex
```

Replace `codex` with your target host. This checks installation files and configuration without calling a model. It does not establish background authentication, complete capture or a successful replay.

Common statuses include:

- `staged`: adapter files are prepared; native registration is not complete.
- `installed_local`: local registration or adapter installation is complete; tool availability must still be checked inside the host.
- `analysis_configuration_required`: a supported background engine must be explicitly selected.
- `waiting_for_worker`: an existing analysis is still finishing; rerun setup after the background process stops gracefully.

Version 0.3.0 has completed Windows synthetic-data acceptance and real Codex background analysis and controlled research checks. Native loading, real authentication and model requests in other hosts require separate verification. Successful installation does not imply that every capability has passed acceptance.

## 5. Start your first workflow

In the original conversation with the plugin enabled, describe your actual goal and completion criteria:

> Use Blueprint Workflow to record this task and develop a reusable workflow as we work. The goal is … and the completion criteria are …

Continue working with your AI, supplying relevant information and corrections. Each workflow learns its method from that task and continues in the same original conversation. The background engine develops the draft incrementally. Meaningful improvements are presented at suitable checkpoints for your decision.

After completing the actual work and checking the deliverables, explicitly request finalization:

> This task is now 100% complete. Finalize the reusable workflow.

Finalization makes the method ready to test. A new-input execution must still be carried out and checked against its actual results before that replay can be recorded as passed.

## 6. Local data, updates and removal

The default Windows private data root is `%LOCALAPPDATA%\BlueprintWorkflowPlugin`. Recordings, drafts, workflow versions and exports are stored separately from the public source. The program runs locally; AI inference still uses your selected model service. Local storage does not mean offline inference. Provide data according to the actual task's authorization.

Account-installed plugins, Git marketplace installations and local installations have different update paths. Identify your source using [Updates](UPDATES.md); a GitHub version number alone does not establish that the installed copy has updated.

For an adapter installed from local source, obtain the chosen source version and run the same installation command from its plugin directory. The installer requests a graceful stop of the existing background process and creates a backup when existing data needs migration. Do not update by deleting the private data directory. After the host loads the new version, check the version, tools and recording behavior.

To remove one locally installed host adapter, run this command from the plugin source directory:

```powershell
python scripts/launch.py uninstall --host codex
```

Removing an adapter preserves private work records, versions and exports. Deleting private data is a separate operation.

---

[Product overview](../README.en.md) · [繁體中文安裝指南](installation.zh-Hant.md)
