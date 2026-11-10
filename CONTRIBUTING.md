# Contributing to Blueprint Workflow

Thank you for helping people turn real work into reusable methods. Contributions
should keep the core domain-neutral: goals, tools, routes and completion criteria
come from the current task and its evidence.

## Get started

1. Fork this repository and create a branch for your change.
2. Work in `plugins/blueprint-workflow` using Python 3.12 or later.
3. Install the project with `python -m pip install -e .` in a virtual environment.
4. Run `python -m unittest discover -s tests -q`.
5. Open a pull request explaining the problem, resulting behavior and validation.

Use synthetic fixtures and temporary directories. Do not commit personal workflow
records, customer files, authentication details, runtime databases, local settings
or private acceptance receipts. Opt-in live tests require a separately configured
provider and are not part of normal CI.

Keep recording independent from analysis failures. Preserve task isolation,
accepted-event durability, evidence references and user decisions. Changes to
storage or updates must explain migration, compatibility and recovery.

Contributions are provided under Apache-2.0, the license of this project. Brand
names and logos identify the publisher; the license does not grant trademark rights.

Product questions and feature requests belong in
[GitHub Issues](https://github.com/collieai-g/Blueprint-Workflow/issues).
For security reports, follow [SECURITY.md](SECURITY.md).
