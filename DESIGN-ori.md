下面给你一版 **CAO 外挂“兔笼 Rabbit Hutch”设计方案**。定位是：**不 fork CAO，不改 CAO 核心，把 CAO 当 CLI Agent Runtime；兔笼负责渗透测试 workflow、Agent 拼装、状态持久化、证据链和断点恢复。**

CAO 目前本身适合承担底层 runtime：它支持把 Claude Code、Codex CLI、Gemini CLI、OpenCode、Amazon Q 等 CLI 作为 agent 运行，并通过 tmux session 隔离 agent，再用 supervisor-worker / MCP 模式做协调。([GitHub][1]) 另外，CAO release 信息里已经出现了 session CLI、Web UI、agent profile provider override、persistent agent memory 等方向，说明它越来越适合作为被外挂层调用的 agent runtime，而不是需要你直接二开的业务框架。([GitHub][2])

# CAO 外挂“兔笼”设计方案

## 1. 项目定位

**Rabbit Hutch，兔笼** 是 CAO 的安全任务外挂层。

它不替代 CAO，而是在 CAO 上方增加一层：

```text
CAO = 兔子运行时
Rabbit Hutch = 兔笼、饲料、任务卡、观察记录、繁殖/拼装规则
```

也就是：

```text
CAO 负责：
- 启动 opencode / codex / Claude Code / Gemini CLI 等 agent
- 管理 tmux session
- 提供 MCP / session 通信能力
- 支持多 agent 并发、串行或 swarm

Rabbit Hutch 负责：
- 渗透测试 workflow
- Agent profile 拼装
- AGENTS.md / skills / mcp.json 生成
- 任务分发协议
- agent 间文件化通信
- findings / evidence / artifacts 持久化
- checkpoint / resume
- 根据反馈动态生成新 agent
```

一句话：**CAO 管“活着的 agent”，兔笼管“任务世界”。**

---

## 2. 设计目标

### 核心目标

```text
1. 将 opencode、codex 等 CLI 作为独立 Agent instance 管理
2. 每个 Agent 有独立工作目录、上下文、技能和 MCP 配置
3. Agent 间不依赖纯对话传递，而是通过持久化文件协议交互
4. workflow 可中断、可恢复、可审计、可回放
5. orchestrator 不直接参与漏洞分析，只负责 workflow 状态推进
6. 支持根据任务目标、项目类型、反馈结果动态生成新 Agent
7. 后续可以平滑演进成 git worktree / workspace per agent 模式
```

### 非目标

```text
1. 不做新的 LLM agent 框架
2. 不替代 CAO 的 tmux/session/provider 能力
3. 不把 pentest 业务逻辑写进 CAO fork
4. 不依赖 agent 间自由聊天作为唯一状态源
5. 不让 supervisor LLM 自己决定全部流程
```

---

## 3. 总体架构

```text
┌─────────────────────────────────────────────┐
│ Rabbit Hutch CLI / API                       │
│ hutch run / hutch resume / hutch status      │
└─────────────────────────────────────────────┘
                    │
┌─────────────────────────────────────────────┐
│ Workflow Engine                              │
│ workflow.yaml / task graph / state machine   │
└─────────────────────────────────────────────┘
                    │
┌─────────────────────────────────────────────┐
│ Agent Factory                                │
│ AGENTS.md / skills / mcp.json / permissions  │
└─────────────────────────────────────────────┘
                    │
┌─────────────────────────────────────────────┐
│ Durable Communication Layer                  │
│ inbox / outbox / findings / artifacts        │
└─────────────────────────────────────────────┘
                    │
┌─────────────────────────────────────────────┐
│ CAO Adapter                                  │
│ cao session / cao launch / MCP / tmux        │
└─────────────────────────────────────────────┘
                    │
┌─────────────────────────────────────────────┐
│ CAO Runtime                                  │
│ opencode / codex / claude-code / gemini      │
└─────────────────────────────────────────────┘
```

---

## 4. 核心概念

### 4.1 Run

一次完整安全任务叫一个 **Run**。

例如：

```text
runs/
  2026-06-19-java-agent-audit/
```

每个 Run 是完整可恢复的任务空间。

```text
runs/<run-id>/
  run.yaml
  workflow.yaml
  state.json
  task_graph.json
  agents/
  shared/
  artifacts/
  findings.jsonl
  evidence/
  logs/
  decisions.md
```

其中：

