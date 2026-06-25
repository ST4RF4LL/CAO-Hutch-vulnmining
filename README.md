# QU / Hutch

Hutch 是 CAO 的构造与审计编排层。它把一个代码仓库的完整漏洞挖掘任务拆成三个 CAO 原生 Flow，并在 Dashboard 中按 Campaign 聚合展示；Agent 的实际执行、session 和 Flow 状态仍由 CAO 管理。

通用框架定义见 [`workflows/generic-vulnerability-mining-framework.json`](workflows/generic-vulnerability-mining-framework.json)。它不是固定 Agent 数量的静态 Flow，而是一个自适应 Campaign：前两个 Flow 先生成仓库情报与审计计划，第三个 Flow 再按模块规模和计划拆分并发挖掘任务。

## 通用漏洞挖掘流程

```text
recon
  架构/业务/外部接口梳理 + 模块清单 + 攻击面/信任域/威胁模型
    ↓ 已完成且交付物通过合约校验
planning
  根据模块规模、风险与路径重叠生成覆盖全部模块的审计计划
    ↓ 已完成且 audit-plan 通过完备性校验
mining
  组件风险情报 → 并行审计分片 → 确定性覆盖门 → 漏洞复核去重 → 总报告
```

默认检查轨道包括路由与鉴权、注入与代码执行、反序列化、文件/路径/上传、SSRF 与外连、密钥与配置、Native/FFI/内存安全、依赖与供应链、业务逻辑与租户隔离。确认漏洞必须同时具备真实入口、用户可控输入、未被阻断的 source-to-sink、可利用影响、安全复现证据，以及文件/符号/行号证据；否则只能保留为线索或待确认项。

Flow 使用不可变目标快照、独立 Agent Cell、受限并发、逐模块覆盖合约、最终漏洞复核和报告一致性检查。审计 Agent 不联网、不下载工具、不执行目标代码，也不修改目标仓库。

## 启动一次完整审计

先启动本地 CAO 和 Hutch Dashboard/MCP。项目内 Codex 与 OpenCode 已将 `bin/hutch-mcp` 注册为 `hutch` MCP Server。完整 Campaign 由三个有边界的操作组成：

1. 调用 `create_audit_campaign(target="/absolute/path/to/repo", campaign_id="my-audit")`。
2. recon Run 显示为 `completed` 后，调用 `advance_audit_campaign(recon_run_id)`。
3. planning Run 显示为 `completed` 后，调用 `advance_audit_campaign(planning_run_id)`。

Dashboard 地址默认为 `http://127.0.0.1:9890`。同一 Campaign 的三个 CAO Flow 会聚合为一个总 Flow，同时保留每个子 Flow、Agent session 和交付物。详细 MCP 工具说明见 [`docs/hutch-mcp.md`](docs/hutch-mcp.md)。

## 快速部署

本地快速部署脚本是 [`bin/hutch-deploy`](bin/hutch-deploy)。它只管理 Hutch 本地运行目录和本机服务进程，不 patch CAO、不修改目标仓库、不强制选择 Codex 或 OpenCode。部署前只要求本机至少存在一个 Agent CLI：

```text
codex 或 opencode 至少一个可用
```

常用流程：

```bash
./bin/hutch-deploy check
./bin/hutch-deploy init
./bin/hutch-deploy start
./bin/hutch-deploy status
```

一条命令完成检查、初始化和启动：

```bash
./bin/hutch-deploy all \
  --cao-repo /path/to/cli-agent-orchestrator \
  --hutch-home ~/.hutch
```

带项目注册和模板 Flow 安装：

```bash
./bin/hutch-deploy all \
  --cao-repo /path/to/cli-agent-orchestrator \
  --project-root /absolute/path/to/application-root \
  --project-id demo \
  --project-name Demo \
  --target-repo /absolute/path/to/service-repo \
  --template one-run \
  --flow-name demo-one-run \
  --install-flow
```

停止由部署脚本启动的本地服务：

```bash
./bin/hutch-deploy stop
```

## 运行数据布局

Git 仓库只保存 Hutch 源码、模板、文档和可复用的配置。后续运行过程中可能变更的数据默认写入 `~/.hutch/`：

```text
~/.hutch/runs/                  Run 实例、state、events、inbox/outbox、artifacts、snapshot、Agent Cell
~/.hutch/runs/.trash/           Dashboard 删除后的 Run 记录
~/.hutch/workflows/             由模板生成的目标专用 workflow
~/.hutch/generated/             编译后的 CAO bundle、generated profiles、prepare-run.sh
~/.hutch/projects/projects.json Dashboard 项目注册表
```

如需调整运行数据根目录，设置：

```bash
export HUTCH_HOME=/path/to/hutch-runtime
```

