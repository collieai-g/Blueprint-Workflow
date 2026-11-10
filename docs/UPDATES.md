# Updating Blueprint Workflow

Blueprint Workflow separates the plugin shown by the host, the local runtime that records and analyses work, and the private work data. A new GitHub commit or an uploaded plugin release is not proof that an existing chat is running the new runtime.

This document describes the supported update routes and a proposed automatic-update design. The release does **not** contain an unattended runtime updater or promise to replace running tools inside an active chat.

## Identify the installed source

In Codex, inspect the installed entry before updating:

```powershell
codex plugin list --json
codex plugin marketplace list --json
```

Match the plugin name, marketplace, installed version and source. These are separate distribution routes:

| Route | What owns the update | What must be preserved |
| --- | --- | --- |
| Account-uploaded plugin | A new release of the **same backend plugin ID** | Existing account/workspace scope, audience, prompts, connection settings and plugin identity |
| Managed local installation | The source release and `scripts/install-local.ps1` | Existing data root, native marketplace identity, provider/model selection, Python environment and installation receipt |
| GitHub marketplace | The marketplace source and the host's refresh/install flow | Stable marketplace and plugin names, reviewed release ref, private data location and host permissions |

An existing `blueprint-workflow@blueprint-workflow-local` installation is not the same identity as an account-uploaded entry or a future `blueprint-workflow@blueprint-studio` entry. Adding a second source does not upgrade the first one. Avoid enabling two recorders against the same work. Migrate the existing route deliberately, verify the replacement, and retain the original until recovery has been checked.

## 1. Update an account-uploaded plugin

Publish the updated manifest, English presentation, brand assets and implementation through the existing plugin's eligible update workflow. Keep its backend ID rather than creating a duplicate. Read back the saved release and affected files afterwards.

Saving a release and refreshing an installed host are separate checks. Reopen the plugin details and verify its version and presentation. If a host only loads bundled skills and tools when a chat starts or the application restarts, resume the original work chat after that supported refresh. Record the active runtime version separately from the listing version.

A local stdio MCP server still needs its local runtime and dependencies. Uploading an archive does not deploy that process to a web or mobile host. Where the installed package is a thin wrapper pointing at a fixed core snapshot, updating its listing alone does not update that snapshot.

## 2. Update a managed local installation

Use the new, reviewed source release in the plugin source directory. Preserve the **existing** private data root and explicit provider/model settings. The following is an operator-assisted route, not an unattended update:

```powershell
# Inspect first; replace this with the existing installation's data root.
$WorkflowData = Join-Path $env:LOCALAPPDATA 'BlueprintWorkflowPlugin'
python -X utf8 scripts/launch.py --data-dir $WorkflowData install-plan --host codex
```

Before applying, pause work that can invoke hooks or MCP writes, wait for in-flight calls to finish, stop the old background worker, and make a recoverable backup of the complete private data directory. A SQLite-only backup does not include task files, inbox receipts, artifact versions or exports. Save the old release and its installation receipt as well. Do not copy provider authentication stores into the package or repository.

Run the installer with the same data root and any explicitly selected provider/model:

```powershell
& .\scripts\install-local.ps1 -Host codex -DataDirectory $WorkflowData
python -X utf8 scripts/launch.py --data-dir $WorkflowData doctor --host codex
```

If an existing installation explicitly selected a background provider or model, pass those same `-Provider` / `-Model` values; do not allow an update to silently substitute defaults. `-StageOnly` prepares files, but is not a guarantee of a completely read-only operation. Use `install-plan` or `-DryRun` for inspection.

Restart the desktop host when required, resume the original chat and verify tool discovery, the active version, the same task binding, draft readability, pending records and provider selection. Run a harmless recording/read-back check before resuming ordinary work. Do not call a preserved workflow completed merely to upgrade the software.

### Protection already present in the installer

The implementation creates version-and-content-addressed core snapshots, preserves its local marketplace identity, serializes installation against worker launches, asks the old worker to stop, and restores managed wrapper/catalog files if native registration fails. Data migrations use schema locks and take a database backup when applicable. Credentials are not read, copied or rewritten by the installer.

### Protection still required for unattended upgrades

The current installer does not provide a complete upgrade transaction across active hooks, existing MCP servers, task files, dependency changes and host activation. In particular, stopping the analysis worker does not fence every writer or drain every accepted capture receipt. Its wrapper rollback is not a complete rollback of the database, Python dependencies or host state. An automatic updater must close these gaps before it can apply an update without an operator.

## 3. Follow a GitHub marketplace

The intended public catalog is `blueprint-studio`; the plugin name remains `blueprint-workflow`. Once that catalog exists in the public repository, the host-supported commands are:

```powershell
codex plugin marketplace add collieai-g/Blueprint-Workflow --ref stable --json
codex plugin marketplace list --json
codex plugin add blueprint-workflow@blueprint-studio --json
```

For an existing installation from that exact marketplace:

```powershell
codex plugin marketplace upgrade blueprint-studio --json
codex plugin add blueprint-workflow@blueprint-studio --json
codex plugin list --json
```

Inspect the returned marketplace refresh errors and installed version, then restart or reload through the host's supported flow. Do not remove a working entry as the first update step. Confirm the installed CLI's `--help` when a command is unavailable.