```text
run.yaml        本次任务基本信息
workflow.yaml   本次执行的 workflow 快照
state.json      当前状态
task_graph.json 任务 DAG
agents/         每个 agent 的独立空间
shared/         共享上下文
artifacts/      产物
findings.jsonl  漏洞发现结构化记录
evidence/       证据材料
logs/           运行日志
decisions.md    orchestrator 的决策记录
```

---

### 4.2 Agent Cell

每个 agent 在兔笼里对应一个 **Cell**。

```text
runs/<run-id>/agents/java-static-audit-agent/
  profile.yaml
  AGENTS.md
  skills/
  mcp.json
  permissions.yaml
  inbox/
  outbox/
  workspace/
  artifacts/
  memory/
  status.json
  transcript.md
```

含义：

```text
profile.yaml     Agent 身份和能力描述
AGENTS.md        给 CLI agent 的系统/项目指令
skills/          该 agent 可用 skill
mcp.json         该 agent 的 MCP 配置
permissions.yaml 工具权限配置
inbox/           收到的任务
outbox/          输出的结果
workspace/       该 agent 的工作区
artifacts/       该 agent 产物
memory/          局部记忆
status.json      当前状态
transcript.md    重要交互摘要
```

---

### 4.3 Durable Message

agent 间不直接靠聊天传递，而是靠文件化消息。

```text
agents/<agent>/inbox/T-0003.task.json
agents/<agent>/outbox/T-0003.result.json
```

任务消息：

```json
{
  "schema": "hutch.task.v1",
  "task_id": "T-0003",
  "type": "static_audit",
  "target": {
    "repo": "/workspace/project",
    "scope": ["service-a", "service-b"]
  },
  "objective": "Find authorization bypass candidates in Java service APIs.",
  "inputs": [
    "shared/openapi/index.json",
    "shared/code-map/routes.json"
  ],
  "expected_outputs": [
    "findings",
    "evidence",
    "next_tasks"
  ],
  "constraints": {
    "no_code_modification": true,
    "authorized_scope_only": true
  }
}
```

结果消息：

```json
{
  "schema": "hutch.result.v1",
  "task_id": "T-0003",
  "agent": "java-static-audit-agent",
  "status": "done",
  "summary": "Found 3 candidate authorization bypass issues.",
  "findings": [
    {
      "finding_id": "F-0007",
      "type": "authorization-bypass",
      "severity": "medium",
      "confidence": "low",
      "evidence": [
        "evidence/F-0007/call-chain.md",
        "evidence/F-0007/source-snippets.json"
      ],
      "recommended_next_agent": "evidence-review-agent"
    }
  ],
  "artifacts": [
    "artifacts/T-0003/routes-to-handlers.json"
  ],
  "next_tasks": [
    {
      "type": "evidence_review",
      "target_finding": "F-0007"
    }
  ]
}
```

---

## 5. 模块设计

## 5.1 Hutch CLI

命令行入口。

```bash
hutch init
hutch run workflows/java-whitebox-audit.yaml --target /repo/project
hutch status <run-id>
hutch resume <run-id>
hutch pause <run-id>
hutch attach <run-id> java-static-audit-agent
hutch findings <run-id>
hutch report <run-id>
```

### MVP 命令

```bash
hutch run
hutch status
hutch resume
hutch attach
```

### 后续命令

```bash
hutch spawn-agent
hutch replay-task
hutch export-report
hutch diff-run
hutch gc
```

---

## 5.2 Workflow Engine

负责读取 `workflow.yaml`，生成任务图，并推进状态。

示例：

```yaml
name: java-whitebox-audit
version: 0.1

target:
  type: source_repo
  language: java

agents:
  - id: repo-map-agent
    profile: repo-map
    provider: codex

  - id: openapi-map-agent
    profile: openapi-map
    provider: opencode

  - id: java-static-audit-agent
    profile: java-static-audit
    provider: opencode

  - id: evidence-review-agent
    profile: evidence-review
    provider: codex

  - id: report-agent
    profile: report
    provider: codex

stages:
  - id: collect-context
    tasks:
      - id: T-0001
        agent: repo-map-agent
        type: repo_map
      - id: T-0002
        agent: openapi-map-agent
        type: api_map

  - id: static-audit
    depends_on:
      - collect-context
    tasks:
      - id: T-0003
        agent: java-static-audit-agent
        type: static_audit

  - id: evidence-review
    depends_on:
      - static-audit
    dynamic: true
    spawn_from: findings

  - id: report
    depends_on:
      - evidence-review
    tasks:
      - id: T-9999
        agent: report-agent
        type: final_report
```