CAO 自身的 runtime 数据仍由 CAO 管理，默认位于 `~/.aws/cli-agent-orchestrator/`。

## 通用模板快速构建

通用模板位于 [`template/flows`](template/flows)，覆盖信息收集、威胁建模、漏洞挖掘，以及首次访问摸底用的 `one-run` 单 Flow。

给定一个目标 Git checkout，可以直接把目标目录与模板拼接成适配该目标的 Hutch workflow：

```bash
./bin/hutch --json flow from-template /absolute/path/to/target-repo \
  --template one-run \
  --name target-one-run \
  --provider codex
```

默认输出为 `~/.hutch/workflows/<name>.generated.json`，避免把本机目标路径误提交到仓库。需要同时编译 CAO bundle：

```bash
./bin/hutch --json flow from-template /absolute/path/to/target-repo \
  --template one-run \
  --name target-one-run \
  --provider codex \
  --compile
```

`--provider` 支持 `codex` 和 `opencode_cli`。不指定时使用模板声明的 provider。

可用模板：

- `information-collection`：只做仓库架构、业务逻辑、外部接口、模块情报。
- `threat-modeling`：信息收集后继续输出攻击面、信任边界和威胁模型。
- `vulnerability-mining`：通用漏洞挖掘、复核、报告。
- `one-run`：把信息收集、威胁建模、漏洞挖掘、复核、报告合并为一个 CAO 可见 Flow，适合对新项目首次摸底。
- `security-knowledge-one-run`：基于 `secknowledge-skill` 和 `hack-skills` 的首次摸底单 Flow。
- `security-knowledge-recon` / `security-knowledge-threat-model` / `security-knowledge-vulnerability-mining`：同一安全知识体系拆成三段式 Flow。

### 一条命令创建单次完整审计 Flow

`flow one_run` 会针对一个 Git checkout 渲染 `one-run` 模板，编译并安装到
CAO。该 Flow 依次执行仓库侦察、威胁建模、审计规划、分维度审计、漏洞复核和
最终报告；默认替换同名 Flow，但不会自动运行：

```bash
./bin/hutch --json flow one_run /absolute/path/to/target-repo \
  --provider codex
```

安装后立即运行：

```bash
./bin/hutch --json flow one_run /absolute/path/to/target-repo \
  --provider codex \
  --start
```

可用 `--name` 指定 CAO Flow 名称，或用 `--no-replace` 禁止替换已有同名 Flow。

不生成独立 Supervisor profile 时，使用：

```bash
./bin/hutch --json flow one_run /absolute/path/to/target-repo \
  --provider codex \
  --no-supervisor \
  --start
```

该模式由 `recon-planner` 作为 CAO Flow 入口，依次完成 recon 和 planning。
Planning 必须对 Java、Web、C/C++、Python、Reverse 五个领域分别输出
`run` 或 `skip`。所有领域 profile 都会安装，但只有计划选择的 auditor 会启动；
跳过项由 Hutch 写入明确的跳过证据，最后由 report agent 统一整合。

模板声明的通用 skill 会在本地 `skill_roots` 中存在时自动挂载；缺失时默认降级渲染。若希望缺少 skill 直接失败，增加 `--strict-skills`。

### `secknowledge-skill` / `hack-skills` 模板

安全知识模板的来源映射见 [`template/skill-bundles/security-knowledge.json`](template/skill-bundles/security-knowledge.json)，Agent 角色模板见 [`template/agent-profiles/security-knowledge.json`](template/agent-profiles/security-knowledge.json)。Hutch 不自动把外部仓库内容提交进本仓库；`hack-skills` 检查到 MIT license，`secknowledge-skill` 在当前检查的 checkout 中未发现 LICENSE/NOTICE，因此需要操作者提供合法本地副本。

准备外部 skill：

```bash
mkdir -p ../external-skills
git clone https://github.com/Pa55w0rd/secknowledge-skill ../external-skills/secknowledge-skill
git clone https://github.com/yaklang/hack-skills ../external-skills/hack-skills

export SECKNOWLEDGE_SKILL_ROOT=$(pwd)/../external-skills/secknowledge-skill
export HACK_SKILLS_ROOT=$(pwd)/../external-skills/hack-skills
```

`SECKNOWLEDGE_SKILL_ROOT` 指向包含根 `SKILL.md` 和 `references/` 的目录；`HACK_SKILLS_ROOT` 指向包含 `skills/*/SKILL.md` 的仓库根目录。Hutch 会递归发现其中的 skill，并在 Agent Cell 内复制成运行期隔离名称，避免污染 OpenCode 全局 skill。

查看可用模板：

```bash
./bin/hutch --json flow templates
```

首次接触一个项目时，建议先使用 `security-knowledge-one-run`。它会生成一个 CAO 可见 Flow，包含目标分类、威胁建模、Web/API/Auth、Injection、File/SSRF/Protocol、AI/Agent、Infra/SupplyChain、复核和最终报告阶段：

