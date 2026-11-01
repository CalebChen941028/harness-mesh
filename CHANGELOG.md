# Changelog

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
