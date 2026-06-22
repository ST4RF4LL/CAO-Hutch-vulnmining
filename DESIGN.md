

  - CAO 是底层运行时：负责注册并执行原生 flow，启动 CLI Agent、tmux/session、通信和生命周期。
  - Rabbit Hutch（兔笼）是外挂控制层和编译器：负责 workflow/Agent 定义生成、持久化、证据链和断点恢复；实时 Agent 调度必须通过 CAO 执行。
  - Orchestrator 只是确定性状态机，不参与漏洞判断。
  - Agent 通过 inbox/outbox 文件协议通信，不把对话或 LLM 记忆作为事实源。
  - 每次 Run 都保存完整任务图、状态、事件、findings、evidence 和 Agent Cell。
  - Agent Factory 根据任务、技术栈、skills、MCP 和权限生成独立 Agent Cell。
  - OpenCode Agent Cell 使用 `runs/<run>/agents/<agent>/workspace`，其中保存 Cell 本地 `.opencode/agents`、`.opencode/skills` 和技能白名单；`shared`、`inbox`、`outbox`、`artifacts`、`tmp` 通过相对软链接入 Run 协议。
  - workflow 只声明源 skill 名称。Hutch 将其复制到 Cell 并编译成稳定的 Cell 专属运行时名称，避免 OpenCode 全局同名 skill 抢占。CAO 安装 profile 后，Hutch 只重写 CAO 管理的 OpenCode agent 运行时文件以落实同一白名单，不修改 CAO 仓库。
  - skill 白名单限制 OpenCode `skill` 工具调用，但不是文件系统安全边界；跨 Cell 文件隔离仍需容器、独立用户或挂载命名空间。
  - CAO 只能通过 AgentRuntime/CaoRuntime 适配器接入，避免业务层直接操作 tmux 或 CAO 内部数据库。Dashboard 的实时终端同样通过 CAO terminal API 读取 pane 和发送输入；tmux 窗口结束后退化为只读 snapshot/scrollback。
  - 第一阶段只做最小闭环：生成 profiles/flow → CAO 注册并启动 flow → supervisor 通过 Hutch 的受约束 CAO API launcher 创建 worker → 收集结果 → 更新状态 → resume。launcher 从 task contract 强制使用 Agent Cell workspace，避免 CAO MCP 可选 working-directory 开关关闭时静默继承 supervisor 目录。
  - Finding 是核心业务资产，Evidence 是判断与审计依据。
  - 动态 Agent 从“后续能力”提升为当前必要能力：Hutch 根据仓库规模和威胁情报生成有界的 Agent Cell 和审计 DAG；Finding Pipeline、worktree、插件继续作为后续扩展。

  当前设计进入实现前需要收敛的几个点：

  1. 两套 workflow 表达方式（stages 和扁平 tasks）需要统一。
  2. state.json、SQLite、JSONL 必须明确唯一事实源和派生关系。
  3. inbox/outbox 消费需要幂等、原子写入及任务租约，否则恢复时可能重复执行。
  4. permissions.yaml 只是声明，必须由 CAO、容器或系统沙箱真正执行。
  5. CAO 原生 flow 当前是“定时启动一个 Agent session”，没有 DAG stage 模型；Rabbit Hutch 将 DAG 编译进 supervisor 协议，并把阶段状态保存在 Run 数据中。
  6. CAO 只展示存活的 tmux session，清理后的 Agent terminal 不再通过 Web/API 可见；Rabbit Hutch Dashboard 对存活 terminal 提供 tmux-backed 实时查看和受控输入，对已清理 terminal 从 Run 证据与 CAO terminal snapshot 构建只读历史视图。Dashboard 的“CAO 执行”只白名单开放 `cao flow run` 和结构化 `cao launch`，不提供任意 shell。

  总体原则已经明确：CAO 必须看得见 flow 和所有 Agent 运行，兔笼管生成与安全任务状态；CAO 可替换，Run 数据不可丢。

## 自适应源码审计 Campaign

大型基础框架仓库不能继续用“一个 Agent 审计整仓”的非结构化任务。模型可以输出 50 个模块，却只审计前 5 个高风险模块，而当前系统无法区分“已完成”和“漏审”。新的一次完整审计是一个 Campaign，由三个 CAO 可见 Flow 组成：

1. **信息收集与威胁建模 Flow**
   - Hutch 先对不可变源码快照执行确定性扫描，产生 `repository-inventory.json` 和 `modules.json`。
   - 模块记录稳定 ID、路径、语言、文件数、字节数和构建描述符。模块边界优先来自 Maven/Gradle 构建文件和 `src/main`，无法归属的源码必须进入根模块，不得丢失。
   - 情报 Agent 以模块清单为边界，输出架构、业务流、外部接口、攻击面、信任边界和威胁模型。文档用于人阅读，JSON 用于后续 Flow 的机器校验。

2. **审计计划 Flow**
   - 计划 Agent 读取第一阶段情报，输出 `audit-plan.json`，决定 `whole_repo`、`sharded` 或 `hybrid` 策略，并为每个分片绑定模块 ID、路径、威胁 ID、专业技能和工作量。
   - Agent 只提交声明式计划，不能直接构造 shell、CAO profile 或运行任意代码。Hutch 对 schema、路径、模块归属、技能白名单、并发上限和全量覆盖做确定性校验。Agent 请求的并发数超限时，Hutch 只向下钳制并记录 normalization，不因为纯调度参数重做计划。
   - Hutch 固定限制单任务的模块数和源文件数，计划 Agent 不能自行放大上限。超大单模块可单独成为任务，但不能再与其他模块合并。
   - 计划未覆盖任一模块时不得编译为执行 Flow。

