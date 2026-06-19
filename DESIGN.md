

  - CAO 是底层运行时：负责启动 CLI Agent、tmux/session、通信和生命周期。
  - Rabbit Hutch（兔笼）是外挂控制层：负责 workflow、Agent 拼装、任务调度、持久化、证据链和断点恢复。
  - Orchestrator 只是确定性状态机，不参与漏洞判断。
  - Agent 通过 inbox/outbox 文件协议通信，不把对话或 LLM 记忆作为事实源。
  - 每次 Run 都保存完整任务图、状态、事件、findings、evidence 和 Agent Cell。
  - Agent Factory 根据任务、技术栈、skills、MCP 和权限生成独立 Agent Cell。
  - CAO 只能通过 AgentRuntime/CaoRuntime 适配器接入，避免业务层依赖 tmux 或 CAO 内部实现。
  - 第一阶段只做最小闭环：run → 创建 Cell → 启动 CAO Agent → 投递任务 → 收集结果 → 更新状态 → resume。
  - Finding 是核心业务资产，Evidence 是判断与审计依据。
  - 后续才扩展动态 Agent、Finding Pipeline、worktree、插件和 UI。

  当前设计进入实现前需要收敛的几个点：

  1. 两套 workflow 表达方式（stages 和扁平 tasks）需要统一。
  2. state.json、SQLite、JSONL 必须明确唯一事实源和派生关系。
  3. inbox/outbox 消费需要幂等、原子写入及任务租约，否则恢复时可能重复执行。
  4. permissions.yaml 只是声明，必须由 CAO、容器或系统沙箱真正执行。
  5. CAO 的实际 CLI/session 接口需要验证，文档中的调用目前属于适配层设想。

  总体原则已经明确：CAO 管 Agent 运行，兔笼管安全任务状态；CAO 可替换，Run 数据不可丢。