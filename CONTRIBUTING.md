# Contributing to Harness-Mesh

Thanks for helping improve cross-harness orchestration. This document keeps contributions predictable for maintainers and reviewers.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -v
.venv/bin/ruff check src tests
```

Python 3.11+ is required. macOS is the primary test platform because the real harness integration suite exercises locally installed desktop applications. Linux and WSL work with CLI binaries available on `PATH`; real-machine tests skip when the macOS bundles are absent.

## Real-machine tests

`pytest -q` never launches a vendor CLI. The real-machine suites are opt-in and consume provider quota (one short prompt per harness):

```bash
HARNESS_MESH_REAL_MACHINE=1 .venv/bin/pytest -q tests/test_local_machine_harnesses.py tests/test_real_machine_mcp_env.py
```

Without the variable both files are skipped, so a developer laptop and CI produce the same default run. The MCP environment probe expects Claude Code, Codex, DeepSeek Harness, Antigravity and ZCode to be installed; missing bundles skip the suite.

Tests that write a harness' global configuration additionally require `HARNESS_MESH_REAL_MACHINE_GLOBAL=1`. That covers ZCode (plugin marketplace entries in `~/.zcode/cli/plugins/`) and Antigravity (`~/.gemini/config/mcp_config.json`). These tests restore the previous state in a `finally` block and fail if the restore fails; only enable the switch on a machine whose harness configuration you own.

## What to contribute

Useful contributions include:

- a new `BaseHarnessAdapter` for another coding harness;
- a new discovery layer for a harness configuration format;
- corrections to existing CLI templates after a vendor CLI change;
- shared-context and dependency-pipeline improvements;
- documentation, translations, and reproducible bug reports.

For a large behavioural change, open an issue or design note first so routing and security implications can be reviewed before implementation.

## Coding rules

- Keep harness-specific command knowledge in `config/harnesses.example.yaml` and the adapter's `_augment_command`; do not hard-code a static model catalogue.
- Preserve the `Ticket` / `Assignment` split. A ticket owns a worktree; assignments are single attempts.
- All output returned to a controller must stay bounded. Store full diffs/transcripts as resources and expose them through `read_task_resource`.
- Never write credentials into SQLite, logs, prompts, or generated config files.
- Treat failover, cancellation, and timeout paths as first-class: child processes must be killed and worktrees must remain inspectable.
- Installer edits stay scoped: use the managed `mcp__harness-mesh__` / `mcp(harness-mesh/` prefixes or the Codex marker block, back up before the first write, write through a temporary file, validate by re-parsing, and restore the previous content on failure. Cover fresh install, unrelated existing settings, idempotency, level changes, `none`/uninstall cleanup, validation failure and `--dry-run` with a temporary `HOME`.
- Add or update tests for the behaviour you change. Real-CLI tests belong in `tests/test_local_machine_harnesses.py` or `tests/test_real_machine_mcp_env.py`, must stay behind `HARNESS_MESH_REAL_MACHINE=1`, and must skip on unsupported hosts.

## Tests and review

Before opening a pull request:

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src tests
.venv/bin/pytest -v
```

The suite includes fast mocked tests and an optional real-machine suite. If your change touches CLI protocols, include at least one mocked regression test and note which real harness you smoke-tested. Reviewers check routing safety, state cleanup, output bounds, and whether a claimed capability is actually wired to an adapter.

## Commit and PR conventions

- Use focused commits with an imperative subject, for example `fix(deepseek): restore headless credentials overlay`.
- Describe the problem, the chosen behaviour, and how it was verified.
- Do not include `.harness-mesh/`, credentials, personal home-directory paths in tests, or generated caches.
- By contributing, you agree that your contribution is licensed under the MIT License in this repository.