Workflow Engine 不做漏洞分析，只做：

```text
1. 判断依赖是否完成
2. 投递任务
3. 读取结果
4. 更新状态
5. 根据结果生成后续任务
6. 必要时调用 Agent Factory 生成新 Agent
```

---

## 5.3 Agent Factory

Agent Factory 是兔笼最重要的模块之一。

它负责把：

```text
任务目标
项目类型
agent profile
skills 集合
mcp 集合
权限策略
历史反馈
```

拼装成一个可运行的 CLI agent cell。

输入：

```yaml
agent:
  id: java-static-audit-agent
  base_profile: java-static-audit
  provider: opencode
  target:
    language: java
    framework: spring-like
    api_definition: openapi-yaml
  skills:
    include:
      - java-code-audit
      - taint-analysis
      - openapi-route-mapping
    exclude:
      - exploit-generation
  mcp:
    include:
      - filesystem
      - ripgrep
      - joern
  permissions:
    allow:
      - read_file
      - search
      - shell_readonly
    deny:
      - network_scan
      - destructive_write
```

输出：

```text
agents/java-static-audit-agent/
  AGENTS.md
  skills/
  mcp.json
  permissions.yaml
  profile.yaml
```

### AGENTS.md 模板

```markdown
# Role

You are java-static-audit-agent.

# Mission

Analyze the assigned Java source code for security-relevant control-flow and data-flow issues.

# Boundaries

- Work only within the authorized repository path.
- Do not modify source code.
- Do not perform external network actions.
- Write all structured outputs to outbox/.
- Save evidence under artifacts/ and evidence/.

# Input Protocol

Read task files from inbox/*.task.json.

# Output Protocol

For each task, write:

- outbox/<task_id>.result.json
- artifacts/<task_id>/
- evidence/<finding_id>/
```

---

## 5.4 CAO Adapter

CAO Adapter 是兔笼和 CAO 的边界。

它只做 runtime 操作，不关心漏洞逻辑。

接口：

```python
class AgentRuntime:
    def create_agent(self, profile: AgentProfile) -> AgentHandle:
        ...

    def send_task(self, agent_id: str, task: Task) -> None:
        ...

    def read_status(self, agent_id: str) -> AgentStatus:
        ...

    def attach(self, agent_id: str) -> None:
        ...

    def stop(self, agent_id: str) -> None:
        ...

    def resume(self, agent_id: str) -> None:
        ...
```

CAO 实现：

```python
class CaoRuntime(AgentRuntime):
    def create_agent(self, profile):
        # 生成 CAO agent profile
        # 调用 cao launch / cao session
        pass

    def send_task(self, agent_id, task):
        # 写入 inbox
        # 通过 CAO session/MCP 通知 agent 读取任务
        pass
```

设计原则：

```text
Rabbit Hutch 不直接依赖 tmux。
Rabbit Hutch 只依赖 CaoRuntime。
如果以后不用 CAO，只换 runtime adapter。
```

---

## 5.5 Durable State Store

建议 MVP 用文件 + SQLite。

```text
state.json        人可读状态快照
hutch.db          查询和索引用
findings.jsonl    漏洞发现流
events.jsonl      事件流
```

事件流示例：

```json
{"ts":"2026-06-19T10:00:00+09:00","event":"run_started","run_id":"R-001"}
{"ts":"2026-06-19T10:01:00+09:00","event":"agent_spawned","agent":"java-static-audit-agent"}
{"ts":"2026-06-19T10:02:00+09:00","event":"task_assigned","task_id":"T-0003","agent":"java-static-audit-agent"}
{"ts":"2026-06-19T10:30:00+09:00","event":"finding_created","finding_id":"F-0007"}
```

`state.json` 示例：

```json
{
  "run_id": "R-001",
  "status": "running",
  "current_stage": "static-audit",
  "agents": {
    "java-static-audit-agent": {
      "status": "running",
      "current_task": "T-0003",
      "runtime": "cao",
      "provider": "opencode"
    }
  },
  "tasks": {
    "T-0001": "done",
    "T-0002": "done",
    "T-0003": "running"
  },
  "findings": {
    "total": 3,
    "confirmed": 0,
    "needs_review": 3
  }
}
```

---

## 6. Agent 类型设计