```bash
export SECKNOWLEDGE_SKILL_ROOT=/path/to/secknowledge-skill
export HACK_SKILLS_ROOT=/path/to/hack-skills

./bin/hutch --json flow from-template /absolute/path/to/target-repo \
  --template security-knowledge-one-run \
  --name target-security-one-run \
  --strict-skills \
  --compile
```

上面的命令会生成：

- workflow：`~/.hutch/workflows/target-security-one-run.generated.json`
- CAO bundle：`~/.hutch/generated/target-security-one-run/`
- Agent profiles：`~/.hutch/generated/target-security-one-run/profiles/*.md`

这些生成物属于运行期数据，默认不写入 Git 仓库。需要把 Flow 安装到 CAO 时增加 `--install --replace`：

```bash
./bin/hutch --json flow from-template /absolute/path/to/target-repo \
  --template security-knowledge-one-run \
  --name target-security-one-run \
  --strict-skills \
  --compile \
  --install \
  --replace
```

然后在 CAO/Hutch 中启动：

```bash
./bin/hutch --json flow start target-security-one-run
```

如果目标仓库较大，建议使用三段式模板，让每个阶段的交付物更稳定：

```bash
./bin/hutch --json flow from-template /absolute/path/to/target-repo \
  --template security-knowledge-recon \
  --name target-security-recon \
  --strict-skills \
  --compile --install --replace

./bin/hutch --json flow from-template /absolute/path/to/target-repo \
  --template security-knowledge-threat-model \
  --name target-security-threats \
  --strict-skills \
  --compile --install --replace

./bin/hutch --json flow from-template /absolute/path/to/target-repo \
  --template security-knowledge-vulnerability-mining \
  --name target-security-mining \
  --strict-skills \
  --compile --install --replace
```

当前三段式 security-knowledge 模板是通用模板实例化方式：每个 Flow 独立读取同一目标快照。若需要像 adaptive campaign 一样自动把 recon 产物作为 threat-model/mining 的 seed artifact，应使用后续 Campaign 编排增强，而不是手工改 CAO Flow。

生成的 security-knowledge Agent 角色包括：

| Agent | 用途 |
|---|---|
| `security-router` | 目标类型、攻击面和 skill 路由 |
| `web-api-auth-auditor` | Web/API 认证、授权、身份、业务逻辑 |
| `injection-auditor` | SQL/NoSQL、命令、模板、反序列化、XXE、XSS 等注入类 |
| `file-ssrf-auditor` | 文件、上传、路径、SSRF、协议、CORS/CSRF、WebSocket |
| `ai-agent-auditor` | AI/LLM/RAG/MCP/Agent/沙箱相关安全面 |
| `infra-supply-auditor` | 源码管理、依赖混淆、构建发布、容器/K8s、暴露服务 |
| `security-validator` | 候选漏洞复核、去重和状态分类 |
| `security-report-writer` | 汇总最终报告 |

该模板生成的 Agent 仍是 Hutch/CAO 受控 profile：默认静态源码审计、只读目标快照、禁止联网和执行目标代码；外部 skill 只作为方法论和检查清单来源，不能提升权限。

常见失败处理：

- `skills not found in skill_roots`：确认两个环境变量指向正确目录，或去掉 `--strict-skills` 先做降级 dry run。
- `target is not a Git checkout`：目标目录必须是具体微服务 Git repo，而不是上层项目目录。
- `duplicate skill`：两个 skill root 中存在同名 skill；需要只保留一个来源或拆分不同 Flow。
- `secknowledge-skill` 许可证未确认：不要把其内容提交进本仓库；只在本地授权环境中通过 `SECKNOWLEDGE_SKILL_ROOT` 引用。

## `audit-skills` 集成

本仓库固定引入 `RuoJi6/audit-skills` 的 commit `8a66d3c876a00317e3d69174a5f309ed9600a0e3`，完整 Skill 位于 [`third_party/skills/audit-skills`](third_party/skills/audit-skills)，来源和许可证状态见其 [`PROVENANCE.md`](third_party/skills/audit-skills/PROVENANCE.md)。上游在该 commit 未声明 LICENSE，因此重新分发或生产使用前必须单独审查权利条件。

集成点如下：

