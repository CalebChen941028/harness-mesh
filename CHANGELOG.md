# Changelog

## 0.4.2 — 2026-11-02

Security hardening release following an independent audit of 0.4.1. No new harness or model features; behaviour changes are security fixes.

### Security — critical

- Workspace configuration is untrusted. `find_config_path` no longer discovers `{workspace}/.harness-mesh/config.yaml` or `{workspace}/config/harnesses.yaml`; only `HARNESS_MESH_CONFIG`, `$XDG_CONFIG_HOME/harness-mesh/config.yaml`, `~/.config/harness-mesh/config.yaml` and `~/.harness-mesh/config.yaml` are trusted, with the packaged example as a read-only fallback. A malicious checkout could previously point a harness `binary`, `version_args` or `execute_template` at its own payload and execute it from `hmesh status` or from pre-approved MCP read tools such as `list_harnesses`. `HARNESS_MESH_TRUST_WORKSPACE_CONFIG=1` re-enables only a safe field list (market, routing, sizes, quotas) and never the executable, credential or path fields. `hmesh install` and `hmesh doctor` no longer read workspace configuration at all. Migration: teams that kept `binary_paths`, `execute_template` or harness overrides in a committed `config/harnesses.yaml` must move them to `~/.config/harness-mesh/config.yaml` (or pass `--config`); harness CLIs installed on `PATH` need no configuration at all, and the built-in macOS bundle paths remain fallbacks.
- Credentials never enter the ledger or tool responses. `DiscoveredModel.provider` is now a short enum-shaped label; a provider registry stored as a dict, a long value, or a credential-shaped string falls back to the harness name. `discover_harness_models` projects every model through `public_view()`, and `list_skills_and_plugins` redacts MCP server `env` values. `StateStore` rewrites legacy credential-shaped `provider` values on open, then checkpoints and vacuums so the old bytes leave the database and WAL.
- Model discovery cannot be pointed at an attacker. Base URL and API-key sources are restricted to the environment and user-level files; workspace harness files no longer contribute to authentication. Requests require HTTPS (plain HTTP only for loopback), reject userinfo, do not follow redirects, and `HARNESS_MESH_OFFLINE=1` now also stops discovery HTTP. `trust_env=False` keeps proxy environment variables out of the market and discovery clients.
- Worktree `.git` pointers are validated before any Git command runs inside a worktree: the pointer must be a regular file, its `gitdir` must sit under the primary repository's `.git/worktrees/`, and the backlink must return to that worktree. The git guard hashes the pointer and gitdir path as well as the primary git metadata, closing a filter-driver path to command execution from a file-write-only agent.

### Security — high

- `task_id` is validated (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, no `..`, no trailing dot, reserved names rejected). A cross-process running assignment blocks the same id with a non-mutating `task_conflict` rejection, and worktree reuse requires a matching `.harness-mesh-worktree.json` marker. `task_id="."` can no longer remove the worktree root.
- Sensitive-file filtering now runs on the resolved path list rather than Git glob pathspecs, so nested `.env`/`*.pem`/`id_rsa` files and case-varied names are excluded from the diff, from the ledger and from `apply_task_patch`. Merge applies require a clean target and roll back on failure; apply verifies newly created symlinks and decodes Git's quoted paths.
- Instruction files (`AGENTS.md`, `CLAUDE.md`, `.harness-mesh/PROJECT_CONTEXT.md`, `.cursorrules`, Copilot instructions) must be regular files inside the workspace; symlinks and sensitive paths are rejected before their content reaches `get_project_context` or a dispatch prompt. Boundary-tag escaping normalises full-width lookalikes.
- `HARNESS_MESH_READ_ONLY=1` now refuses every write tool (`apply_task_patch`, `install_integration`, `update_project_context`, `report_quota_status`, `cancel_task`), and a caller resolved to a running assignment is refused the controller-only subset. `hmesh apply` and `hmesh install` use the same gate in the CLI. `--approve all` no longer pre-approves `apply_task_patch` or `install_integration`; those two are always manual.
- `hmesh context set` derives provenance from lineage, so a worker can no longer write into the trusted `<project_shared_memory>` block; `router.update_project_context` defaults to `auto`.
- Worker stdout/stderr, diffs and checkpoints are redacted before they are persisted or placed in a prompt; the transcript resource is capped by `transcript_max_chars` (default 200,000, head and tail kept) instead of growing without bound.
- `hmesh doctor` executes a registered command only when it is the current interpreter with `-m harness_mesh.mcp_server`; anything else is reported without running. DSH profile backups and uninstall now use atomic writes with mode 0600.