## 6.1 基础 Agent

```text
repo-map-agent
  负责项目结构、模块边界、构建系统、入口点识别

api-map-agent
  负责 OpenAPI / Swagger / route / handler 映射

source-audit-agent
  负责源码静态审计

dataflow-agent
  负责调用链、数据流、污点路径分析

evidence-review-agent
  负责证据复核，降低误报

exploitability-agent
  负责可利用性分析，但不做越权范围外动作

report-agent
  负责生成最终报告
```

注意：为了合规和可控，`exploitability-agent` 应该默认限制在 **授权测试环境和非破坏性验证** 内。

---

## 6.2 动态 Agent

当 workflow 发现新需求时，Agent Factory 可以生成临时 agent。

例如：

```text
发现项目使用 MyBatis
  -> 生成 mybatis-sql-audit-agent

发现项目使用 Shiro
  -> 生成 shiro-config-review-agent

发现项目有大量 OpenAPI YAML
  -> 生成 openapi-operationid-trace-agent

发现存在复杂鉴权逻辑
  -> 生成 authz-path-review-agent
```

动态生成过程：

```text
Finding / Feedback
       ↓
Capability Gap Detector
       ↓
Select base profile
       ↓
Select skills
       ↓
Select MCP
       ↓
Render AGENTS.md
       ↓
Spawn via CAO
```

---

## 7. 通信协议

## 7.1 Task

```json
{
  "schema": "hutch.task.v1",
  "task_id": "T-0010",
  "type": "review_finding",
  "priority": "high",
  "agent": "evidence-review-agent",
  "inputs": [
    "findings/F-0007.json",
    "evidence/F-0007/"
  ],
  "objective": "Review whether the finding is supported by source-level evidence.",
  "acceptance": {
    "must_output": [
      "result_json",
      "review_notes"
    ]
  }
}
```

## 7.2 Finding

```json
{
  "schema": "hutch.finding.v1",
  "finding_id": "F-0007",
  "title": "Possible authorization bypass in user management API",
  "type": "authorization-bypass",
  "severity": "medium",
  "confidence": "low",
  "status": "needs_review",
  "source_agent": "java-static-audit-agent",
  "evidence": [
    {
      "kind": "source",
      "path": "src/main/java/...",
      "line_start": 120,
      "line_end": 155
    },
    {
      "kind": "call_chain",
      "path": "evidence/F-0007/call-chain.md"
    }
  ],
  "next_action": "evidence_review"
}
```

## 7.3 Agent Status

```json
{
  "schema": "hutch.agent_status.v1",
  "agent": "java-static-audit-agent",
  "status": "running",
  "current_task": "T-0003",
  "last_heartbeat": "2026-06-19T10:31:00+09:00",
  "runtime": {
    "type": "cao",
    "session": "java-static-audit-agent"
  }
}
```

---

## 8. 断点恢复设计

恢复时不问 LLM “你刚才干到哪了”，而是看状态文件。

`hutch resume <run-id>` 执行：

```text
1. 读取 state.json
2. 读取 task_graph.json
3. 检查每个 task 的 result 是否存在
4. 检查每个 agent 的 status.json
5. 检查 CAO session 是否还活着
6. 对活着的 session 做 reconnect
7. 对死掉的 session 用原 profile 重新 spawn
8. 对 running 但无结果的 task 标记为 resumable
9. 给 agent 投递 resume task
```

Resume task 示例：

```json
{
  "schema": "hutch.task.v1",
  "task_id": "T-0003-resume",
  "type": "resume",
  "resume_of": "T-0003",
  "instructions": "Continue from the existing artifacts and notes. Do not restart from scratch unless required.",
  "inputs": [
    "outbox/T-0003.partial.json",
    "artifacts/T-0003/",
    "transcript.md"
  ]
}
```

---

## 9. Worktree 演进设计

MVP 可以所有 agent 共享只读 repo。

后续演进成：

```text
runs/<run-id>/worktrees/
  base/
  java-static-audit-agent/
  patch-test-agent/
  exploitability-agent/
```

策略：

```text
只读审计 agent:
  使用 shared readonly repo

需要实验/修改的 agent:
  分配独立 git worktree

报告/复核 agent:
  只读取 artifacts/evidence，不直接改 repo
```

这样可以避免 agent 互相污染工作区。

---

## 10. 权限模型

每个 Agent Cell 都有 `permissions.yaml`。

示例：