- recon 的仓库分析 Agent 可使用组件扫描能力补充依赖与部署产物情报。
- planning Agent 把组件风险、反编译边界和漏洞有效性标准纳入任务拆分。
- mining 首先运行 `component-risk-analyst`；Java 规则命中写入 `component-risk.json`，只作为线索，不能直接计为漏洞。
- 每个挖掘 Agent 和最终 `finding-validator` 使用同一套可达、可控、传播、可利用、安全复现和影响判定标准。
- HTTP 证据使用无破坏 Payload、占位 Host/Cookie/凭据和可粘贴到 Burp Repeater 的原始请求；无法安全证明时降级为线索。
- 上游默认输出目录由 Hutch 任务合约覆盖：临时文件进入 Agent Cell 的 `tmp/`，交付物进入 Run 的 `artifacts/`。上游文档中的工具下载步骤在 Hutch 中禁止执行。

## 引用与可插拔的开源安全项目

当前只把 `audit-skills` 的文件纳入仓库；其余项目是方法与后续工具适配的参考，没有被打包，也不会在审计时自动下载或执行。

| 项目 | 地址 | 当前用途 |
|---|---|---|
| RuoJi6/audit-skills | https://github.com/RuoJi6/audit-skills | 已固定版本集成：Java 组件规则、Java/.NET 审计参考、漏洞有效性与安全证据规范 |
| Semgrep | https://github.com/semgrep/semgrep | 规则式静态分析参考 |
| CodeQL | https://github.com/github/codeql | 数据流与污点分析参考 |
| OSV-Scanner | https://github.com/google/osv-scanner | 开源依赖漏洞匹配参考 |
| Trivy | https://github.com/aquasecurity/trivy | 依赖、配置和密钥扫描参考 |
| Gitleaks | https://github.com/gitleaks/gitleaks | 密钥泄露检测参考 |
| CFR | https://github.com/leibnitz27/cfr | `audit-skills` 引用的 Java 反编译器；仅允许使用预装版本 |
| ILSpy | https://github.com/icsharpcode/ILSpy | `audit-skills` 引用的 .NET 反编译器；仅允许使用预装版本 |
| de4dot | https://github.com/de4dot/de4dot | `audit-skills` 引用的 .NET 反混淆工具；仅允许使用预装版本 |

## 边界

Hutch 不替代 CAO 运行 Agent，不直接修改 CAO，也不把依赖版本命中等同于确认漏洞。完整审计结论以持久化 Run 产物、覆盖门结果和 finding validator 的复核结果为准。

## 临时修复 CAO 的 OpenCode TUI 检测

截至 2026-06-22 检查的 CAO `origin/main` commit `b8b1897`，上游尚未合入 Hutch 验证过的 OpenCode rendered-screen 修复。上游已有 OpenCode Provider、raw-buffer 状态规则和 inbox poller，但 `OpenCodeCliProvider.supports_screen_detection` 仍为 `False`，因此 StatusMonitor 不会使用 pyte 合成后的可见画面。OpenCode 的 alternate-screen/cursor redraw 可能拆散 footer 控制序列，导致初始化、运行中或完成状态误判；宽终端的 metadata sidebar 也可能污染结果提取。

Hutch 仓库提供临时 patch：[`patches/cao-opencode-rendered-screen.patch`](patches/cao-opencode-rendered-screen.patch)。它只修改 CAO 的 OpenCode Provider，不修改 CAO 数据库、API 或 Flow 实现，也不把 CAO 的全局默认 Provider 改成 OpenCode；Hutch 生成的 Flow 会显式声明 `opencode_cli`。

先停止本地 CAO 服务，并确认 CAO 中没有需要保留的运行中 session。然后执行：

```bash
export HUTCH_REPO=$(pwd)
export CAO_REPO=/path/to/cli-agent-orchestrator
export CAO_PATCH="$HUTCH_REPO/patches/cao-opencode-rendered-screen.patch"

git -C "$CAO_REPO" fetch origin
git -C "$CAO_REPO" status --short

if git -C "$CAO_REPO" apply --reverse --check "$CAO_PATCH"; then
  echo "CAO OpenCode patch is already applied"
elif git -C "$CAO_REPO" apply --check "$CAO_PATCH"; then
  git -C "$CAO_REPO" apply "$CAO_PATCH"
else
  echo "Patch is incompatible with this CAO revision; do not force it" >&2
  exit 1
fi
```

`git status --short` 若显示非预期修改，应先自行提交或备份；不要在脏工作区执行 `git reset --hard`。应用后验证：

```bash
uv run --directory "$CAO_REPO" python -c \
  'from cli_agent_orchestrator.providers.opencode_cli import OpenCodeCliProvider as P; assert P.supports_screen_detection is True'
uv run --directory "$CAO_REPO" pytest \
  test/providers/test_opencode_cli_unit.py -q
```

验证通过后重启 `cao-server` 和 CAO Web。升级 CAO 时先判断上游是否已经出现等价的 `supports_screen_detection = True` 与 `get_status_from_screen()` 实现；若已经合入，不再应用此 patch。需要回滚临时 patch 时执行：

```bash
git -C "$CAO_REPO" apply --reverse "$CAO_PATCH"
```
