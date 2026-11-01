# Harness-Mesh

Run one coding ticket across Claude Code, OpenAI Codex, DeepSeek Harness, Google Antigravity and ZCode. A ticket owns one isolated Git worktree; individual *assignments* may fail over between harnesses, models and sessions while the worktree and partial work stay in place.

[中文文档](README.zh-CN.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [MIT License](LICENSE)

## What the product is

Harness-Mesh is a **local orchestration layer**, shipped as:

- a Python package (`harness_mesh`) with a typed data model and SQLite ledger;
- a CLI, `hmesh`, for status, model discovery, dispatch, context and patches;
- an stdio MCP server, `hmesh-mcp`, exposing 18 tools to a controller harness;
- a local runtime directory, `.harness-mesh/`, holding state, worktrees and paged resources.

It does not provide a model, a hosted service, a web UI or a multi-user scheduler. It orchestrates the harness CLIs you already have installed and authenticated on the machine. Prompts and code are sent to those CLIs; the mesh process itself keeps a local ledger and bounded summaries.

## Who it is for

- Developers who use more than one coding harness and want one task queue instead of copying prompts between terminals.
- Platform and agent-infrastructure engineers who need quota-aware routing, worktree isolation and auditable assignment history.
- Researchers and reviewers evaluating cross-harness scheduling, failover and shared-context behaviour.

It is not a replacement for a harness's permission model, sandbox, or vendor terms. It assumes you have legitimate access to each CLI and its models.

## Scope

| In scope | Out of scope |
| :--- | :--- |
| Dynamic discovery of locally authorised models from configuration, environment, live model endpoints and CLI enumeration | Maintaining a fixed or hosted model catalogue |
| Difficulty tiers plus market-aware routing by task kind, and skill/plugin injection | Training or hosting models |
| Ticket/Assignment failover on quota exhaustion, unauthorised models, credential failures and transient errors | Multi-tenant authentication, RBAC or billing |
| Git worktree isolation, dirty-workspace sync, dependency pre-apply and explicit patch application | Merging worktrees automatically without review |
| Shared project instructions, a cross-harness memory blackboard and dependency context | Uploading project data to a remote service |
| Claude Code, Codex, DSH, Antigravity and ZCode adapters | Changing a vendor CLI's own protocol beyond the configured template |

## Requirements

- Python 3.11+.
- One or more supported harness CLIs installed and logged in. A harness that Harness-Mesh runs as a worker needs its own command-line login; see [Logins: controller versus worker](#logins-controller-versus-worker).
- Git available on `PATH` for isolated worktrees.
- macOS is the primary target for the shipped App Bundle paths. Linux/WSL works with binaries on `PATH`; real-machine tests skip when the macOS bundles are absent.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Two entry points are installed:

- `hmesh` — operator CLI.
- `hmesh-mcp` — MCP stdio server.

Optional Antigravity Python SDK support:

```bash
.venv/bin/pip install -e ".[antigravity]"
```

## One-command install into desktop harnesses

After installing the Python package, register Harness-Mesh in every detected desktop harness with one command:

```bash
cd /path/to/your/project
hmesh install
```

What it installs:

| Harness | Integration |
| :--- | :--- |
| Claude Code | `claude mcp add` user-scope MCP server |
| OpenAI Codex / ChatGPT desktop | `codex mcp add` MCP server in `~/.codex/config.toml` |
| Google Antigravity | `agy mcp add` MCP server in `~/.gemini/config/mcp_config.json` |
| ZCode | Generates a local MCP plugin + marketplace, registers/installs it through `zcode plugins`; if the CLI is unavailable, leaves the marketplace ready for the UI |
| DeepSeek Harness | Adds an `@deepseek-ai/dsh-mcp-client` entry to `~/.dsh/profiles/{desktop,headless}/cordis.patch.yml` (with backup) |

Useful options:

```bash
hmesh install --dry-run                 # print planned commands/files, change nothing
hmesh install --yes                     # non-interactive install
hmesh install --approve read            # pre-approve 11 read-only MCP tools
hmesh install --harness zcode           # one harness only (repeatable)
hmesh install --workspace /path/project # pin the MCP workspace explicitly
hmesh install --uninstall               # remove integrations
```

Without `--dry-run`, `hmesh install` prints the planned files and commands and asks for confirmation. A non-interactive shell must pass `--yes`.

### MCP tool pre-approvals

Interactive sessions ask before an MCP tool runs. Headless commands such as `codex exec` and `agy -p` cannot answer that prompt, so `hmesh install` writes per-tool allow rules:

```bash
hmesh install --approve dispatch   # default: 14 read and dispatch tools
hmesh install --approve read       # 11 read-only tools
hmesh install --approve all        # all 18 tools
hmesh install --approve none       # write nothing and remove rules written by hmesh
```

`dispatch`, the default, approves the eleven read tools plus `dispatch_task`, `parallel_dispatch` and `cancel_task`. Dispatched work runs in an isolated worktree; its changes reach the primary checkout only through `apply_task_patch`. The four write tools stay manual: `apply_task_patch`, `report_quota_status`, `update_project_context` and `install_integration` change the primary workspace, shared state or another application's configuration. Pass `--approve all` to pre-approve them too.

| Harness | File | Rule |
| :--- | :--- | :--- |
| Claude Code | `~/.claude/settings.json` | `permissions.allow` gains `mcp__harness-mesh__<tool>` |
| Codex | `~/.codex/config.toml` | A managed block holds `[mcp_servers.harness-mesh.tools.<tool>]` with `approval_mode = "approve"` |
| Antigravity | `~/.gemini/config/config.json` | `userSettings.globalPermissionGrants.allow` gains `mcp(harness-mesh/<tool>)` |
| ZCode, DeepSeek Harness | none | Workers call MCP tools without approval settings |

The installer is idempotent. It keeps a `<file>.harness-mesh.bak` copy before the first edit, writes through a temporary file, re-parses the result and restores the previous bytes if validation fails. Lowering the level or uninstalling removes only entries with the `mcp__harness-mesh__` or `mcp(harness-mesh/` prefix, plus the Codex managed block; other rules stay in place. `--dry-run` prints the approval count and each entry without touching files. `hmesh doctor` reports approved and missing tools against the default `dispatch` level and changes no files; on Codex, a service-wide `default_tools_approval_mode = "approve"` counts as covering every tool.

Headless runs cannot answer approval prompts. Measured on the real harnesses (2026-11-01): `codex exec` denies the MCP call with "approval policy is never" unless the server is approved; `agy -p` auto-denies it unless `permissions.allow` holds a matching rule such as `mcp(harness-mesh/list_harnesses)`. The per-tool rules above cover the selected level; the service-wide Codex setting `-c 'mcp_servers.harness-mesh.default_tools_approval_mode="approve"'` remains a manual alternative that approves every tool. ZCode and DeepSeek Harness workers call the tools without extra configuration, and Claude Code shows the server as connected. Interactive sessions prompt as usual.

ZCode's `--scope project` still records the marketplace and plugin in the global state files `~/.zcode/cli/plugins/known_marketplaces.json` and `installed_plugins.json`, even though the plugin and its cache are project-scoped. `hmesh install --uninstall` removes both entries; the matching `zcode plugins uninstall` and `zcode plugins marketplace remove` commands do the same by hand.

Restart the desktop application after installation and start a new chat/task. Existing chats may keep a cached tool list. The installer is idempotent; editing an existing DSH entry or re-adding an MCP server replaces the previous registration rather than duplicating it.

### When another harness arrives later

Claude Code and Codex are often installed first; ZCode or another supported harness may follow. The two roles need different handling.

**Worker.** No action. Dispatch re-resolves the binary for every assignment and routing re-checks adapter runnability, so the next ticket can use the new harness. Only the model inventory is cached, for five minutes.

**Controller.** The harness must have the Mesh MCP server or plugin registered before it can call these tools. Mesh reports the gap and leaves the decision to you:

- `hmesh integrations` lists every configured harness as `integrated`, `detected_not_integrated`, `stale` (registered but the command no longer runs), `not_detected` or `ignored`.
- `hmesh doctor` and `hmesh status` end with one line naming the detected harness and the install command.
- Inside a controller conversation, `list_harnesses` adds `integration_hints`. The agent explains the hint, calls `plan_integration` to show the files, approval entries and commands, and calls `install_integration` only after you agree.

`plan_integration` returns a `plan_id`: the first 16 hex characters of a SHA-256 over the normalised plan and the current content of the target files. `install_integration` recomputes the plan and refuses any id that no longer matches, so editing a config file between review and install forces a new review. It is a `write` tool; the default `dispatch` level leaves it for manual approval. Workers are refused through the same lineage resolution as nested dispatch, `HARNESS_MESH_READ_ONLY=1` blocks it, and the target harness must restart before its tool list appears.

`hmesh integrations ignore zcode` stops the reminder and `unignore` restores it. The command writes only `integration.ignore` in the Mesh config; it never edits another application's configuration.

### Logins: controller versus worker

A harness can play two roles, and each role uses a different login.

| Role | Who calls the model | Login it needs |
|---|---|---|
| Controller | You, inside the harness, for example the Claude Code desktop app calling the harness-mesh tools | The login the harness application already has |
| Worker | Harness-Mesh starts the harness command-line tool as a subprocess to run a ticket | The command-line tool's own credentials |

The two logins can differ. Measured on macOS with the Claude Code desktop app (2026-11-01): the app was logged in and called every harness-mesh tool as a controller, while the bundled `claude` command reported `loggedIn: false` and `Not logged in · Please run /login`. The desktop app hands credentials to the sessions it hosts through its own channel; no keychain item, credentials file or API key existed for a separately started `claude` to read. A command-line Claude Code used as a controller is itself the command line, so it needs the command-line login too.

When a worker is not logged in, Harness-Mesh does not fail the ticket. The first attempt returns `not_logged_in`, the harness circuit opens (`hmesh quota` shows `claude_code/*: state=open reason=not_logged_in`), routing skips that harness and the ticket moves to another one, for example Claude models served through Antigravity. Probes run automatically and the circuit closes once the login works.

To use the Claude Code command line as a worker, log in once in a terminal and confirm it:

```bash
claude auth login        # or set ANTHROPIC_API_KEY
claude auth status       # loggedIn must be true
```

The same rule applies to every harness: a worker uses that tool's own login. `hmesh status` and `hmesh quota` show which harnesses are usable. Not verified: whether a worker started by a Harness-Mesh server hosted inside the desktop app can inherit the desktop login; on the test machine it could not.

## Quick start

```bash
# Discover installed harnesses, real CLI versions and authorised models.
hmesh status

# Force the three-layer model discovery and the tier mapping.
hmesh models --refresh

# Inspect quota circuits and waiting tickets.
hmesh quota --probe

# Fetch market prices and evaluation scores, then preview a route.
hmesh market refresh
hmesh market show --unmatched
hmesh route --objective "Design the migration plan" --kind plan

# List indexed SKILL.md files and MCP/plugin manifests.
hmesh skills

# Write and read a shared project fact.
hmesh context set arch.overview "Federated Harness Mesh" --category architecture
hmesh context list

# Print the MCP configuration for a controller harness.
hmesh init-mcp --print

# Dispatch a ticket and inspect the result.
hmesh dispatch --harness deepseek \
  --objective "Create a short design note in docs/mesh.md" \
  --difficulty medium --fallback codex --fallback zcode

hmesh tasks
hmesh read <task_id> diff --limit 4000
hmesh apply <task_id> --strategy patch
hmesh apply <task_id> --strategy copy --force-copy
hmesh doctor
```

Start the MCP server with background recovery, effects consumption and market refresh (the default):

```bash
hmesh-mcp
```

Disable every background worker for a pure request/response server:

```bash
hmesh-mcp --no-monitor
```

The server reads `HARNESS_MESH_WORKSPACE` for the primary project root and `HARNESS_MESH_CONFIG` for an optional YAML configuration.

## Supported harnesses

The defaults target the real macOS installations observed on a developer machine. All paths and command templates are configuration, not code.

| Harness | Default binary search | Real non-interactive protocol | Model discovery |
| :--- | :--- | :--- | :--- |
| Claude Code | `~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude` | `claude -p <prompt> --output-format json --model <model> --effort <effort> ...`; write mode adds `--dangerously-skip-permissions`, read-only adds `--permission-mode plan`, resume adds `--resume <id>` | `~/.claude/cache/model-catalog/*.json`, `~/.claude.json`, settings, env, `/v1/models` |
| OpenAI Codex | `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex` and plugin app-server path | `codex exec ... --json ...`; write mode adds `--full-auto`, read-only adds `--sandbox read-only`, reasoning uses `-c model_reasoning_effort=...` | `~/.codex/config.toml`, `~/.codex/models_cache.json`, env, `/v1/models` |
| DeepSeek Harness | `/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh` | `dsh --profile headless --patch <overlay> --json <prompt>`; generated overlay re-enables local credentials and merges the desktop model/provider row | `~/.dsh/profiles/*/cordis.patch.yml`, env, optional API, plus the built-in runtime channel |
| Google Antigravity | `/Applications/Gemini.app/Contents/Helpers/agy` | `agy -p <prompt> --output-format json --model <model> --effort <effort>`; write mode adds `--dangerously-skip-permissions`, read-only `--mode plan`, resume `--conversation <id>` | `agy models` CLI (spinner frames cleaned), local settings, env, Gemini API |
| ZCode | `/Applications/ZCode.app/...` wrapped into `.harness-mesh/bin/zcode` | wrapper runs the Electron binary with the CJS entry and provider config; `zcode -p <prompt> --cwd <dir> --json --mode plan|yolo`, resume `--resume <id>` | `~/.zcode/v2/*.json`, provider/model order fields, env |

`hmesh status` returns `available: true` only when the binary resolves and its version command succeeds. Model lists are runtime data and are never hard-coded.

## How it works

### Ticket, Assignment and failover

```text
Ticket task_x (one Git worktree)
  ├── Assignment #1  claude_code/claude-opus-<version>   ended_quota
  ├── Assignment #2  codex/gpt-<version>-<variant>             completed
  └── failover_history: quota_exhausted:rate_limit_429
```

1. `dispatch` creates a ticket and an isolated worktree. If the primary checkout has uncommitted files, `sync_uncommitted` copies them into the worktree and records a baseline commit.
2. The router selects an authorised, quota-healthy model for the task's difficulty tier and runs one adapter.
3. On quota exhaustion, an unauthorised model, a harness-wide login failure, or a transient error, it records a `ContinuationCheckpoint` and creates a new assignment on the next harness/model. The worktree is reused.
4. Recovery Notes force the next agent to inspect `git status` and `git diff` before continuing. The final diff is measured against the baseline commit, so only the ticket's net changes are returned.
5. Outputs are capped: summary, diff preview and raw logs are bounded; full content is saved as `diff`, `transcript` and `assignments` resources and read page by page with `read_task_resource`.

Quota policies:

- `failover` (default): create a new assignment on the next healthy candidate.
- `wait_and_resume`: suspend the ticket as `waiting_for_quota` and resume it after a real provider probe succeeds.
- `fail`: fail immediately.

Circuits are `CLOSED` / `OPEN` / `HALF_OPEN`. A probe makes one deliberately tiny real model call; local configuration alone never closes a quota circuit. A successful assignment also closes it.

### Shared project context

- `ProjectContextManager` scans `AGENTS.md`, `CLAUDE.md`, `.harness-mesh/PROJECT_CONTEXT.md`, `.cursorrules` and `.github/copilot-instructions.md`, with a character budget.
- `update_project_context` writes architecture notes, conventions, API contracts, decisions, pitfalls and notes to a SQLite blackboard. Later tickets receive `<project_shared_memory>`.
- `context_files` are rendered as bounded `<context_file_previews>` so the child agent does not start with a blind search.
- `depends_on` injects upstream summaries, modified files and diffs as `<upstream_task_outputs>` and pre-applies the upstream diff into the downstream worktree baseline.
- `auto_record_context` records a completed ticket's summary as a shared note.

These blocks are project data, not instructions that override the ticket objective or the security policy.

### Sessions and permissions

`TaskRequest.session_id` flows through MCP and `AdapterCall`. Adapters map it to `--resume` (Claude/ZCode), `codex exec resume <id>`, `--session-id` (DSH) or `--conversation` (Antigravity). Write-mode templates inject each CLI's approval bypass; read-only tickets use plan/read-only modes and run in a disposable worktree.

## Market data and model routing

The mesh can rank models by published price and evaluation score instead of relying only on the four difficulty tiers. The data layer is read-only and separate from dispatch.

### Sources

| Source | Authentication | Fields used |
| :--- | :--- | :--- |
| OpenRouter `/api/v1/models` | none | list price per token, context window, reasoning and tool flags |
| models.dev `/api.json` | none | list price per million tokens, context window, release date |
| Epoch AI `benchmark_data.zip` | none | reasoning and coding benchmark percentiles, benchmark dates |
| Artificial Analysis `/api/v2/language/models/free` | `ARTIFICIAL_ANALYSIS_API_KEY` | intelligence index, list price, output speed |

models.dev lists the same model once per provider, including hundreds of resale gateways. Those rows fold onto one canonical model per name. The price comes from the model vendor's own entry (listed in `market.first_party_providers`), then from OpenRouter, then from the median of every priced offer. An entry that prices input and output at zero counts as unknown, not free.

Epoch AI publishes the CC BY 4.0 ["Capabilities & benchmarking"](https://epoch.ai/benchmarks) archive and needs no key, so it is the default evaluation source. Each benchmark contributes one percentile computed inside a 540-day reference window (`market.epoch.reference_window_days`); an axis with fewer than three scored benchmarks stays empty (`market.epoch.min_benchmarks`). The two axes are `intelligence_reasoning` and `intelligence_coding`, and `intelligence` is the equal-weight mean of the axes a model has. Artificial Analysis takes precedence when `ARTIFICIAL_ANALYSIS_API_KEY` is set. Without either source the mesh computes a labelled heuristic from the existing discovery score, the price percentile, the reasoning flag and the release date.

The heuristic is not an evaluation. Every score carries `intelligence_source`: `epoch_ai`, `artificial_analysis`, `override` or `heuristic`. `hmesh market show` and the `get_model_market` MCP tool print that field.

Epoch AI data is CC BY 4.0 and requires attribution. `hmesh market show` ends with `Benchmark data: Epoch AI, 'Capabilities & benchmarking', https://epoch.ai/benchmarks (CC BY 4.0)`; `--json` carries the same string in `attribution`, and every routing decision includes it.

`hmesh market refresh` writes a snapshot to `.harness-mesh/state.db`. The default lifetime is six hours. Epoch AI and Artificial Analysis are cached for at least twelve hours; Artificial Analysis is also limited to 20 requests per process per day. A failed refresh keeps the previous snapshot and marks it stale. Cached data older than seven days still works and is flagged expired.

`market.enabled: false` or `HARNESS_MESH_OFFLINE=1` stops every outbound request.

### Hands-off refresh

The cache stays current without a manual step. The MCP server refreshes in the background at start-up when a source is missing or past its TTL, then on a TTL cadence. The first dispatch that finds no market data at all waits at most `market.wait_seconds` (8 s) and then continues with the local tiers while the refresh finishes in the background. One-shot commands (`dispatch`, `route`, `status`, `models`, `market show`) run the same refresh synchronously under the same 8 s bound and fall back to the previous snapshot or the heuristic when a source is slow or down. A failed refresh only logs a warning.

The controller sends an objective, nothing else. `dispatch_task` over MCP and `hmesh dispatch` default `target_harness` to `auto`, so the mesh routes across every installed harness and picks the model. The MCP `initialize` response states the same rule to every client. Passing `model` or `target_harness` explicitly keeps the previous behaviour.

On a machine with only some of the five harnesses, `hmesh status`, `hmesh models` and `list_harnesses` report a missing harness as `not installed` with the reason and leave its models and tier map empty; `hmesh doctor` ends with `N of 5 harnesses installed`. `hmesh install --dry-run` returns `skipped` for a missing binary, and ZCode is skipped as `ZCode not found` without writing a plugin when neither its CLI nor its app is present.

The market score also drives discovery: when a harness model matches the catalogue, its `capability_score`, `assigned_tiers` and tier map come from the market intelligence instead of the local heuristic. `hmesh models`, `hmesh status` and `list_harnesses` show each model's `intelligence`, `intelligence_source` and blended price, so the tier a user reads, the model dispatch picks and the route preview agree. `hmesh doctor` adds a read-only `market` row with the sources, the last successful refresh, staleness, the match rate and the unmatched models.

### Task kinds and tiers

`task_kind` is `plan`, `audit`, `review`, `implement`, `refactor`, `debug`, `test`, `docs` or `auto`. `auto` reads the objective (Chinese and English keywords) and otherwise follows `evaluate_difficulty`.

| Tier | Task kinds and difficulty | Rule |
| :--- | :--- | :--- |
| `premium` | `plan`, `audit`, `review`, or `critical` | highest intelligence; ties go to the lower blended price |
| `balanced` | `refactor`, `debug`, or `high` | highest intelligence per blended dollar among candidates at or above 85% of the best score |
| `value` | `implement`, `test`, `docs`, or `low`/`medium` | cheapest candidate at or above a difficulty floor (0.55 for low, 0.70 for medium) |

`blended_price = (3 * input + output) / 4` in USD per million tokens. A model with no known price is left out of price comparisons and used only when nothing else qualifies.

`routing.axis_weights` decides which evaluation axis a task kind is scored on: `plan`, `audit` and `review` use reasoning 0.7 / coding 0.3; `implement`, `refactor`, `debug` and `test` use coding 0.7 / reasoning 0.3; `docs` uses reasoning 1.0. When a model has only one axis, that axis carries full weight.

Claude Code and Codex bill through a subscription quota rather than per token. The router uses the API list price as a proxy for quota consumption and states that in every decision record.

### Routing modes

`routing.mode` defaults to `auto`. `manual` keeps the model you pass; `off` uses the four difficulty tiers. `HARNESS_MESH_ROUTING` overrides the configured default, and a request-level `routing_mode` wins over both.

`routing.pins` fixes `{harness, model}` for one task kind. `routing.premium_hints` and `routing.value_hints` add weight to matching models; `routing.strict_hints: true` turns them into a hard filter. The example configuration uses exact `glm-5.3`, so it does not match `glm-5.3-flash`.

When no market snapshot exists, no harness model matches the catalogue, or the planner returns no choice, dispatch falls back to `discovery.resolve_model(harness, difficulty)`. The decision record keeps the reason.

### Decision record

Every result carries a `routing` object: mode, task kind, tier, chosen harness/model/effort, intelligence and its source, `axis_scores` (both axis scores with the weights used), `benchmarks_used`, the attribution string, list prices, the top three alternatives with scores, the data sources, `fetched_at`, `stale`, `expired` and the match confidence. The same block appears at the top of the transcript.

### Not connected

The Aider polyglot leaderboard has no data after 2025-10. The community LMArena snapshot (`oolong-tea-2026/arena-ai-leaderboards`) has returned empty `leaderboards` every day since 2026-10-20. `api.llm-stats.com` returns vendor-reported scores marked `verified: false`, only four models carry an AA-Index, and its terms page returns 404. None of the three is used.

## MCP tools (18)

| Tool | Purpose |
| :--- | :--- |
| `list_harnesses` | Installed harnesses, real binary, version, authorised models, tier map and quota state |
| `discover_harness_models` | Force three-layer discovery for one or all harnesses |
| `get_model_market` | Read cached list prices, context windows and intelligence scores; `intelligence_source` names the source |
| `preview_routing` | Rank a task without dispatching: task kind, tier, chosen model and alternatives |
| `list_skills_and_plugins` | Search indexed `SKILL.md` files and MCP/plugin manifests |
| `dispatch_task` | Create a ticket, choose a model and run assignments with failover |
| `parallel_dispatch` | Dispatch several tickets concurrently |
| `get_task_status` | Ticket status plus assignments and failover history |
| `read_task_resource` | Page through `diff`, `transcript` or `assignments` |
| `get_quota_status` | Circuits, waiting queue; `probe_now` probes and resumes |
| `get_integration_status` | Five-state integration report for every enabled harness; read-only |
| `plan_integration` | Reviewable install plan and `plan_id`; writes nothing |
| `install_integration` | Write tool: installs only when `plan_id` still matches; workers and read-only mode are refused |
| `report_quota_status` | Manual `reset`, `open` or `half_open` |
| `apply_task_patch` | Apply a completed isolated worktree to the primary workspace with `patch` or `merge` (copy stays CLI-only) |
| `cancel_task` | Cancel a running or waiting ticket; child processes are killed |
| `get_project_context` | Read shared memory entries and scanned instruction files |
| `update_project_context` | Write or update one shared project fact/contract |

## CLI

```text
hmesh install [--dry-run] [--yes] [--approve LEVEL] [--harness H]... [--uninstall]
hmesh doctor [--harness H] [--json]
hmesh status [--refresh] [--json]
hmesh integrations [--json]
hmesh integrations ignore|unignore NAME [--json]
hmesh models [--refresh] [--harness H]
hmesh quota [--probe]
hmesh route --objective O [--kind K] [--difficulty D] [--harness H] [--json]
hmesh market refresh [--source S]... [--json]
hmesh market show [--model GLOB] [--axis reasoning|coding] [--unmatched] [--json]
hmesh skills [--query Q]
hmesh dispatch --objective O [--harness H] [--difficulty D] [--kind K] [--routing-mode M] [--fallback H]...
hmesh tasks [--status S]
hmesh read <task_id> [diff|transcript|assignments] [--offset N] [--limit N]
hmesh apply <task_id> [--strategy patch|merge|copy] [--force-copy]
hmesh context list [--category C] [--json]
hmesh context set <key> <content> [--category C] [--author A] [--task-id T]
hmesh init-mcp [--print] [--target PATH]
```

## Configuration

The template lives at [`config/harnesses.example.yaml`](config/harnesses.example.yaml). Copy it to `.harness-mesh/config.yaml` or pass `--config` / `HARNESS_MESH_CONFIG`. Binary search supports `~` and `*` globs and selects the newest version. ZCode on macOS is wrapped automatically into `.harness-mesh/bin/zcode`.

Templates support `{prompt}`, `{system_prompt}`, `{system_prompt_file}`, `{model}`, `{effort}`, `{workdir}`, `{mcp_config}`, `{task_id}` and `{assignment_id}`. Empty flag/value pairs are dropped, so `<cli_active_default>` means "use the harness's own active model".

Key settings:

| Setting | Default | Meaning |
| :--- | :--- | :--- |
| `state_dir` | `.harness-mesh` | Ledger, worktrees, generated MCP configs |
| `allowed_workspace_roots` | service workspace | Optional allowlist for request-level `workspace_root`; symlinks are resolved before containment checks |
| `auto_init_repo` | `false` | Refuse a non-Git workspace unless it is explicitly enabled |
| `auto_approve_shared_write` | `false` | Injection of approval-bypass flags when `workspace_mode=shared_write` |
| ZCode `extra.restricted_mode` | `build` | Mode used when approval is withheld (`build`, `edit` or `plan`); `zcode -p` would otherwise default to yolo |
| `max_active_assignments` | `8` | Global cap on simultaneously running assignments |
| `summary_max_chars` | `1500` | Summary returned to the controller |
| `diff_preview_max_chars` | `4000` | Diff preview returned to the controller |
| `max_call_depth` | `3` | Nested dispatch depth limit |
| `max_failover_attempts` | `4` | Maximum failovers per ticket |
| `quota_probe_interval_seconds` | `60` | Background recovery probe interval |
| `difficulty_timeout_seconds` | `600/1200/1800/3600` | Per-tier assignment timeout |
| `market.enabled` | `true` | Read-only market data and its snapshot cache |
| `market.ttl_seconds` | `21600` | Snapshot lifetime for OpenRouter and models.dev |
| `market.wait_seconds` | `8` | Longest automatic refresh before dispatch or a command continues |
| `market.sources` | four sources | Providers to refresh |
| `market.first_party_providers` | `[]` (the example config lists twelve) | Vendors whose own models.dev entry sets the list price |
| `market.epoch.reasoning_benchmarks` | six benchmarks | Benchmarks that feed the reasoning axis |
| `market.epoch.coding_benchmarks` | nine benchmarks | Benchmarks that feed the coding axis |
| `market.epoch.reference_window_days` | `540` | Percentile window before a benchmark's latest release |
| `market.epoch.min_benchmarks` | `3` | Minimum scored benchmarks before an axis counts |
| `market.aliases` | `{}` | Map a harness model id or glob to a market key |
| `market.overrides` | `{}` | Glob-keyed intelligence and price overrides |
| `routing.mode` | `auto` | `auto`, `manual` or `off`; `HARNESS_MESH_ROUTING` wins |
| `routing.floors` | `0.55/0.70` | Value-tier intelligence floor by difficulty |
| `routing.axis_weights` | per task kind | Reasoning/coding weights used to score a task |
| `routing.premium_hints` / `routing.value_hints` | `[]` | Model globs weighted per tier |
| `routing.strict_hints` | `false` | Turn hint weighting into a filter |
| `routing.pins` | `{}` | Fixed harness/model per task kind |
| `integration.ignore` | `[]` | Harness names whose integration hints are suppressed |

## Testing

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src tests
.venv/bin/pytest -v
```

The fast suite uses mocked adapters, temporary Git repositories, `httpx.MockTransport` market providers and cached snapshots. It opens no sockets. Live HTTP checks live in `tests/test_real_machine_market.py` and run only with `HARNESS_MESH_REAL_MACHINE=1`; the Artificial Analysis branch also needs `ARTIFICIAL_ANALYSIS_API_KEY`. `tests/test_local_machine_harnesses.py` additionally exercises the real locally installed harnesses when the macOS bundles are present; it asserts real binary resolution, real model inventories, and a real DeepSeek ticket that creates a file in a worktree and applies it back. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow.

## Operational notes

- The mesh state directory may contain source excerpts and prompts. Keep it out of commits and shared archives.
- Approval-bypass flags make a disposable worktree convenient, not safe. Built-in harnesses default to `auto_approve: true` for isolated worktrees; `shared_write` suppresses bypass unless `auto_approve_shared_write: true`. Use isolated worktrees, review diffs, and respect provider terms.
- The DeepSeek built-in runtime strips credential-shaped environment variables from its shell tool. That reduces accidental key exposure but does not create a sandbox.
- A one-shot `hmesh dispatch --policy wait_and_resume` exits after scheduling the suspension; the long-lived MCP server runs the background monitor and effects consumer. Use `hmesh quota --probe` or the MCP tool to resume manually.
- Antigravity SDK timeouts cancel waiting but cannot kill a third-party worker thread.
- Codex CLI versions differ in accepted approval flags. The built-in default injects `--full-auto`; the example YAML overrides it with `--dangerously-bypass-approvals-and-sandbox` for codex-cli 0.166. Adjust `execute_template` or `extra.auto_approve_args` for your version.

## Workspace and context boundaries

- The MCP server and CLI require a Git workspace; startup fails from `$HOME`, `/`, or a directory without Git.
- Request-level `workspace_root` must resolve (including symlinks) inside `allowed_workspace_roots`, which defaults to the service workspace.
- Sensitive filtering covers `.env` and real credential/key files, plus `.ssh`, `.aws`, `.gnupg`, and `.git` path segments. `.env.example`, `.env.sample`, `.env.template`, `.env.dist`, ordinary `credentials_service.py`-style source, and public `*.pub` keys stay visible. Every skipped path is reported in `skipped_sensitive` and warnings; Mesh no longer edits the user's `.git/info/exclude`.
- Worktree symlinks are rewritten as relative links into the worktree. Escaping or dangling links are reported; apply rejects symlink diffs that leave the target root.
- Approval bypass flags are suppressed for `shared_write` unless `auto_approve_shared_write: true`; the DeepSeek built-in shell drops credential-shaped environment variables, cloud key names, SSH agent sockets, and URLs containing passwords.
- Shared memory is capped at 128 characters per key, 4,000 per entry and 6,000 rendered characters. Only `controller` entries render as verified; worker and auto entries render in a separate unverified block. Author validation checks the name only.
- Mesh Git commands disable hooks, fsmonitor, pager, external diff and protocol.ext, record a per-task metadata guard once, and reject apply or abort the ticket when metadata changes after task start.
- MCP call depth can be recovered from the worktree cwd or lineage marker when inherited environment variables are unavailable; `max_active_assignments` caps running assignments.

## Trademarks and independence

Harness-Mesh is an independent open-source project. It is not affiliated with, sponsored by, or endorsed by Anthropic, OpenAI, DeepSeek, Google, or ZCode. Claude, Codex, DeepSeek Harness, Google Antigravity, and ZCode are names or trademarks of their respective owners, used here only to describe interoperability. Each harness remains governed by its own licence, acceptable-use policy, and data terms.

## License

MIT. See [LICENSE](LICENSE).
