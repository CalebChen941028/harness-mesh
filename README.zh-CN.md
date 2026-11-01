# Harness-Mesh

一个工单可以依次交给 Claude Code、OpenAI Codex、DeepSeek Harness、Google Antigravity、ZCode 执行。工单绑定一份独立 Git Worktree；单次执行（Assignment）在额度耗尽、模型未授权或凭证失效时换到另一个 Harness，半成品代码和上下文留在原目录。

[English](README.md) · [安全策略](SECURITY.md) · [贡献指南](CONTRIBUTING.md) · [MIT License](LICENSE)

## 产品形态

Harness-Mesh 是本地编排层，交付形态有四个：

- Python 包 `harness_mesh`：类型化数据模型和 SQLite 状态账本；
- 命令行 `hmesh`：查看状态、发现模型、派发任务、管理共享上下文、合入补丁；
- stdio MCP 服务端 `hmesh-mcp`：向主控 Harness 暴露 13 个工具；
- 本地运行目录 `.harness-mesh/`：状态库、Worktree、分页资源。

它不提供模型、不运营云端服务、不带 Web UI 或多租户调度。模型调用由本机已安装并登录的 Harness CLI 完成；Harness-Mesh 只在本地记录状态，并把裁剪后的摘要返回给主控端。

## 适用对象

- 同时使用两个以上编码 Harness，希望用一个任务队列代替手工复制提示词的开发者；
- 需要额度感知路由、Worktree 隔离和可审计执行历史的平台/Agent 基础设施工程师；
- 研究跨 Harness 调度、换棒与共享上下文行为的研究者与评审者。

它不能替代 Harness 自身的权限模型、沙箱或厂商服务条款。使用前应确认你对各 CLI 及其模型拥有合法访问权限。

## 适用范围

| 范围内 | 范围外 |
| :--- | :--- |
| 从本地配置、环境变量、实时模型接口和 CLI 枚举动态发现已授权模型 | 维护固定或托管的模型目录 |
| 四档难度路由（`low` → `critical`）与技能/插件注入 | 训练或托管模型 |
| 额度耗尽、模型未授权、凭证失效、瞬态错误时的 Ticket/Assignment 换棒 | 多租户认证、RBAC、计费 |
| Git Worktree 隔离、脏工作区同步、依赖补丁预应用、显式合入 | 未经审查自动合并 Worktree |
| 项目规范桥接、跨 Harness 记忆黑板、依赖上下文 | 把项目数据上传到远程服务 |
| Claude Code、Codex、DSH、Antigravity、ZCode 适配器 | 超出配置模板去改变厂商 CLI 自身协议 |

## 环境要求

- Python 3.11 及以上；
- 一个或多个已安装并登录的受支持 Harness CLI；
- Git 在 `PATH` 中，用于隔离 Worktree；
- 默认二进制路径针对 macOS 桌面应用；Linux/WSL 可通过配置使用 `PATH` 中的命令行。非 macOS 环境会自动跳过真机测试。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

安装后得到两个入口：`hmesh` 和 `hmesh-mcp`。Antigravity Python SDK 为可选依赖：

```bash
.venv/bin/pip install -e ".[antigravity]"
```

## 一键安装到桌面 Harness

安装 Python 包后，在当前项目目录执行一条命令，即可把 Harness-Mesh 注册到本机已安装的桌面 Harness：

```bash
cd /path/to/your/project
hmesh install
```

安装内容：

| Harness | 集成方式 |
| :--- | :--- |
| Claude Code | `claude mcp add` 注册 user 级 MCP Server |
| OpenAI Codex / ChatGPT 桌面版 | `codex mcp add` 写入 `~/.codex/config.toml` |
| Google Antigravity | `agy mcp add` 写入 `~/.gemini/config/mcp_config.json` |
| ZCode | 生成 MCP 插件与本地插件市场，使用 `zcode plugins` 注册并安装；CLI 不可用时保留市场文件，供 UI 添加 |
| DeepSeek Harness | 向 `~/.dsh/profiles/{desktop,headless}/cordis.patch.yml` 写入 `@deepseek-ai/dsh-mcp-client` 条目（先备份） |

常用参数：

```bash
hmesh install --dry-run                 # 只显示计划执行的命令/文件，不修改任何配置
hmesh install --harness zcode           # 只安装一个 Harness（可重复）
hmesh install --workspace /path/project # 显式绑定 MCP 工作区
hmesh install --uninstall               # 卸载集成
```

安装后重启对应桌面应用，并新建会话/任务；旧会话可能缓存旧工具列表。安装器是幂等的：重复执行会替换已有注册，不会产生重复 DSH 条目。

## 快速开始

