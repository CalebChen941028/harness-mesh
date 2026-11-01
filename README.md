# Harness-Mesh

Run one coding ticket across Claude Code, OpenAI Codex, DeepSeek Harness, Google Antigravity and ZCode. A ticket owns one isolated Git worktree; individual *assignments* may fail over between harnesses, models and sessions while the worktree and partial work stay in place.

[中文文档](README.zh-CN.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [MIT License](LICENSE)

## What the product is

Harness-Mesh is a **local orchestration layer**, shipped as:

- a Python package (`harness_mesh`) with a typed data model and SQLite ledger;
- a CLI, `hmesh`, for status, model discovery, dispatch, context and patches;
- an stdio MCP server, `hmesh-mcp`, exposing 13 tools to a controller harness;
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
| Four-tier difficulty routing (`low` → `critical`) and skill/plugin injection | Training or hosting models |
| Ticket/Assignment failover on quota exhaustion, unauthorised models, credential failures and transient errors | Multi-tenant authentication, RBAC or billing |
| Git worktree isolation, dirty-workspace sync, dependency pre-apply and explicit patch application | Merging worktrees automatically without review |
| Shared project instructions, a cross-harness memory blackboard and dependency context | Uploading project data to a remote service |
| Claude Code, Codex, DSH, Antigravity and ZCode adapters | Changing a vendor CLI's own protocol beyond the configured template |

## Requirements

- Python 3.11+.
- One or more supported harness CLIs installed and logged in.
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
hmesh install --harness zcode           # one harness only (repeatable)
hmesh install --workspace /path/project # pin the MCP workspace explicitly
hmesh install --uninstall               # remove integrations
```

Without `--dry-run`, `hmesh install` prints the planned files and commands and asks for confirmation. A non-interactive shell must pass `--yes`.

Restart the desktop application after installation and start a new chat/task. Existing chats may keep a cached tool list. The installer is idempotent; editing an existing DSH entry or re-adding an MCP server replaces the previous registration rather than duplicating it.

## Quick start

```bash
# Discover installed harnesses, real CLI versions and authorised models.
hmesh status

# Force the three-layer model discovery and the four-tier mapping.
hmesh models --refresh

# Inspect quota circuits and waiting tickets.
hmesh quota --probe

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

Start the MCP server with background recovery and effects consumption (the default):

```bash
hmesh-mcp
```

Disable background workers for a pure request/response server:

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

## MCP tools (13)

| Tool | Purpose |
| :--- | :--- |
| `list_harnesses` | Installed harnesses, real binary, version, authorised models, tier map and quota state |
| `discover_harness_models` | Force three-layer discovery for one or all harnesses |
| `list_skills_and_plugins` | Search indexed `SKILL.md` files and MCP/plugin manifests |
| `dispatch_task` | Create a ticket, choose a model and run assignments with failover |
| `parallel_dispatch` | Dispatch several tickets concurrently |
| `get_task_status` | Ticket status plus assignments and failover history |
| `read_task_resource` | Page through `diff`, `transcript` or `assignments` |
| `get_quota_status` | Circuits, waiting queue; `probe_now` probes and resumes |
| `report_quota_status` | Manual `reset`, `open` or `half_open` |
| `apply_task_patch` | Apply a completed isolated worktree to the primary workspace with `patch` or `merge` (copy stays CLI-only) |
| `cancel_task` | Cancel a running or waiting ticket; child processes are killed |
| `get_project_context` | Read shared memory entries and scanned instruction files |
| `update_project_context` | Write or update one shared project fact/contract |

## CLI

```text
hmesh install [--dry-run] [--yes] [--harness H]... [--uninstall]
hmesh doctor [--harness H] [--json]
hmesh status [--refresh] [--json]
hmesh models [--refresh] [--harness H]
hmesh quota [--probe]
hmesh skills [--query Q]
hmesh dispatch --harness H --objective O [--difficulty D] [--fallback H]...
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
| `summary_max_chars` | `1500` | Summary returned to the controller |
| `diff_preview_max_chars` | `4000` | Diff preview returned to the controller |
| `max_call_depth` | `3` | Nested dispatch depth limit |
| `max_failover_attempts` | `4` | Maximum failovers per ticket |
| `quota_probe_interval_seconds` | `60` | Background recovery probe interval |
| `difficulty_timeout_seconds` | `600/1200/1800/3600` | Per-tier assignment timeout |

## Testing

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src tests
.venv/bin/pytest -v
```

The fast suite uses mocked adapters and temporary Git repositories. `tests/test_local_machine_harnesses.py` additionally exercises the real locally installed harnesses when the macOS bundles are present; it asserts real binary resolution, real model inventories, and a real DeepSeek ticket that creates a file in a worktree and applies it back. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow.

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
- Sensitive filenames such as `.env`, PEM/key files, `credentials*`, `.npmrc` and `.netrc` are excluded from baselines, dirty sync and copy operations.
- Untracked symlinks are recreated only when their resolved target stays inside the source workspace; external and dangling links are skipped.
- Approval bypass flags are suppressed for `shared_write` unless `auto_approve_shared_write: true`; the DeepSeek built-in shell drops credential-shaped environment variables.
- Shared memory is capped at 128 characters per key, 4,000 per entry and 6,000 rendered characters; auto-recorded summaries render in a separate unverified block.
- Mesh Git commands run with hooks disabled and reject apply when `.git/config` or `.git/hooks` changes after task start.

## Trademarks and independence

Harness-Mesh is an independent open-source project. It is not affiliated with, sponsored by, or endorsed by Anthropic, OpenAI, DeepSeek, Google, or ZCode. Claude, Codex, DeepSeek Harness, Google Antigravity, and ZCode are names or trademarks of their respective owners, used here only to describe interoperability. Each harness remains governed by its own licence, acceptable-use policy, and data terms.

## License

MIT. See [LICENSE](LICENSE).
