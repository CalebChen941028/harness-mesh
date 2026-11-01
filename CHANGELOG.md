# Changelog

## 0.3.1 — 2026-11-01

Security hardening release addressing the independent audit.

### Added

- `allowed_workspace_roots` config, symlink-resolved containment checks for dispatch/apply, and refusal to start the MCP/CLI service from `$HOME`, `/`, or a non-Git directory.
- Sensitive-file exclusion in `.git/info/exclude` and in dirty-sync/copy paths; `auto_init_repo` now defaults to false.
- `auto_approve_shared_write` config, default false; `shared_write` no longer inherits approval bypass implicitly.
- `hmesh doctor` checks registered command paths and `python -m harness_mesh.mcp_server --help`.
- `hmesh install --yes` for non-interactive use; without it, a non-interactive install is refused after printing the plan.
- Dependabot configuration for pip and GitHub Actions, plus a non-blocking `pip-audit` CI step.

### Changed

- Workspace roots are validated before any directory creation; active task state is rejected when the root escapes the allowlist.
- Baselines and worktrees skip `.env`, key material, credential files and similar names; untracked symlinks are recreated only when their target stays inside the workspace.
- MCP call depth uses `max(argument, inherited)` and trace chains are merged, so a worker cannot reset recursion state.
- DeepSeek Harness uses `workspace-write` when `auto_approve` is false; built-in shell commands drop credential-shaped environment variables.
- Mesh Git commands disable repository hooks; `.git/config` and `.git/hooks` hashes are compared before apply.
- Shared memory keys and content are capped, authors must be configured harness names or `controller`, delimiters are escaped, and auto-recorded summaries move to a separate unverified block.
- MCP `apply_task_patch` supports only `patch` and `merge`; CLI copy requires `--force-copy`. Apply requires a completed ticket and runs a patch preflight.
- Installer subprocesses use `stdin=DEVNULL`, restore the previous registration when add fails, write DSH patches atomically with rollback, and use a consistent Claude `user` scope.
- Dependency upper bounds added for pydantic, PyYAML, httpx and Typer; MCP module documentation corrected.

## 0.3.0 — 2026-11-01

### Added

- `hmesh install` one-command installer for all supported desktop harnesses.
- Claude Code, Codex and Antigravity MCP registration through their native `mcp add` commands.
- ZCode plugin/marketplace generation with project-scope CLI installation and an offline UI-ready fallback.
- DeepSeek Harness `@deepseek-ai/dsh-mcp-client` Cordis patch entries for desktop/headless profiles with backups.
- Idempotent install/uninstall, `--dry-run`, `--harness` filters and JSON output.
- Installer regression tests covering command generation, ZCode manifests, DSH YAML editing, backups and dry-run.

## 0.2.0 — 2026-11-01

Second-phase iteration focused on real macOS harness interoperability and shared project context.

### Added

- Binary discovery through `binary_paths` with `~`/glob expansion, newest-version selection, injected-`which` isolation for tests, and an automatic ZCode Electron/CJS wrapper under `.harness-mesh/bin/zcode`.
- Real CLI protocols for Claude Code, Codex, DeepSeek Harness, Antigravity and ZCode, including approval flags, session resume, and `stdin=DEVNULL`.
- DeepSeek headless credential/model overlay generation with cleanup, NDJSON event parsing, `DSH_PERMISSION_MODE`/`DSH_TOOLS_MODE`, and `/v1` endpoint normalisation.
- Claude model-catalog parsing with context windows, reasoning flags, active model, subscription-credit penalties and pure-subscription tier bindings.
- Codex `models_cache.json` `slug` discovery; ZCode v2 `personalModelIds`/`modelOrder` discovery; Antigravity spinner-frame cleaning.
- `sync_uncommitted` dirty-workspace replication into isolated worktrees with a baseline commit and net-diff capture.
- `ProjectContextManager`, the `project_context_entries` SQLite table, instruction-file bridging, shared memory, context-file previews and `auto_record_context`.
- `depends_on` dependency chains with upstream output injection and pre-applied diffs.
- MCP tools `get_project_context` and `update_project_context` (13 tools total) and `hmesh context list|set`.
- Real-machine integration tests for binary resolution, full inventories and a real DeepSeek ticket.
- English and Chinese product documentation, security/contribution/conduct policies, MIT licence and CI workflow.

### Changed

- `TaskRequest` gains `depends_on`, `sync_uncommitted`, `include_project_context`, `auto_record_context`; `session_id` is passed through MCP and all adapters.
- Capability scoring understands newer model families and enforces Claude subscription tier expectations.
- `merge` conflicts now raise instead of silently copying files over the primary workspace.

## 0.1.0 — 2026-10-31

Initial implementation: Ticket/Assignment model, dynamic model discovery, three-state quota circuits, Git worktree isolation, skills/plugins, SQLite ledger, 11 MCP tools and the `hmesh` CLI.