```bash
# 查看 Harness、真实二进制、版本和已授权模型
hmesh status

# 强制执行三层模型发现与四档映射
hmesh models --refresh

# 查看额度熔断器与挂起队列
hmesh quota --probe

# 查看本机 SKILL.md 与插件清单
hmesh skills

# 写入并读取共享项目事实
hmesh context set arch.overview "Federated Harness Mesh" --category architecture
hmesh context list

# 打印 MCP 配置
hmesh init-mcp --print

# 派发任务
hmesh dispatch --harness deepseek \
  --objective "在 docs/mesh.md 写一段简短设计说明" \
  --difficulty medium --fallback codex --fallback zcode

# 查看接力记录、分页读取 diff、合入主工作区
hmesh tasks
hmesh read <task_id> diff --limit 4000
hmesh apply <task_id> --strategy patch
```

MCP 服务端默认启动额度探活与 effects 消费；只想要请求/响应时可关闭：

```bash
hmesh-mcp
hmesh-mcp --no-monitor
```

主工作区由 `HARNESS_MESH_WORKSPACE` 指定，配置文件由 `HARNESS_MESH_CONFIG` 指定。

## 五个 Harness 的真实接入

默认值来自本机真实安装位置；路径与命令模板都放在配置里，不在代码里写死模型。

| Harness | 默认二进制搜索 | 非交互协议 | 模型发现来源 |
| :--- | :--- | :--- | :--- |
| Claude Code | `~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude` | `claude -p ... --output-format json --model ... --effort ...`；写入模式加 `--dangerously-skip-permissions`，只读加 `--permission-mode plan`，续接加 `--resume` | `~/.claude/cache/model-catalog/*.json`、`~/.claude.json`、settings、环境变量、`/v1/models` |
| Codex | `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex` 及 plugin app-server 路径 | `codex exec ... --json ...`；写入模式加 `--full-auto`，只读加 `--sandbox read-only`，推理强度走 `-c model_reasoning_effort=...` | `~/.codex/config.toml`、`~/.codex/models_cache.json`、环境变量、`/v1/models` |
| DeepSeek Harness | `/Applications/DeepSeek Harness.app/.../runtime/cli/bin/dsh` | `dsh --profile headless --patch <overlay> --json <prompt>`；自动生成 overlay 恢复本地凭证并合并 desktop 的模型/provider 行 | `~/.dsh/profiles/*/cordis.patch.yml`、环境变量、可选 API、内置 Runtime |
| Antigravity | `/Applications/Gemini.app/Contents/Helpers/agy` | `agy -p ... --output-format json --model ... --effort ...`；写入模式加 `--dangerously-skip-permissions`，只读 `--mode plan`，续接 `--conversation` | `agy models`（清理 `\r` 动画帧）、本地设置、环境变量、Gemini API |
| ZCode | `/Applications/ZCode.app/...` 自动生成 `.harness-mesh/bin/zcode` 包装脚本 | 包装脚本带 `ELECTRON_RUN_AS_NODE=1` 与 provider 配置；`zcode -p ... --cwd ... --json --mode plan|yolo`，续接 `--resume` | `~/.zcode/v2/*.json` 的 `models`、`personalModelIds`、`modelOrder` 等字段 |

`hmesh status` 只有在二进制解析成功且版本命令退出 0 时才返回 `available: true`。模型列表是运行期数据。

## 工作机制

### Ticket、Assignment 与换棒

```text
Ticket task_x（一份 Git Worktree）
  ├── Assignment #1  claude_code/claude-opus-<version>   ended_quota
  ├── Assignment #2  codex/gpt-<version>-<variant>             completed
  └── failover_history: quota_exhausted:rate_limit_429
```

1. `dispatch` 创建 Ticket 与隔离 Worktree。主工作区有未提交修改时，`sync_uncommitted` 把修改和未追踪文件复制进 Worktree，并记录 baseline commit。
2. 路由器按难度档位选择已授权且额度健康的模型，执行一个适配器。
3. 遇到额度耗尽、模型未授权、整个 Harness 未登录或瞬态错误时，记录 `ContinuationCheckpoint`，在同一 Worktree 中创建新 Assignment，换到下一个候选 Harness/模型。
4. Recovery Note 要求接续方先运行 `git status` 和 `git diff`，基于已有半成品继续。最终 Diff 相对 baseline commit 计算，只包含本 Ticket 的净增量。
5. 返回主控的 summary、diff 预览与原始日志都有长度上限；完整内容存为 `diff` / `transcript` / `assignments` 资源，用 `read_task_resource` 分页读取。

额度策略：

- `failover`（默认）：立即换到下一个健康候选；
- `wait_and_resume`：挂起为 `waiting_for_quota`，真实探活成功后自动续派；
- `fail`：直接失败。

熔断器状态为 `CLOSED` / `OPEN` / `HALF_OPEN`。探活会发起一次极短的真实模型调用；本地配置本身不会关闭熔断。真实 Assignment 成功也会关闭。