### Security — medium

- The state directory is created/tightened to 0700 and `state.db`, `-wal`, `-shm` to 0600. Generated per-task MCP config files are 0600 and their directory 0700.
- `market.total_timeout_seconds` (default 60) puts a wall-clock ceiling on a provider refresh; the per-socket timeout remains 20 seconds.
- Committed `.harness-mesh/` state is still not trusted as configuration, but the state directory can hold sensitive excerpts: keep it out of commits, archives and shared machines. Upgrading from 0.4.1 rewrites legacy credential columns on first open; restart any harness that runs `hmesh-mcp` so the new server process is used.
- New-user troubleshooting: `hmesh doctor`, `hmesh status` and `hmesh install` print one `binary hint` line when a configured harness CLI cannot be resolved, naming the missing harnesses, the user-level config path and the `binary_paths` option. Harnesses found on `PATH` never trigger the hint.

### Packaging, CI and documentation

- `MANIFEST.in` now ships exactly `config/harnesses.example.yaml` and the published docs instead of any `config/*.yaml`; a developer's local config can no longer leak into an sdist.
- CI actions are pinned by commit SHA (`actions/checkout`, `actions/setup-python`), and `pip-audit` is version-pinned and blocking. The audit runs in a dedicated virtualenv so preinstalled runner packages cannot fail or mask the project's own dependency set.
- The independent audit is published at `docs/security/2026-11-audit.md` with code locations and remediation. Raw credential values, local paths and ready-to-run attack payloads are not included; internal research and planning notes were removed from the repository.
- `docs/README.md` indexes the published documentation.

## 0.4.1 — 2026-11-01

Pre-release audit of the public repository. No runtime behaviour changes.

### Documentation

- Both READMEs explain that a harness's controller login and worker login differ. Measured with the Claude Code desktop app: it works as a controller while the bundled `claude` command stays logged out, so a worker needs `claude auth login` or `ANTHROPIC_API_KEY`; without it the circuit opens with `not_logged_in` and tickets move to another harness.
- `tests/fixtures/README.md` records the source and terms of every fixture, including the Epoch AI CC BY 4.0 attribution.
- `hmesh version` is listed with the other commands.

### Packaging and CI

- `pyproject.toml` declares the licence as the SPDX expression `MIT` with `license-files`, removing two setuptools deprecation warnings that become errors in 2027; the build backend now requires setuptools 77 or newer.
- Python 3.14 is declared and added to the CI matrix.
- Bandit reports no medium or high findings: column-name interpolation is restructured behind its allow-list and the DSH patch loader is annotated as a `SafeLoader` subclass.
- The source distribution now ships every test fixture (`*.json`, `*.csv`, `*.txt`, `*.md`), so its test suite runs from the archive.

### Repository

- Test modules are named after what they cover; internal work-package labels are gone from file names and docstrings.
- GitHub private vulnerability reporting, which `SECURITY.md` points to, is enabled.

## 0.4.0 — 2026-11-01

### Added