3. **审计与挖掘 Flow**
   - Hutch 将通过校验的计划编译为 `hutch.cao-workflow.v1`，每个分片对应独立 Agent Cell、受限路径、明确交付物和 CAO 可见 terminal。
   - 无依赖的分片按 `max_concurrency` 有界并发；高风险模块可由多个不同专长 Agent 重叠审计，但覆盖计算仍以模块 ID 为准。
   - 每个挖掘 Agent 除报告与 findings 外，必须提交 `coverage.json`，按模块声明 `audited`、`deferred` 或 `failed`。`audited` 必须包含实际审阅文件数和位于模块路径内的源码证据；`deferred` 必须给出理由；Agent 不得声明任务合约以外的模块。
   - Coverage Gate 合并所有分片结果。未被声明、失败或无理由延后的模块必须进入补充任务；仍有缺口时 Flow 不得完成。
   - 所有分片完成后，验证 Agent 去重、评估可利用性和证据强度；报告 Agent 聚合分片报告、验证结果、覆盖缺口和限制，产生唯一完整报告。

### 数据契约与完成条件

- `hutch.repository-inventory.v1`：仓库文件和语言规模，以及生成该清单的快照信息。
- `hutch.module-inventory.v1`：不重复的稳定模块 ID 与路径边界。
- `hutch.audit-plan.v1`：策略、有界并发数和分片任务。其模块并集必须等于模块清单。
- `hutch.coverage.v1`：单个审计任务对合约内模块的逐项处理状态。
- `hutch.coverage-summary.v1`：全部分片合并结果和 gap 列表。
- `hutch.campaign.v1`：串联 `recon`、`planning`、`mining` 三个 Flow Run，记录父 Run、情报 Run 和计划 Run。

Dashboard 将同一 campaign lineage 聚合为一个只读“总 Flow”实例：以情报 Run 为根，CAO 子 Flow 为节点，经过校验的阶段交接为 handoff 边，并汇总状态、Stage、Agent、报告与全部持久化产物。聚合不替代或隐藏子 Flow；每个子 Flow 继续保留独立的 CAO session、Agent 图、终端、产物和删除生命周期。同名 campaign 的不同根 Run 或不同目标仓库必须形成不同总 Flow，避免跨批次误合并。

Campaign 只在以下条件全部成立时可标记完成：情报产物通过 schema 校验；计划覆盖全部模块；每个执行 Agent 有 CAO session/terminal 证据；Coverage Gate 不存在 gap；最终报告明示审计覆盖、未解决限制和经过验证的 findings。

### 调度边界

Hutch 是计划编译器、确定性调度器和完成门禁；CAO 是所有 supervisor 和 worker Agent 的唯一运行时。Hutch 不直接调用模型，也不允许计划 Agent 扩大文件系统、网络或命令权限。断点恢复以任务合约、原子结果文件和已校验状态为依据，而不是 terminal 是否仍存活。

## CLI 操纵面与 Agent 外壳

Hutch 对人和上层 CLI Agent 提供同一套 `bin/hutch` 操纵面。Codex 与 OpenCode 只是不同的意图理解和配置外壳，不各自实现调度逻辑，也不直接调用 tmux 或 CAO 数据库。

`bin/hutch-mcp` 将同一结构化 HTTP 控制面暴露为 MCP stdio 工具，供 Codex、OpenCode 或其他 MCP Client 使用。MCP 默认只返回产物元数据，产物正文必须按 Run ID 与精确路径单独读取，避免把完整 Campaign 的全部报告一次性灌入模型上下文。MCP 不开放任意 shell、直接 tmux、CAO 数据库、Run 状态改写、证据删除或任意 workflow 编译；高层构建工具只能生成固定的 recon/planning/mining 合约，且推进前强制检查上游 Run 已完成。启动、停止和调度开关仍由 Hutch API 执行。

- 项目操作：`project open/list/info` 注册应用根目录并读取自适应微服务树。
- Flow 定义操作：`flow catalog/start/enable/disable` 通过 Hutch 的结构化 API 控制 CAO 原生 Flow。
- Flow 实例操作：`flow list/info/stop` 读取持久化 Run；停止操作先删除整条 CAO session，再将 Run 原子标记为 `stopped`，保留中断 stage 和事件证据。
- Agent 定制：`agent list/info/import-opencode` 将 OpenCode Agent 配置转换为 CAO profile 源文件。
- Flow 定制：`flow compile` 将 `hutch.cao-workflow.v1` 编译为 CAO bundle；默认只生成，显式 `--install` 才安装，显式 `--enable` 才启用定时调度。

Codex 通过项目级 `AGENTS.md`、`.codex/config.toml` 和 `qu-orchestrator` skill 接入；OpenCode 通过名为 `QU` 的 primary agent 和权限配置接入。两套外壳共享同一命令契约和 JSON 输出，因此人工操作与 Agent 操作具有相同的可观察行为和审计边界。

QU 的名称灵感来自《所有的，明天》。它不仅操作既有资源，还必须能够为任务构造新的 Agent 和 Flow。外部开源 Agent/Skill 先经 `qu-construct-agent` 执行发现、选择、许可检查、权限降级和 provenance 固化，再生成 CAO profile。导入器默认只授予 `fs_read`/`fs_list`；写文件、Shell 和 supervisor/CAO MCP 均需显式开启，外部 MCP、凭证、Hook 和模型配置不自动继承。每次导入产生 `hutch.agent-import.v1` manifest，记录来源哈希、许可证、角色、工具、保留的 Skill 资源和所有降级警告。
