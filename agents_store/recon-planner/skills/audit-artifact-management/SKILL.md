---
name: audit-artifact-management
description: Manage per-agent-session audit artifacts using SARIF for static analysis, JSON for vulnerability mining, and tmp cleanup/promotion rules.
license: MIT
compatibility: opencode
metadata:
  role: shared
  phase: artifact-management
---

# Audit Artifact Management

Use this skill whenever an audit agent creates temporary files, tool reports, static-analysis output, vulnerability-mining output, scripts, or rules.

## Required Paths

Use `.opencode/agent-manifest/artifact-policy.json` as the source of truth.

- Static-analysis report: `reports/sarif/<agent-name>.<agent-session-id>.sarif`
- Vulnerability-mining report: `reports/vulnerability-mining/<agent-name>.<agent-session-id>.json`
- Scratch workspace: `tmp/<task-module>/`

## Static Analysis Reports

One agent session produces one SARIF file. If several static tools are used in the same session, merge their results into that session's SARIF file using multiple `runs`.

Use SARIF 2.1.0. At minimum include:

- `version`
- `$schema`
- `runs[].tool.driver.name`
- `runs[].results[]`
- `runs[].results[].ruleId`
- `runs[].results[].message.text`
- `runs[].results[].locations[].physicalLocation.artifactLocation.uri`

## Vulnerability-Mining Reports

One vulnerability-mining agent session produces one JSON file. At minimum include:

- `agent_name`
- `agent_session_id`
- `scope`
- `language`
- `tool_inputs`
- `findings`
- `artifacts`
- `learning_candidates`

## Cleanup and Promotion

The orchestrator cleans only the task subdirectories under `tmp/` at task end after it has summarized reports and after `security-skill-optimizer` has promoted reusable scripts, rules, or cases. Do not store durable audit knowledge only in `tmp/`.
