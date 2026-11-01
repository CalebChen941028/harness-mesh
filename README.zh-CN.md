# Harness-Mesh

一个工单可以依次交给 Claude Code、OpenAI Codex、DeepSeek Harness、Google Antigravity、ZCode 执行。工单绑定一份独立 Git Worktree；单次执行（Assignment）在额度耗尽、模型未授权或凭证失效时换到另一个 Harness，半成品代码和上下文留在原目录。

[English](README.md) · [安全策略](SECURITY.md) · [贡献指南](CONTRIBUTING.md) · [MIT License](LICENSE)

## 产品形态

Harness-Mesh 是本地编排层，交付形态有四个：

- Python 包 `harness_mesh`：类型化数据模型和 SQLite 状态账本；
- 命令行 `hmesh`：查看状态、发现模型、派发任务、管理共享上下文、合入补丁；
- stdio MCP 服务端 `hmesh-mcp`：向主控 Harness 暴露 15 个工具；
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
| 四档难度与按任务类型的市场感知路由，以及技能/插件注入 | 训练或托管模型 |
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
hmesh install --yes                     # 非交互确认后安装
hmesh install --approve read            # 预先批准 9 个只读 MCP 工具
hmesh install --harness zcode           # 只安装一个 Harness（可重复）
hmesh install --workspace /path/project # 显式绑定 MCP 工作区
hmesh install --uninstall               # 卸载集成
```

非 `--dry-run` 时会先打印计划修改的文件和命令并请求确认；非交互环境必须显式传 `--yes`。

### MCP 工具预先批准

交互式会话在调用 MCP 工具前会询问。`codex exec`、`agy -p` 这类无界面命令无法回答，`hmesh install` 因此按工具写入放行规则：

```bash
hmesh install --approve dispatch   # 默认：12 个只读与调度工具
hmesh install --approve read       # 9 个只读工具
hmesh install --approve all        # 全部 15 个
hmesh install --approve none       # 不写入，并清掉本工具写过的规则
```

默认级别 `dispatch` 批准 9 个只读工具，以及 `dispatch_task`、`parallel_dispatch`、`cancel_task`。派发任务在隔离 Worktree 内运行，改动要经过 `apply_task_patch` 才进入主工作区。三个写入工具仍默认人工批准：`apply_task_patch`、`report_quota_status`、`update_project_context` 会改动主工作区或共享状态。确实需要时用 `--approve all` 一并放行。

| Harness | 文件 | 规则 |
| :--- | :--- | :--- |
| Claude Code | `~/.claude/settings.json` | `permissions.allow` 加入 `mcp__harness-mesh__<工具>` |
| Codex | `~/.codex/config.toml` | 托管块内写入 `[mcp_servers.harness-mesh.tools.<工具>]`，值为 `approval_mode = "approve"` |
| Antigravity | `~/.gemini/config/config.json` | `userSettings.globalPermissionGrants.allow` 加入 `mcp(harness-mesh/<工具>)` |
| ZCode、DeepSeek Harness | 无 | Worker 调用 MCP 工具无需批准设置 |

安装器是幂等的。首次改动前写 `<文件>.harness-mesh.bak` 备份，改动经临时文件替换，写后重新解析；校验失败时还原改动前的内容。降低级别或卸载时，只移除 `mcp__harness-mesh__` 或 `mcp(harness-mesh/` 开头的条目，以及 Codex 托管块，其他规则保持原样。`--dry-run` 打印批准数量与每条规则，不落盘。`hmesh doctor` 按默认级别 `dispatch` 报告已批准和缺失的工具，不改任何文件；在 Codex 上，服务级 `default_tools_approval_mode = "approve"` 也算覆盖全部工具。

无界面运行无法回答审批提示。真机实测（2026-11-01）：`codex exec` 以"approval policy is never"拒绝 MCP 调用，服务未获批准时无法执行；`agy -p` 在 `permissions.allow` 没有匹配规则时自动拒绝，例如缺少 `mcp(harness-mesh/list_harnesses)`。上面的按工具规则覆盖所选级别；Codex 的服务级参数 `-c 'mcp_servers.harness-mesh.default_tools_approval_mode="approve"'` 仍可手工使用，它一次批准该服务的全部工具。ZCode 与 DeepSeek Harness 作为 Worker 无需额外配置即可调用，Claude Code 显示该服务已连接。交互式会话照常弹出审批。

ZCode 的 `--scope project` 仍会写入全局状态文件 `~/.zcode/cli/plugins/known_marketplaces.json` 与 `installed_plugins.json`，插件本身和缓存则按项目隔离。`hmesh install --uninstall` 会清除这两处记录；手工清理时对应命令为 `zcode plugins uninstall` 与 `zcode plugins marketplace remove`。

安装后重启对应桌面应用，并新建会话/任务；旧会话可能缓存旧工具列表。安装器是幂等的：重复执行会替换已有注册，不会产生重复 DSH 条目。

## 快速开始

```bash
# 查看 Harness、真实二进制、版本和已授权模型
hmesh status

# 强制执行三层模型发现与档位映射
hmesh models --refresh

# 查看额度熔断器与挂起队列
hmesh quota --probe

# 拉取市场标价与评测分，再预览路由
hmesh market refresh
hmesh market show --unmatched
hmesh route --objective "设计迁移方案" --kind plan

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
hmesh apply <task_id> --strategy copy --force-copy
hmesh doctor
```

MCP 服务端默认启动额度探活、effects 消费与市场刷新；只想要请求/响应时可关闭：

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

## 模型市场与路由

Harness-Mesh 可以按公开标价和评测分给模型排序，不必只依赖四档难度。数据层只读，与派发流程分开。

### 数据来源

| 来源 | 密钥 | 使用的字段 |
| :--- | :--- | :--- |
| OpenRouter `/api/v1/models` | 无 | 每 token 标价、上下文窗口、推理与工具标志 |
| models.dev `/api.json` | 无 | 每百万 token 标价、上下文窗口、发布日期 |
| Epoch AI `benchmark_data.zip` | 无 | 推理与编码基准分位、基准日期 |
| Artificial Analysis `/api/v2/language/models/free` | `ARTIFICIAL_ANALYSIS_API_KEY` | 智能指数、标价、输出速度 |

models.dev 按提供商逐条列出同一个模型，其中包含几百个转售平台。这些条目按模型名折叠成一个规范模型。价格依次取模型原厂的条目（列在 `market.first_party_providers`）、OpenRouter、全部有效报价的中位数。输入与输出都为 0 的条目不当作免费，记为价格未知。

Epoch AI 以 CC BY 4.0 发布 [Capabilities & benchmarking](https://epoch.ai/benchmarks) 数据，不需要密钥，因此是默认的评测来源。每个基准在 540 天参考窗口内折算成一个百分位（`market.epoch.reference_window_days`）；某个轴的有效基准少于三个时留空（`market.epoch.min_benchmarks`）。两条轴分别是 `intelligence_reasoning` 与 `intelligence_coding`，`intelligence` 取模型已有轴的等权平均。配置了 `ARTIFICIAL_ANALYSIS_API_KEY` 时 Artificial Analysis 优先。两个来源都没有时，用现有发现分数、价格分位、推理标志和发布日期算出启发式分数。

启发式分数不是评测分。每个分数都带 `intelligence_source`，取值为 `epoch_ai`、`artificial_analysis`、`override` 或 `heuristic`；`hmesh market show` 与 `get_model_market` MCP 工具都会显示该字段。

Epoch AI 数据采用 CC BY 4.0，需要署名。`hmesh market show` 末尾固定输出 `Benchmark data: Epoch AI, 'Capabilities & benchmarking', https://epoch.ai/benchmarks (CC BY 4.0)`；`--json` 把同一字符串放在 `attribution` 字段，每条路由决策也带该字段。

`hmesh market refresh` 把快照写入 `.harness-mesh/state.db`。默认缓存 6 小时；Epoch AI 与 Artificial Analysis 至少缓存 12 小时，Artificial Analysis 每个进程每天最多请求 20 次。刷新失败保留旧快照并标记 stale；超过 7 天的缓存仍可用，同时标记 expired。

`market.enabled: false` 或 `HARNESS_MESH_OFFLINE=1` 会关闭全部外发请求。

### 自动刷新

缓存不需要手动维护。MCP 服务启动时若某个来源缺失或超过 TTL，会在后台刷新，之后按 TTL 周期刷新。第一次派发如果完全没有市场数据，最多等待 `market.wait_seconds`（8 秒），随后先用本地分档继续，刷新在后台完成。一次性命令（`dispatch`、`route`、`status`、`models`、`market show`）用同样的 8 秒上限同步刷新；来源变慢或失败时回落到旧快照或启发式，只记一条警告。

控制端只发目标，不必点名模型。MCP 的 `dispatch_task` 与 `hmesh dispatch` 都把 `target_harness` 默认成 `auto`，由 Mesh 在所有已安装的 Harness 之间路由并选模型。MCP 的 `initialize` 响应会把这条规则告诉每个客户端。显式传入 `model` 或 `target_harness` 时行为不变。

机器上只装五家中的一两家时，`hmesh status`、`hmesh models` 与 `list_harnesses` 把未安装的 Harness 标成 `not installed` 并给出原因，模型与分档留空；`hmesh doctor` 末尾汇总 `N of 5 harnesses installed`。`hmesh install --dry-run` 对缺少二进制的 Harness 返回 `skipped`；ZCode 既没有 CLI 也没有应用时返回 `ZCode not found`，不写插件文件。

市场分数同时驱动模型发现：Harness 模型匹配到目录后，`capability_score`、`assigned_tiers` 与分档映射改用市场智能分，没有匹配时保持原启发式。`hmesh models`、`hmesh status` 与 `list_harnesses` 会显示每个模型的 `intelligence`、`intelligence_source` 与混合价格，因此用户看到的分档、派发选中的模型和路由预览保持一致。`hmesh doctor` 增加只读的 `market` 一行：数据来源、最近一次成功刷新、是否过期、匹配率与未匹配模型。

### 任务类型与档位

`task_kind` 取 `plan`、`audit`、`review`、`implement`、`refactor`、`debug`、`test`、`docs`、`auto`。`auto` 先读目标文本里的中英文关键词，没有命中就沿用 `evaluate_difficulty`。

| 档位 | 任务类型与难度 | 规则 |
| :--- | :--- | :--- |
| `premium` | `plan`、`audit`、`review`，或 `critical` | 智能分最高；并列时取混合价格低者 |
| `balanced` | `refactor`、`debug`，或 `high` | 在智能分不低于最高值 85% 的候选里，取每美元智能分最高者 |
| `value` | `implement`、`test`、`docs`，或 `low`/`medium` | 在难度下限之上取最便宜者（low 0.55，medium 0.70） |

`blended_price = (3 * input + output) / 4`，单位为美元每百万 token。价格未知的模型不参与价格比较，只在没有别的候选时使用。

`routing.axis_weights` 决定任务类型落在哪条评测轴上：`plan`、`audit`、`review` 用推理 0.7 / 编码 0.3；`implement`、`refactor`、`debug`、`test` 用编码 0.7 / 推理 0.3；`docs` 用推理 1.0。模型只有一条轴时，该轴按全权重计算。

Claude Code 与 Codex 走订阅额度，不按 token 单独计费。路由把 API 标价当作额度消耗的代理值，并在每条决策记录里注明。

### 路由模式

`routing.mode` 默认 `auto`。`manual` 保留你传入的模型；`off` 回到四档难度。`HARNESS_MESH_ROUTING` 覆盖配置默认值，请求里的 `routing_mode` 优先级最高。

`routing.pins` 按任务类型固定 `{harness, model}`。`routing.premium_hints` 与 `routing.value_hints` 给命中的模型加权；`routing.strict_hints: true` 时改为硬过滤。示例配置用精确 glob `glm-5.3`，不会匹配 `glm-5.3-flash`。

没有市场快照、Harness 模型全部匹配不上，或规划器没有给出选择时，派发回落到 `discovery.resolve_model(harness, difficulty)`；决策记录保留原因。

### 决策记录

每个结果都带 `routing` 字段：模式、任务类型、档位、选中的 Harness/模型/推理档、智能分与来源、`axis_scores`（两条轴的分数与权重）、`benchmarks_used`、署名、标价、前三名备选及得分、数据来源、`fetched_at`、`stale`、`expired`、匹配置信度。同一段内容出现在转录开头。

### 未接入的来源

Aider polyglot 排行榜在 2025-10 之后没有新数据；社区维护的 LMArena 快照（`oolong-tea-2026/arena-ai-leaderboards`）从 2026-10-20 起每天返回空的 `leaderboards`；`api.llm-stats.com` 的分数多为厂商自报（`verified: false`），带 AA-Index 的只有 4 个模型，服务条款页返回 404。三个来源都没有接入。

## MCP 工具（15 个）

`list_harnesses`、`discover_harness_models`、`list_skills_and_plugins`、`dispatch_task`、`parallel_dispatch`、`get_task_status`、`read_task_resource`、`get_quota_status`、`report_quota_status`、`apply_task_patch`、`cancel_task`、`get_project_context`、`update_project_context`。

`dispatch_task` 返回的 `git_diff` 是预览，完整内容通过 `read_pointers` 和 `read_task_resource` 分页读取。

## CLI

```text
hmesh install [--dry-run] [--yes] [--approve LEVEL] [--harness H]... [--uninstall]
hmesh doctor [--harness H] [--json]
hmesh status [--refresh] [--json]
hmesh models [--refresh] [--harness H]
hmesh quota [--probe]
hmesh route --objective O [--kind K] [--difficulty D] [--harness H] [--json]
hmesh market refresh [--source S]... [--json]
hmesh market show [--model GLOB] [--axis reasoning|coding] [--unmatched] [--json]
hmesh skills [--query Q]
hmesh dispatch --objective O [--harness H] [--difficulty D] [--fallback H]...
hmesh tasks [--status S]
hmesh read <task_id> [diff|transcript|assignments] [--offset N] [--limit N]
hmesh apply <task_id> [--strategy patch|merge|copy] [--force-copy]
hmesh context list [--category C] [--json]
hmesh context set <key> <content> [--category C] [--author A] [--task-id T]
hmesh init-mcp [--print] [--target PATH]
```

## 配置

模板在 [`config/harnesses.example.yaml`](config/harnesses.example.yaml)。复制到 `.harness-mesh/config.yaml`，或用 `--config` / `HARNESS_MESH_CONFIG` 指定。二进制搜索支持 `~` 与 `*` glob，并按版本号/修改时间取最新；macOS ZCode 自动生成 `.harness-mesh/bin/zcode`。

模板占位符：`{prompt}`、`{system_prompt}`、`{system_prompt_file}`、`{model}`、`{effort}`、`{workdir}`、`{mcp_config}`、`{task_id}`、`{assignment_id}`。空 flag/value 对会自动省略，因此 `<cli_active_default>` 表示使用 Harness 自己的激活模型。

市场与路由设置：

| 配置 | 默认值 | 含义 |
| :--- | :--- | :--- |
| `market.enabled` | `true` | 只读市场数据与快照缓存 |
| `market.ttl_seconds` | `21600` | OpenRouter 与 models.dev 的缓存时长 |
| `market.wait_seconds` | `8` | 自动刷新在派发或命令继续前的最长等待 |
| `market.sources` | 四个来源 | 需要刷新的来源 |
| `market.first_party_providers` | `[]`（示例配置列出十个） | 用自家 models.dev 条目定标价的厂商 |
| `market.epoch.reasoning_benchmarks` | 六个基准 | 推理轴使用的基准 |
| `market.epoch.coding_benchmarks` | 九个基准 | 编码轴使用的基准 |
| `market.epoch.reference_window_days` | `540` | 基准最新发布日期之前的百分位窗口 |
| `market.epoch.min_benchmarks` | `3` | 一条轴至少要有几个有效基准 |
| `market.aliases` | `{}` | 把 Harness 模型 id 或 glob 映射到市场键 |
| `market.overrides` | `{}` | 按 glob 覆盖智能分与价格 |
| `routing.mode` | `auto` | `auto`、`manual` 或 `off`；`HARNESS_MESH_ROUTING` 优先 |
| `routing.floors` | `0.55/0.70` | value 档按难度取的智能分下限 |
| `routing.axis_weights` | 按任务类型 | 给任务打分时推理轴与编码轴的权重 |
| `routing.premium_hints` / `routing.value_hints` | `[]` | 按档位加权的模型 glob |
| `routing.strict_hints` | `false` | 把加权改为硬过滤 |
| `routing.pins` | `{}` | 按任务类型固定 Harness/模型 |

## 测试

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src tests
.venv/bin/pytest -v
```

快速测试使用 Mock 适配器、临时 Git 仓库、`httpx.MockTransport` 市场提供者与缓存快照，不会打开网络连接。真实 HTTP 检查放在 `tests/test_real_machine_market.py`，只有设置 `HARNESS_MESH_REAL_MACHINE=1` 才运行；Artificial Analysis 分支还需要 `ARTIFICIAL_ANALYSIS_API_KEY`。`tests/test_local_machine_harnesses.py` 在本机存在 macOS 应用包时运行真机联调：真实二进制解析、五家模型清单、以及一次真实 DeepSeek 任务在 Worktree 中创建文件并合回主工作区。开发流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 运维与边界

- `.harness-mesh/` 可能包含源码片段和提示词，不要提交或随意共享。
- 审批放行参数只服务于一次性 Worktree，不等同于安全沙箱。五个内置 Harness 默认 `auto_approve: true`；`shared_write` 模式默认不注入放行参数，只有 `auto_approve_shared_write: true` 才注入。请审查 Diff，并遵守厂商条款。
- DeepSeek 内置运行时会从 shell 工具环境中剔除凭据形态的变量，降低误暴露风险；这不构成文件系统或网络沙箱。
- 一次性 `hmesh dispatch --policy wait_and_resume` 只负责挂起；后台探活和 effects 消费运行在常驻的 `hmesh-mcp` 中，也可手动 `hmesh quota --probe`。
- Antigravity SDK 超时只能取消等待，无法强杀第三方线程。
- Codex 各版本接受的审批参数不同。代码内默认注入 `--full-auto`；示例 YAML 针对 codex-cli 0.166 改用 `--dangerously-bypass-approvals-and-sandbox`。可按需调整 `execute_template` 或 `extra.auto_approve_args`。

## 工作区与上下文边界

- MCP 服务与 CLI 要求 Git 工作区；在 `$HOME`、`/` 或非 Git 目录启动会失败。
- 请求级 `workspace_root` 必须先解析 symlink，再确认位于 `allowed_workspace_roots` 内；默认只允许服务启动工作区。
- `.env`、PEM/密钥、`credentials*`、`.npmrc`、`.netrc` 等敏感文件不会进入 baseline、脏工作区同步或复制路径。
- 未追踪 symlink 只在解析后的目标仍位于源工作区内时重建；外部链接与悬空链接跳过。
- `shared_write` 默认不注入审批放行参数，只有 `auto_approve_shared_write: true` 才注入；DSH 内置 shell 会剔除凭据形态的环境变量。
- 共享记忆键上限 128 字符、条目上限 4000 字符、渲染总预算 6000 字符；自动记录摘要进入独立的未验证区块。
- Mesh 发起的 Git 命令禁用 hooks；任务开始后 `.git/config` 或 `.git/hooks` 变化时拒绝 apply。

## 商标与独立性

Harness-Mesh 是独立开源项目，与 Anthropic、OpenAI、DeepSeek、Google、ZCode 无隶属、赞助或背书关系。Claude、Codex、DeepSeek Harness、Google Antigravity、ZCode 是各自权利人的名称或商标，这里仅用于描述互操作。每个 Harness 仍受其自身许可证、可接受使用政策和数据条款约束。

## 许可证

MIT，见 [LICENSE](LICENSE)。
