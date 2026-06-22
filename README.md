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
export HUTCH_REPO=/Users/wh4lter/Workspace/Qu-Studio
export CAO_REPO=/Users/wh4lter/Workspace/lab/cli-agent-orchestrator
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
