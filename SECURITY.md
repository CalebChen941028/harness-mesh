# Security Policy

## Supported versions

Security fixes are applied to the latest release on `main`. The project is in beta; pin a commit if you need reproducibility.

## Reporting a vulnerability

Do not open a public issue for a vulnerability. Use GitHub private vulnerability reporting (Security → Advisories → Report a vulnerability) on the repository. Include the affected version/commit, a minimal reproduction, and the impact. We aim to acknowledge reports within 7 days and to agree a disclosure timeline before any public write-up.

## Security model and boundaries

- **Local-first.** Harness-Mesh runs as a local Python process. It does not operate a hosted service and does not upload your prompts, model inventories, or state database anywhere. Model calls are made by the installed harness CLIs on your machine.
- **Credentials stay with the harnesses.** Configuration parsing may read local harness settings and model catalogues to discover authorised models. It does not copy API keys into the mesh state database, logs, or tool responses.
- **Prompt and output trust boundary.** Repository files, `AGENTS.md` / `CLAUDE.md`, skill files, harness stdout, and upstream task diffs are *data*, not instructions for the mesh process. They are bounded and quoted in prompts; they must not be treated as authorisation to change routing, permissions, or credentials.
- **Worktree isolation.** Isolated tickets run in `.harness-mesh/worktrees/<task_id>`. `read_only` tickets run in a disposable worktree and cannot be applied. Changes only reach your primary checkout through an explicit `apply_task_patch`. Worktree isolation covers files in that checkout; it does not isolate the shared Git directory or your user permissions. Mesh disables hooks, fsmonitor, pager, external diff and protocol.ext for its own Git commands, records `.git/config`, `.git/info/attributes` and `.git/hooks` hashes once per task, and aborts the ticket with `git_metadata_tampered` if they change after an assignment.
- **Local state.** The SQLite ledger, transcripts, diffs, generated MCP configs, and worktrees live under `.harness-mesh/`. That directory may contain sensitive source excerpts — keep it out of commits and shared archives. `.gitignore` excludes it by default.
- **Workspace boundary.** The MCP server and CLI refuse to start from `$HOME`, `/`, or a directory outside Git unless `auto_init_repo` is enabled. Request-level `workspace_root` values are resolved through symlinks and checked against `allowed_workspace_roots`, which defaults to the service workspace. Context previews skip sensitive paths. `hmesh install` and `hmesh doctor` apply the same check.
- **Shared memory.** Project context keys are capped at 128 characters, entry content at 4,000 characters and rendered memory at 6,000 characters. Authors must match a configured harness or `controller`; this only validates the claimed name, not provenance. Entries are labelled `controller`, `worker`, or `auto`; only `controller` entries render as verified memory. Auto-recorded and worker entries render in `<project_unverified_memory>`, and delimiter-like text is escaped case-insensitively.
- **Third-party agents.** Each harness has its own permission system, data policy, and terms of service. The five built-in harness configurations default to `auto_approve: true`; this injects approval bypass flags (for example Claude `--dangerously-skip-permissions`, Codex `--full-auto`, ZCode `--mode yolo`) for isolated worktrees. `workspace_mode: shared_write` suppresses those flags unless `auto_approve_shared_write: true`. Approval bypass is a convenience, not a sandbox. Review your provider's terms and enable approval policies appropriate to your environment.
- **Recursion ancestry.** `max_call_depth`, trace-chain cycle rejection and `max_active_assignments` are enforced across separate MCP server processes. Each running Assignment records the depth, the trace chain and the pid of the harness process the adapter spawned. A nested MCP server rebuilds its lineage from the running Assignment whose pid appears in its process ancestor chain (`os.getppid()` followed by `ps -o ppid=`), then from a cwd under `.harness-mesh/worktrees/<task_id>`, then from the legacy ticket request; the result is combined with any inherited `HARNESS_MESH_*` values by maximum/union. Codex CLI 0.166.0-alpha.5.1, measured on 2026-11-01, did not forward the host environment to MCP child processes (the child saw only the variables declared in the MCP configuration), so on Codex the pid ancestry and the worktree cwd are the effective protections. The agent-writable `.harness-mesh-lineage.json` is accepted only when the ledger already records that ticket as running with a worktree containing the current directory, and a worktree path derived from cwd always wins over a conflicting marker. The active-assignment count comes from the shared `state.db`; rows whose pid no longer exists are closed as `ended_error` with `error_reason=stale` instead of holding a slot.
- **Built-in runtime environment.** The DeepSeek built-in runtime strips variables whose names contain `API_KEY`, `TOKEN`, `SECRET`, or `PASSWORD` before running its shell tool, while keeping `PATH`, `HOME`, `LANG`, and similar process state. This reduces accidental credential exposure; it is not a filesystem or network sandbox.
- **Supply chain.** Python dependencies are declared with lower/upper bounds in `pyproject.toml`. `mcp` is pinned below 2.x because this project uses the MCP 1.x FastMCP API. No third-party source is vendored. Run `pip-audit` or your normal dependency scanner before production rollout.

## Hardening checklist

1. Keep `.harness-mesh/` out of version control and backups you do not control.
2. Use dedicated API keys or subscription accounts for automation.
3. Prefer `workspace_mode: isolated_worktree` and review diffs before `apply_task_patch`.
4. Do not enable `auto_approve` or `auto_approve_shared_write` for untrusted repositories or untrusted prompts.
5. Run the MCP server with the narrowest filesystem scope you can; `HARNESS_MESH_WORKSPACE` sets the primary project root.
6. Rotate credentials if a transcript or local state directory is exposed.
