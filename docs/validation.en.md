# Validation status

[繁體中文](validation.zh-Hant.md) · [English](validation.en.md) · [Product overview](../README.en.md)

**Product version: 0.3.0 · Validation date: 2026-11-10 · Primary environment: Windows, Python 3.12 and Codex CLI**

This page summarizes acceptance results for the development version. The application implementation, test suite and detailed acceptance records remain in the private maintenance environment.

## Completed checks

| Area | Result and scope |
| --- | --- |
| Program tests | 198 passed, covering capture, durable ingestion, drafts, sources, decisions, research, installation and compatibility. |
| Long-task simulation | 10,000 synthetic observations across fourteen simulated work periods, including restarts, deduplication and analysis catch-up. This was not fourteen continuous days of observed operation. |
| Background analysis | Real Codex CLI analysis produced incremental drafts, incorporated corrections and continued to finalization. |
| Fast finalization | A caught-up draft was finalized after result checks in approximately 3.2 seconds without another model request. This is an observation from that acceptance run. |
| Read-only research | Real search and page-opening activity was checked. Results citing pages without an observed open were rejected. |
| New-input execution | The current host executed a fixed workflow and read back actual results, including invalid-input and source-overwrite rejection checks. |
| Packaged process | A separately extracted package launched an actual MCP process; 30 tools and real tool calls were checked. |
| Legacy migration | Synthetic data created with the actual 0.2.0 distribution retained its original columns, records and identity, with a backup created. |
| Artifact versions | Explicitly registered snapshots survived changes to original files, and exported contents and hashes were checked. |

## Support by capability

| Capability | Current status |
| --- | --- |
| Four host adapters | Isolated adapters and stdio tool contracts were tested for Codex, Claude Code, Cursor and Gemini CLI. |
| Real Windows/Codex model analysis | Completed the acceptance checks above. |
| Real Claude Code/Gemini CLI authentication and analysis | Requires validation in the respective environments. |
| Native Cursor/Gemini application loading | Requires validation in the respective hosts. |
| Native hook capture | Ingestion and persistence are tested; enablement and trust must be checked in the host and installation environment. |
| Full execution in a separate new Codex process | That attempt was blocked by its shell-launch policy and retained as a failed record. New-input execution through the current host passed separately. |
| Native macOS/Linux environments | Native end-to-end acceptance has not been completed. |

## Understanding workflow states

- **Saved:** records are durable; background analysis may still be pending.
- **Caught up:** the background process has handled current records; actual outputs still require review.
- **Finalized:** a fixed method has been preserved and is ready for reuse testing.
- **Reuse verified:** the method was executed with the current inputs and the relevant results were checked. Agent-reported verification is identified as such.

Acceptance fixtures test product capabilities; they do not become defaults for other workflows. Each task's goals, tools, conditions and completion criteria are established by its actual work.

[Installation and activation →](installation.en.md)