### 共享项目上下文

- `ProjectContextManager` 扫描 `AGENTS.md`、`CLAUDE.md`、`.harness-mesh/PROJECT_CONTEXT.md`、`.cursorrules`、`.github/copilot-instructions.md`，按字符预算裁剪。
- `update_project_context` 把架构事实、约定、接口契约、决策、坑点写入 SQLite 黑板；后续任务收到 `<project_shared_memory>`。
- `context_files` 会以 `<context_file_previews>` 内联前部内容，减少子 Harness 首轮盲搜。
- `depends_on` 注入上游摘要、修改文件与 diff 到 `<upstream_task_outputs>`，并把上游 diff 预应用进下游 Worktree baseline。
- `auto_record_context=True` 时，完成任务的摘要自动写入共享记忆。

这些区块属于项目数据，不覆盖 Ticket 目标或安全策略。

### 会话与权限

`TaskRequest.session_id` 会透传到 MCP 和 `AdapterCall`。适配器将其映射为 Claude/ZCode `--resume`、`codex exec resume <id>`、DSH `--session-id`、Antigravity `--conversation`。写入模式自动注入各 CLI 的审批放行参数；只读 Ticket 使用 plan/read-only 模式并在一次性 Worktree 中执行。

## MCP 工具（13 个）

`list_harnesses`、`discover_harness_models`、`list_skills_and_plugins`、`dispatch_task`、`parallel_dispatch`、`get_task_status`、`read_task_resource`、`get_quota_status`、`report_quota_status`、`apply_task_patch`、`cancel_task`、`get_project_context`、`update_project_context`。

`dispatch_task` 返回的 `git_diff` 是预览，完整内容通过 `read_pointers` 和 `read_task_resource` 分页读取。

## CLI

```text
hmesh install [--dry-run] [--harness H]... [--uninstall]
hmesh status [--refresh] [--json]
hmesh models [--refresh] [--harness H]
hmesh quota [--probe]
hmesh skills [--query Q]
hmesh dispatch --harness H --objective O [--difficulty D] [--fallback H]...
hmesh tasks [--status S]
hmesh read <task_id> [diff|transcript|assignments] [--offset N] [--limit N]
hmesh apply <task_id> [--strategy patch|merge|copy]
hmesh context list [--category C] [--json]
hmesh context set <key> <content> [--category C] [--author A] [--task-id T]
hmesh init-mcp [--print] [--target PATH]
```

## 配置

模板在 [`config/harnesses.example.yaml`](config/harnesses.example.yaml)。复制到 `.harness-mesh/config.yaml`，或用 `--config` / `HARNESS_MESH_CONFIG` 指定。二进制搜索支持 `~` 与 `*` glob，并按版本号/修改时间取最新；macOS ZCode 自动生成 `.harness-mesh/bin/zcode`。

模板占位符：`{prompt}`、`{system_prompt}`、`{system_prompt_file}`、`{model}`、`{effort}`、`{workdir}`、`{mcp_config}`、`{task_id}`、`{assignment_id}`。空 flag/value 对会自动省略，因此 `<cli_active_default>` 表示使用 Harness 自己的激活模型。

## 测试

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src tests
.venv/bin/pytest -v
```

快速测试使用 Mock 适配器与临时 Git 仓库。`tests/test_local_machine_harnesses.py` 在本机存在 macOS 应用包时运行真机联调：真实二进制解析、五家模型清单、以及一次真实 DeepSeek 任务在 Worktree 中创建文件并合回主工作区。开发流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 运维与边界

- `.harness-mesh/` 可能包含源码片段和提示词，不要提交或随意共享。
- 审批放行参数只服务于一次性 Worktree，不等同于安全沙箱；请使用隔离模式、审查 Diff，并遵守厂商条款。
- 一次性 `hmesh dispatch --policy wait_and_resume` 只负责挂起；后台探活和 effects 消费运行在常驻的 `hmesh-mcp` 中，也可手动 `hmesh quota --probe`。
- Antigravity SDK 超时只能取消等待，无法强杀第三方线程。
- Codex 各版本接受的审批参数不同。代码内默认注入 `--full-auto`；示例 YAML 针对 codex-cli 0.166 改用 `--dangerously-bypass-approvals-and-sandbox`。可按需调整 `execute_template` 或 `extra.auto_approve_args`。

## 商标与独立性

Harness-Mesh 是独立开源项目，与 Anthropic、OpenAI、DeepSeek、Google、ZCode 无隶属、赞助或背书关系。Claude、Codex、DeepSeek Harness、Google Antigravity、ZCode 是各自权利人的名称或商标，这里仅用于描述互操作。每个 Harness 仍受其自身许可证、可接受使用政策和数据条款约束。

## 许可证

MIT，见 [LICENSE](LICENSE)。