- Integration discovery for harnesses installed after the mesh. `hmesh integrations [--json]` reports `integrated`, `detected_not_integrated`, `stale`, `not_detected` and `ignored`; `hmesh doctor` and `hmesh status` end with the next command, and `list_harnesses` returns `integration_hints`. `plan_integration` returns the exact files, approval entries and commands with a `plan_id` over the plan and the current target files. `install_integration` is a write tool that recomputes the plan, refuses a stale id, refuses workers through lineage resolution and refuses `HARNESS_MESH_READ_ONLY=1`. `integration.ignore` and `hmesh integrations ignore|unignore` suppress reminders. Detection runs only inside these calls, starts no process and writes no file.
- Epoch AI "Capabilities & benchmarking" as a fourth, keyless market source (`epoch.ai/data/benchmark_data.zip`, CC BY 4.0). The archive is read with `zipfile`, only for the configured benchmark CSVs, with a 25 MB response cap, an 80 MB expanded cap, `..`/absolute-path rejection and same-host redirects. Each benchmark yields a percentile inside a 540-day reference window; the reasoning and coding axes are averaged, an axis with fewer than three scored benchmarks stays empty, and `intelligence_source` gains `epoch_ai`. The default cache lifetime is twelve hours. `hmesh market show` prints the required attribution and every routing decision carries it in `attribution`.
- `routing.axis_weights`: `plan`/`audit`/`review` score reasoning 0.7 / coding 0.3, `implement`/`refactor`/`debug`/`test` score coding 0.7 / reasoning 0.3, `docs` scores reasoning 1.0. A model with one axis uses that axis at full weight. Routing decisions now record `axis_scores` and `benchmarks_used`.
- `hmesh market show --axis reasoning|coding` sorts by and displays one axis, plus the benchmark count, benchmark data date and `intelligence_source`.
- Hands-off refresh. The MCP server refreshes a missing or expired cache in the background at start-up and on a TTL cadence. The first dispatch with no market data waits at most `market.wait_seconds` (8 s) and then continues with the local tiers; one-shot commands run the same refresh synchronously under the same bound and fall back to the previous snapshot or the heuristic. Failures only log a warning.
- The controller no longer names a harness or a model. `dispatch_task` over MCP and `hmesh dispatch` default `target_harness` to `auto` and route across every installed harness; the MCP `initialize` response tells clients to send an objective and an optional `task_kind` instead. `TaskRequest.target_harness` defaults to `auto`.
- Market scores drive discovery. `capability_score`, `assigned_tiers` and the tier map use the matched market intelligence when it exists, and `hmesh models`, `hmesh status`, `list_harnesses` and `hmesh doctor` expose each model's `intelligence`, `intelligence_source` and blended price. `hmesh doctor` adds a read-only `market` row with sources, last refresh, staleness, match rate and unmatched models.

### Fixed

- Machines with only some harnesses installed. `status`, `models` and `list_harnesses` report a missing harness as `not installed` and no longer list example-config models or tiers for it; `hmesh doctor` ends with `N of 5 harnesses installed`. ZCode returns `skipped: ZCode not found` without writing a plugin when neither the CLI nor the macOS app exists. `initial_harness` is the first runnable harness rather than the first configured one. A `usage limit` / `insufficient_quota` error opens the account-wide circuit, fails over to another harness, and otherwise suspends the ticket in `waiting_for_quota` instead of retrying every model on the same account. With no harness at all, dispatch returns the explicit install hint instead of `no_available_model`.
- models.dev reseller rows no longer become separate models. Entries fold onto one canonical model per name and the list price comes from the vendor's own entry, then OpenRouter, then the median of the priced offers. A row that prices input and output at zero is unknown, not free. The catalogue drops from thousands of rows to hundreds of models. Rows without a `canonical_model_id` now merge by their normalised model name too; when no canonical model exists they collapse into one `unknown/<name>` entry instead of one entry per reseller. This removes the duplicate `zai/glm-5-3` style candidates that left ten ZCode models unmatched. The example config lists `minimax` and `minimax-cn` as original vendors so the three MiniMax models keep the vendor price.
- `match_model` resolves several same-name candidates by evidence instead of giving up: an evaluation source (`epoch_ai` or `artificial_analysis`) first, then an OpenRouter or first-party offer, then a non-empty price. A remaining tie still returns `none`. A resolved match reports `confidence: normalized` and a short `note`.
- `hmesh market show --unmatched` prints only the unmatched harness models and the final attribution. Catalogue rows appear only when `--model` is also given; `--json` keeps the full catalogue and unmatched structures.
- The default `pytest` run sets `HARNESS_MESH_OFFLINE=1` for child processes, so subprocess CLI and MCP checks never download market data. The two CLI lineage checks no longer wait on the one-shot refresh timeout.
- `hmesh market show --model` applies the same normalisation as the catalogue (dots and hyphens are equivalent, case-insensitive, globs supported) and reports `no model matches GLOB` instead of claiming the cache is empty. `--unmatched` now reads the discovery ledger and lists harness models with no market match, with `confidence: none` and a `market.aliases` hint; an empty ledger asks for `hmesh models --refresh`.
- ZCode no longer falls back to its yolo default when approval is withheld (`shared_write` without `auto_approve_shared_write`, or `auto_approve: false`): the adapter passes `--mode build`, configurable to `edit` or `plan` through `restricted_mode`. Measured headless on ZCode 0.17.0: `build` denies writes and shell, `edit` allows writes only, `guarded` allows both.
- Install output and `hmesh doctor` approval lines are in English, matching the rest of the CLI.

