# Changelog

## 0.3.1 — 2026-11-01

Security hardening release addressing two independent audits.

### Added

- `allowed_workspace_roots` with symlink-resolved containment checks, plus service-start validation that rejects `$HOME`, `/`, and, unless `auto_init_repo` is enabled, non-Git directories.
- `auto_init_repo` (default false), `auto_approve_shared_write` (default false) and `max_active_assignments` (default 8).
- `skipped_sensitive` and `warnings` on change/apply/task results, so filtered files are visible to CLI and MCP clients.
- Context provenance: a `source` column (`controller`, `worker`, `auto`) with an in-place migration for existing databases.
- `hmesh doctor` checks registered command paths and `python -m harness_mesh.mcp_server --help`; `hmesh install --yes` supports non-interactive installs after printing the plan.
- Dependabot for pip and GitHub Actions, plus a non-blocking `pip-audit` CI step.

### Changed

- Sensitive filtering uses narrowed filename rules plus Git pathspec excludes; `.env.example`/`.env.sample`/`.env.template`/`.env.dist` and ordinary `credentials*.py` source files remain visible. `.ssh`, `.aws`, `.gnupg`, and `.git` path segments are always treated as sensitive. Mesh no longer writes sensitive patterns into the user's `.git/info/exclude`.
- Worktree symlinks are rewritten as relative links pointing inside the destination worktree. Tracked absolute links are normalised, escaping/dangling links are reported as warnings, and apply rejects symlink diffs that leave the target root.
- Git metadata guards are recorded once per task, survive worktree reuse, include `.git/info/attributes`, and abort the ticket with `git_metadata_tampered` before any further assignment. Mesh Git commands disable hooks, fsmonitor, pager, external diff and protocol.ext.
- Recursion state can be recovered from the worktree cwd/lineage marker when MCP cannot rely on inherited environment variables; active assignments are globally capped.
- DeepSeek Harness uses `workspace-write` when `auto_approve` is false. `DSH_PERMISSION_MODE` accepts `read-only`, `workspace-write`, and `danger-full-access` (verified from the installed DSH help/package strings). The built-in shell drops more credential-shaped variables, exact cloud keys, SSH agent sockets, and URLs containing passwords.
- Workspace validation is applied consistently by the CLI, MCP runtime, installer plan, and doctor checks; missing Git is reported as an actionable error.
- DSH patches are validated before writing, written atomically, and rolled back to the exact pre-write content on failure.
- Context entries now carry provenance; only `source=controller` entries render as verified memory. Worker and auto entries render in `<project_unverified_memory>`. Authors are checked for a configured harness name or `controller` only; that check does not prove provenance. Delimiter escaping is case-insensitive and whitespace-tolerant.
- MCP `apply_task_patch` supports only `patch` and `merge`; CLI copy requires `--force-copy`. Apply requires a completed ticket and runs a patch preflight.
- Installer subprocesses use `stdin=DEVNULL`, restore the previous registration when add fails, use a consistent Claude `user` scope, and confirm non-interactive changes with `--yes`.
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