OpenAI documents marketplace refresh and the possibility of refreshing configured plugin files. It also documents restarting the desktop app after changing a local source. Those facilities do not establish a guaranteed polling interval, universal account-plugin automatic update, or live replacement of a running MCP process. A marketplace pointing at a fixed SHA or tag intentionally stays on that selected release until its selector changes. [Official packaging and marketplace documentation](https://developers.openai.com/plugins/build/plugins), [official Codex command reference](https://learn.chatgpt.com/docs/developer-commands).

## Proposed automatic runtime update design

The following is a development design, not a capability shipped in this release.

### A. One release authority

Publish a release manifest with the plugin version, immutable commit, archive name, SHA-256, supported Python/host requirements, data schema range, migration identifier and minimum updater version. Tie it to a reviewed release and a verifiable signature/provenance policy. A checksum delivered by the same untrusted source is not independent publisher authentication. Release preparation must fail if runtime, manifest, compatibility overlay, catalog and release metadata disagree.

Keep distribution identity stable. Use a reviewed stable channel for unattended activation; development branches can be offered as explicit preview channels. Never execute paths, commands or service changes supplied by arbitrary workflow records.

### B. Check and stage without interrupting work

Perform a bounded, rate-limited version check at startup or an idle checkpoint. Cache the result and honour retry backoff; a network failure must not stop recording, analysis or reuse. Record `available`, `downloaded`, `verified` and `staged` states with an operation ID, target version and digest. Merge duplicate checks for the same target.

Download into a separate staging directory, verify publisher/release identity and file hashes, reject unsafe archive paths and links, inspect permission changes, and prepare dependencies in a new versioned environment. Do not mutate the active Python environment while another process uses it. Test staging with isolated synthetic data; do not silently call the user's model or upload their records for an update check.

### C. Apply only at a safe checkpoint

Use one durable update lock shared by installer, launcher, hooks, worker and MCP mutations. An active operation stays on its pinned release until it completes. When activation begins:

1. Stop new mutations from the old runtime and prevent new worker launches. A hook can acknowledge only after a durable, compatible receipt has been saved; otherwise return a retriable failure without a success receipt.
2. Wait for existing calls to finish. Drain all acknowledged inbox entries under the appropriate capture boundaries and record the resulting event positions. Preserve unresolved receipts and explicit gaps; never delete an inbox to make an update appear complete.
3. Stop analysis/research workers and establish quiescence for every writer using that data root. If a process cannot stop safely, retain `waiting_for_safe_point` and keep the old release active.
4. Take a complete, verified backup of the private data set plus a database-consistent snapshot. Store a file/hash index, schema version, task bindings, effective configuration and old activation pointer. Exclude authentication stores from collected diagnostics.
5. Run the migration with an exclusive schema lock and a recovery journal. Preserve session IDs, decisions, immutable versions, task files, acknowledged receipts, research status and original-chat bindings.
6. Atomically switch the activation pointer to the staged release and versioned environment. Replace only files owned by Blueprint Workflow; preserve host settings, unrelated plugins and provider authentication.
7. Start the runtime and verify version, tool discovery, schema, inbox reconciliation, task/draft readability and recording/read-back. Release the mutation fence only when checks pass.

If host permissions have materially expanded, require the host's normal review. If the host cannot reload the package during the existing chat, report `restart_required` and wait for a supported restart; retain the saved work for resumption in that same chat.

### D. Recover without losing new work

Before new writes are accepted, a failed activation can restore the old pointer, environment, catalog/wrapper and matching backup as one recovery operation. Keep the failed candidate and diagnostics separate for investigation. Never run the old runtime against a newer schema unless that exact combination is declared and tested compatible.

After new writes have been accepted, do not blindly replace live data with a pre-update backup. Prefer a compatible forward repair; otherwise hold the writes in durable storage and use a tested replay/recovery procedure. Retain old releases and backups until the new release passes acceptance, then remove only verified unreferenced managed artifacts under an explicit retention policy.

### E. Report through the existing conversation

An update does not require a separate mandatory interface. Persist a small update notice and present it at the next suitable activity in the original chat. Distinguish `available`, `staged`, `waiting_for_safe_point`, `applying`, `restart_required`, `verified_active`, `failed` and `rolled_back`. A notification being read is separate from it being shown.

Offer `notify_only` and an explicitly enabled `auto_apply_at_idle` policy, with a preview channel opt-in. The updater cannot manufacture an update button in the host's plugin details. Where verified host background refresh is available, reuse it; otherwise the local update coordinator owns the check/stage/apply transaction and reports its actual state.

## Acceptance before enabling automatic application

- Upgrade a populated synthetic 0.2 data set to 0.3, preserving IDs, decisions, drafts, exports and task bindings; reopen the original task after restart.
- Crash after each journal transition, including after a capture receipt, during inbox drain, migration, pointer switch and first startup; recover every acknowledged record once.
- Test concurrent hooks, MCP calls, analysis and research completion while an update is staged or applied. Stale processes must not write through the mutation fence.
- Reject wrong hashes, wrong publisher/source, unsafe archives, incompatible schemas, missing dependencies and unexpected permission changes without altering the working release.
- Test host refresh and restart separately from package publication; check which runtime is actually serving tools.
- Prove rollback before accepting new writes and forward recovery after accepting them. Compare task/artifact file hashes as well as database contents.
- Simulate failed release checks and unavailable GitHub; existing work must continue under the active release.

The completion claim must name what was verified: source published, package released, host entry refreshed, runtime activated, data migrated and preserved, or unattended update tested. These are separate milestones.