```yaml
filesystem:
  read:
    - "/workspace/project"
    - "./shared"
    - "./inbox"
  write:
    - "./outbox"
    - "./artifacts"
    - "./evidence"
  deny:
    - "/etc"
    - "~/.ssh"

shell:
  allow:
    - "rg"
    - "grep"
    - "find"
    - "cat"
    - "python"
    - "joern"
  deny:
    - "curl"
    - "wget"
    - "rm -rf"
    - "nc"
    - "ssh"

network:
  mode: "disabled"

git:
  mode: "readonly"
```

对于验证类 agent：

```yaml
network:
  mode: "allowlist"
  allow:
    - "http://127.0.0.1:8080"
    - "http://test-env.internal"
```

---

## 11. MVP 实现范围

### MVP 只做 6 件事

```text
1. hutch run 创建 run 目录
2. 根据 workflow.yaml 生成 agent cell
3. 调用 CAO 启动 opencode/codex agent
4. 通过 inbox/outbox 投递和读取任务
5. 维护 state.json / events.jsonl / findings.jsonl
6. 支持 hutch resume
```

先不要做：

```text
Web UI
复杂 RAG
复杂权限沙箱
自动漏洞验证
自动报告美化
复杂 agent 进化
```

---

## 12. 技术选型建议

```text
语言：
  Python 优先，方便写文件协议、YAML、SQLite、CLI

CLI：
  Typer / Click

配置：
  YAML + JSON Schema

状态：
  state.json + SQLite + JSONL

模板：
  Jinja2

CAO 集成：
  subprocess 调 cao
  后续再接 CAO session API / plugin

文件监听：
  watchdog，可选

Schema 校验：
  pydantic / jsonschema
```

---

## 13. 目录结构

```text
rabbit-hutch/
  pyproject.toml
  README.md

  hutch/
    cli.py
    config.py

    workflow/
      engine.py
      parser.py
      scheduler.py
      task_graph.py

    runtime/
      base.py
      cao.py
      mock.py

    factory/
      agent_factory.py
      profile_renderer.py
      skill_selector.py
      mcp_composer.py

    protocol/
      task.py
      result.py
      finding.py
      event.py
      status.py

    store/
      run_store.py
      event_store.py
      finding_store.py
      checkpoint.py

    security/
      permissions.py
      scope.py

    templates/
      agents/
        repo-map/
        java-static-audit/
        evidence-review/
        report/

  workflows/
    java-whitebox-audit.yaml
    web-pentest.yaml

  schemas/
    task.schema.json
    result.schema.json
    finding.schema.json
    workflow.schema.json

  examples/
    java-whitebox-demo/
```

---

## 14. 第一版 workflow 示例

```yaml
name: java-whitebox-audit
version: 0.1

run:
  mode: durable
  runtime: cao

target:
  repo: /workspace/target
  language: java
  scope: authorized-only

agents:
  repo-map:
    provider: codex
    profile: repo-map

  openapi-map:
    provider: opencode
    profile: openapi-map

  java-audit:
    provider: opencode
    profile: java-static-audit

  evidence-review:
    provider: codex
    profile: evidence-review

  report:
    provider: codex
    profile: report

tasks:
  - id: T-0001
    agent: repo-map
    type: repo_map
    outputs:
      - shared/repo-map.json

  - id: T-0002
    agent: openapi-map
    type: openapi_map
    outputs:
      - shared/openapi-map.json

  - id: T-0003
    agent: java-audit
    type: static_audit
    depends_on:
      - T-0001
      - T-0002
    outputs:
      - findings

  - id: T-0004
    agent: evidence-review
    type: review_findings
    depends_on:
      - T-0003
    dynamic_from:
      source: findings
      filter: status == "needs_review"

  - id: T-9999
    agent: report
    type: final_report
    depends_on:
      - T-0004
```

---

## 15. Agent Factory 示例

输入：

```yaml
profile: java-static-audit
provider: opencode
task_type: static_audit
target:
  language: java
  has_openapi: true
  uses_mybatis: true
```

选择结果：

```yaml
skills:
  - java-code-audit
  - openapi-route-mapping
  - mybatis-sql-review
  - authz-logic-review

mcp:
  - filesystem
  - ripgrep
  - joern

permissions:
  shell: readonly
  network: disabled
```

生成：

```text
AGENTS.md
skills/java-code-audit/SKILL.md
skills/openapi-route-mapping/SKILL.md
skills/mybatis-sql-review/SKILL.md
mcp.json
permissions.yaml
```

