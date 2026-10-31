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
- Add or update tests for the behaviour you change. Real-CLI tests belong in `tests/test_local_machine_harnesses.py` and must skip on unsupported hosts.

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