### Added

- Read-only market data layer: OpenRouter and models.dev list prices, Artificial Analysis intelligence scores, model-id matching with explicit aliases, a six-hour snapshot cache, and `market.enabled` / `HARNESS_MESH_OFFLINE` switches. Every score is labelled `artificial_analysis`, `override` or `heuristic`; a failed refresh keeps the stale snapshot.
- Task-kind routing (`plan`, `audit`, `review`, `implement`, `refactor`, `debug`, `test`, `docs`, `auto`) with `premium`, `balanced` and `value` tiers, configurable value floors, model pins and soft or strict premium/value hints. Requests can set `task_kind`, `routing_mode`, `max_price_per_mtok` and `min_intelligence`; `HARNESS_MESH_ROUTING` overrides the configured mode.
- `hmesh market refresh`, `hmesh market show [--unmatched]`, `hmesh route`, `--kind` and `--routing-mode` on `hmesh dispatch`, plus the `get_model_market` and `preview_routing` MCP tools. Every task result carries a `routing` record and the transcript starts with the same block.
- Background market refresh on MCP server start and on the cache TTL. Failures log a warning and leave dispatch unchanged.
- `hmesh install --approve {read,dispatch,all,none}` (default `dispatch`) writes per-tool MCP pre-approvals for Claude Code (`~/.claude/settings.json`), Codex (`~/.codex/config.toml`) and Antigravity (`~/.gemini/config/config.json`). `read` covers the eleven read tools, `dispatch` adds the three dispatch tools, `all` covers all eighteen, and `none` writes nothing while removing rules written by this tool. ZCode and DeepSeek Harness need no approval settings.
- `hmesh doctor` reports approved and missing tools against the default `dispatch` level without changing files.

### Changed

- The install epilogue states the level and number of pre-approved tools instead of printing the old service-wide Codex and Antigravity hint.
- MCP now exposes 18 tools: 11 read, 3 dispatch and 4 write. `hmesh install --approve read` writes 11 rules, the default `dispatch` level 14, and `all` 18.

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

### Documented

- Headless approval behaviour measured on the real harnesses: Codex and Antigravity deny MCP calls without an approval setting. `hmesh install` now prints the two settings after a successful install.

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
- The CLI resolves a cwd under `.harness-mesh/worktrees/` to the primary project root as well, so `hmesh dispatch`, `hmesh status`, and `hmesh tasks` started in a worker worktree use the project ledger instead of creating another `state.db` inside the worktree. `ensure_service_workspace` rejects a task worktree as the workspace root even when `--workspace` names it explicitly; run from the project root or set `HARNESS_MESH_WORKSPACE`.
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