---

## 16. 调度逻辑

伪代码：

```python
def run_workflow(run_id):
    state = load_state(run_id)
    graph = load_task_graph(run_id)

    while not graph.done():
        ready_tasks = graph.get_ready_tasks(state)

        for task in ready_tasks:
            agent = ensure_agent(task.agent)
            write_inbox(agent, task)
            runtime.send_task(agent.id, task)
            mark_task_running(task.id)

        results = collect_results(run_id)

        for result in results:
            validate_result(result)
            store_result(result)
            update_findings(result.findings)
            mark_task_done(result.task_id)

            new_tasks = derive_next_tasks(result)
            graph.add_tasks(new_tasks)

        save_state(state)
```

---

## 17. Orchestrator 角色边界

Orchestrator 不应该做：

```text
不直接判断漏洞真假
不直接写漏洞报告结论
不直接做源码审计
不直接替 agent 总结复杂证据
```

Orchestrator 应该做：

```text
检查结果 schema 是否合法
检查 evidence 文件是否存在
检查 task 是否完成
检查依赖是否满足
根据规则生成下一步 task
根据能力缺口请求 Agent Factory 生成 agent
```

也就是 **workflow controller**，不是 **security analyst**。

---

## 18. 与 CAO 的集成方式

### 第一阶段：subprocess 调用 CAO

```python
subprocess.run([
    "cao",
    "launch",
    "--session-name", agent_id,
    "--working-directory", agent_workspace,
    prompt
])
```

### 第二阶段：使用 CAO session 能力

用 CAO 的 session 相关能力检查、驱动运行中的 agent。CAO release 里已经提到 session CLI 和 one-shot headless task execution 方向，可以优先复用。([GitHub][2])

### 第三阶段：CAO plugin

如果 CAO plugin hook 足够，则写：

```text
cao-rabbit-hutch-plugin
```

负责：

```text
agent_started -> 写 events.jsonl
agent_stopped -> 更新 status.json
output_chunk -> 归档 transcript
task_completed -> 通知 Workflow Engine
```

---

## 19. 什么时候需要改 CAO

只有下面几类需求无法外挂时，才二开或 PR：

```text
1. 无法从外部稳定投递消息到指定 session
2. 无法稳定读取 session 输出
3. 无法获取 agent 运行状态
4. 无法传入 provider-specific profile
5. 无法控制 agent 工作目录
6. 无法注入 agent-specific env
7. 无法提供必要 event hook
```

即使改，也只改 runtime 通用能力，不把 Rabbit Hutch 的安全业务逻辑塞进 CAO。

---

## 20. 最小可用版本 Roadmap

### v0.1：文件协议 + CAO 启动

```text
- hutch init
- hutch run
- run 目录生成
- agent cell 生成
- CAO 启动 agent
- inbox/outbox 通信
- state.json 更新
```

### v0.2：断点恢复

```text
- hutch resume
- task result 检测
- agent session 检测
- dead session respawn
- resume task 生成
```

### v0.3：Agent Factory

```text
- profile 模板
- skills 选择
- mcp.json 生成
- permissions.yaml 生成
```

### v0.4：Finding Pipeline

```text
- findings.jsonl
- evidence 目录
- finding review task
- finding status lifecycle
```

### v0.5：Worktree 模式

```text
- per-agent worktree
- readonly / writable workspace 策略
- artifact diff
```

---

## 21. 最终形态

最终 Rabbit Hutch 应该是：

```text
一个面向安全任务的 durable multi-CLI-agent overlay
```

不是：

```text
又一个 LangGraph
又一个 CrewAI
又一个 CAO fork
```

最重要的设计原则是：

```text
CAO 是运行时。
兔笼是状态机。
Agent 是 worker。
文件系统是事实源。
Workflow 是真流程。
Finding 是核心资产。
Evidence 是审计依据。
```

建议你第一版就按这个边界做：**Rabbit Hutch 只依赖 CAO 的启动、session、通信能力；所有 pentest workflow 和 agent 拼装逻辑都留在外挂层。**

[1]: https://github.com/awslabs/cli-agent-orchestrator?utm_source=chatgpt.com "awslabs/cli-agent-orchestrator"
[2]: https://github.com/awslabs/cli-agent-orchestrator/releases?utm_source=chatgpt.com "Releases · awslabs/cli-agent-orchestrator"
