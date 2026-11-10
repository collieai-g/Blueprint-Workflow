<p align="center">
  <a href="README.md">繁體中文</a> &nbsp; / &nbsp; <strong>English</strong>
</p>

<p align="center">
  <a href="https://blueprint-agent-studio.onrender.com/"><img src="assets/blueprint-studio.svg" alt="Blueprint Studio official website" width="196"></a>
</p>

<p align="center">
  <img src="assets/hero.en.svg" alt="Blueprint Workflow — Turn real work into reusable methods." width="100%">
</p>

<p align="center">
  <strong>Keep working with your AI. Build the method as you go.</strong><br>
  Capture key steps, preserve decisions and corrections, and develop work assets you can verify, deliver and reuse.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/PRODUCT-0.3.0-c9a45c?style=flat-square&amp;labelColor=0d0e10" alt="Product version 0.3.0">
  <img src="https://img.shields.io/badge/RECORDS-LOCAL-a3a5ab?style=flat-square&amp;labelColor=0d0e10" alt="Task records stored locally">
  <a href="LICENSE"><img src="https://img.shields.io/badge/LICENSE-Apache_2.0-e3d8be?style=flat-square&amp;labelColor=0d0e10" alt="Apache 2.0 open-source license"></a>
</p>

<p align="center">
  <a href="#product-value">Product value</a> ·
  <a href="docs/installation.en.md">Installation</a> ·
  <a href="docs/validation.en.md">Validation</a> ·
  <a href="plugins/blueprint-workflow">Source</a> ·
  <a href="docs/UPDATES.md">Updates</a> ·
  <a href="docs/service.en.md">Service plans</a> ·
  <a href="https://blueprint-agent-studio.onrender.com/">Official website ↗</a>
</p>

---

## Product value

**Blueprint Workflow** is an Apache 2.0 open-source workflow-learning plugin for existing AI agents. As you work, it develops a reusable method from your goals, instructions, observed results and corrections—preserving the experience that makes a process useful beyond a single task.

For professionals and teams, the asset includes both the final output and the method behind it. Blueprint Workflow connects how the work was completed with the decisions that shaped it, reducing the effort of reconstructing the process later.

| Traceable work | An evolving method | Human-directed improvement |
| :--- | :--- | :--- |
| Connect goals, decisions, results and corrections to actual evidence. | Maintain a workflow draft across multiple stages and work periods. | Review findings prompted by fundamental issues, recurring failures or evidence of a better approach. |

## How it works

<p align="center">
  <img src="assets/workflow-map.en.svg" alt="A continuous cycle of capture, understanding, structure, refinement and reuse" width="100%">
</p>

1. **Define the outcome and completion criteria** — Start a new workflow in your existing AI conversation.
2. **Continue directing the agent, checking outputs and making corrections** — Key steps are retained while the background process develops the method.
3. **Refine at meaningful moments** — Review evidence-based findings and alternatives. Confirmed changes become part of the working method.
4. **Finalize, deliver and verify reuse with new inputs** — Preserve a fixed version and related materials, with separate progress and result checks for each execution.

Each new task starts with its own requirements. Tools, routes, sequence and completion criteria come from that task's instructions and actual work. Workflow design continues in its original conversation and can recover after an application restart or context compaction.

## Preserve the method as a working asset

| Asset | What is retained |
| --- | --- |
| **A reusable method** | Steps, dependencies, variable inputs, outputs and completion criteria. |
| **Source-linked decisions** | Instructions, corrections, approvals, rejections, deferrals and their supporting references. |
| **Related artifact versions** | Explicitly registered input, reference and output versions, with hashes and source references. |
| **Checkable execution records** | Each run's progress, actual results, unresolved items and verification state. |

Task records are stored in a private local data directory; AI inference uses the selected engine. The foreground agent continues the work while background processing handles understanding, workflow creation and research when needed. Suggestions explain their basis and tradeoffs, and user decisions are retained.

## Work within your AI environment

Local adapters are provided for **Codex, Claude Code, Cursor and Gemini CLI**. The foreground host and background analysis engine are configured independently, using the selected CLI's existing login.

Cursor requires an explicitly selected supported background engine. Native authentication, hooks and research capabilities are distinguished by environment and actual validation. See [Installation](docs/installation.en.md) and [Validation](docs/validation.en.md).

### Start from source

Clone the repository and open PowerShell in the plugin directory. For Codex:

```powershell
git clone https://github.com/collieai-g/Blueprint-Workflow.git
cd Blueprint-Workflow/plugins/blueprint-workflow
python --version   # Python 3.12 or later is required
& .\scripts\install-local.ps1 -Host codex
```

After installation and activation, tell your agent:

> Use Blueprint Workflow to record this task and develop a reusable workflow as we work. The goal is … The completion criteria are …

Source, installers and synthetic tests are in [`plugins/blueprint-workflow`](plugins/blueprint-workflow). Read the [installation guide](docs/installation.en.md) for prerequisites and other hosts. Existing account plugins, local installations and Git marketplaces have different update paths: see [Updates](docs/UPDATES.md). A new GitHub commit does not establish that an installed copy has updated.

## For people who build working methods

Designed for professionals, operations teams, consultants and AI workflow designers who need to develop processes with AI, preserve specialist decisions and deliver reusable ways of working.

## Open source and optional services

The core software is available to use, modify and share under [Apache 2.0](LICENSE). Using the open-source version does not require a Blueprint Studio subscription. See [NOTICE](NOTICE) for branding and related notices.

Blueprint Studio is separately exploring optional installation assistance, compatibility maintenance and usage support. Plans, prices and service commitments will be announced separately. [Explore the service direction →](docs/service.en.md)

## Build it together

Report issues and suggest improvements through [Issues](https://github.com/collieai-g/Blueprint-Workflow/issues), or use the [contribution guide](CONTRIBUTING.md) to propose a change. Read the [security policy](SECURITY.md) before reporting a vulnerability, and keep private task records and credentials out of public reports.

From the plugin source directory:

```powershell
python -m pip install -e .
python -m unittest discover -s tests -q
```

## Development validation

| 198+ tests | 10,000 records | 30 tools |
| :---: | :---: | :---: |
| Passed for product version 0.3.0 | Synthetic records across fourteen simulated work periods | Verified MCP tool interfaces |

Real background-model analysis, read-only research, new-input execution through the current host and a separately extracted package process were also exercised. Test conditions, verified capabilities and outstanding checks are documented in [Validation](docs/validation.en.md).

---

<p align="center">
  <strong>Blueprint Workflow · 藍圖工作流</strong><br>
  Developed by <a href="https://blueprint-agent-studio.onrender.com/">Blueprint Studio · 藍圖工作室</a><br>
  <a href="README.md">繁體中文</a> · <a href="docs/installation.en.md">Installation</a> · <a href="CONTRIBUTING.md">Contribute</a> · <a href="LICENSE">Apache 2.0</a>
</p>
