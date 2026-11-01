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
- Assignment rows now store the lineage handed to the adapter (`call_depth`, `trace_chain`) and the pid of the spawned harness process. MCP resolves a nested server's lineage from the process ancestor chain, then from a cwd under the task worktree, and combines it with the environment by maximum/union. Recursion stops at the first `codex -> codex` cycle even when the host drops `HARNESS_MESH_*`.
- `hmesh dispatch` resolves the same lineage through the shared `harness_mesh.lineage` module. Its effective depth is the maximum of `--call-depth`, `HARNESS_MESH_CALL_DEPTH` and the running Assignment found in the ledger or the process ancestor chain, and the trace chain is the ordered union of the three, so a worker cannot clear its depth by calling the CLI from a shell.
- An Assignment opened by `_dispatch` is closed as `ended_error` with `error_reason=cancelled` or `dispatch_exception` when the dispatch is cancelled or raises before the normal per-assignment update, so the row no longer holds a `max_active_assignments` slot with the dispatcher pid.
- `.harness-mesh-lineage.json` is only accepted when the ledger records the ticket as running with a worktree containing cwd; an agent that rewrites the marker cannot override the worktree-derived task.
- `max_active_assignments` is counted from running rows with live pids in the shared `state.db`; rows left behind by a dead process are closed as `ended_error` with `error_reason=stale`.
- `tests/test_real_machine_mcp_env.py` and a reusable `tests/fixtures/mcp_probe_server.py` verify environment propagation and worktree cwd for the real harness CLIs. Real-machine suites now require `HARNESS_MESH_REAL_MACHINE=1`; global-configuration tests (ZCode plugin marketplace, Antigravity MCP registration) also require `HARNESS_MESH_REAL_MACHINE_GLOBAL=1` and restore the previous state.

### Changed

- Sensitive filtering uses narrowed filename rules plus Git pathspec excludes; `.env.example`/`.env.sample`/`.env.template`/`.env.dist` and ordinary `credentials*.py` source files remain visible. `.ssh`, `.aws`, `.gnupg`, and `.git` path segments are always treated as sensitive. Mesh no longer writes sensitive patterns into the user's `.git/info/exclude`.
- Worktree symlinks are rewritten as relative links pointing inside the destination worktree. Tracked absolute links are normalised, escaping/dangling links are reported as warnings, and apply rejects symlink diffs that leave the target root.
- Git metadata guards are recorded once per task, survive worktree reuse, include `.git/info/attributes`, and abort the ticket with `git_metadata_tampered` before any further assignment. Mesh Git commands disable hooks, fsmonitor, pager, external diff and protocol.ext.
- Recursion state is recovered from the recorded assignment lineage, the process ancestor chain and the worktree cwd when MCP cannot rely on inherited environment variables; active assignments are capped through the shared ledger.
- DeepSeek Harness uses `workspace-write` when `auto_approve` is false. `DSH_PERMISSION_MODE` accepts `read-only`, `workspace-write`, and `danger-full-access` (verified from the installed DSH help/package strings). The built-in shell drops more credential-shaped variables, exact cloud keys, SSH agent sockets, and URLs containing passwords.
- Workspace validation is applied consistently by the CLI, MCP runtime, installer plan, and doctor checks; missing Git is reported as an actionable error.
- DSH patches are validated before writing, written atomically, and rolled back to the exact pre-write content on failure.
- Context entries now carry provenance; only `source=controller` entries render as verified memory. Worker and auto entries render in `<project_unverified_memory>`. Authors are checked for a configured harness name or `controller` only; that check does not prove provenance. Delimiter escaping is case-insensitive and whitespace-tolerant.
- MCP `apply_task_patch` supports only `patch` and `merge`; CLI copy requires `--force-copy`. Apply requires a completed ticket and runs a patch preflight.
- The MCP server derives its default workspace from a cwd under `.harness-mesh/worktrees/<task_id>` (the project root), so a host that launches it inside a worktree without `HARNESS_MESH_WORKSPACE` still opens the project ledger.
- `pytest -q` no longer launches real harness CLIs on machines that happen to have the five applications installed. README, README.zh-CN and CONTRIBUTING document the two opt-in variables.
- SECURITY.md records the measured Codex behaviour (codex-cli 0.166.0-alpha.5.1, 2026-11-01: no host environment in MCP children) and the ancestry-based recursion guard. Both READMEs note that ZCode `--scope project` writes global plugin state which `hmesh install --uninstall` removes.
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
