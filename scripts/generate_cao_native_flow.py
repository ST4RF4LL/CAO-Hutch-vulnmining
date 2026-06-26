#!/usr/bin/env python3
"""Compile a Hutch workflow into CAO profiles and one native CAO flow."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_cells import (
    AgentCellError,
    install_opencode_agent_policy,
    runtime_skill_name,
    validate_cell_specs,
)
from hutch_paths import expand_config_path, expand_config_paths, hutch_generated_dir, repo_relative


ROOT = Path(__file__).resolve().parents[1]
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SUPPORTED_PROVIDERS = {"codex", "opencode_cli"}


class CompileError(RuntimeError):
    pass


def load_and_validate(path: Path) -> dict[str, Any]:
    workflow = json.loads(path.read_text(encoding="utf-8"))
    if workflow.get("skill_roots"):
        workflow["skill_roots"] = [
            str(item) for item in expand_config_paths(workflow.get("skill_roots", []))
        ]
    if workflow.get("schema") != "hutch.cao-workflow.v1":
        raise CompileError("workflow must use hutch.cao-workflow.v1")
    if not NAME_RE.fullmatch(str(workflow.get("name", ""))):
        raise CompileError("workflow name must match [A-Za-z0-9_-]{1,64}")
    if workflow.get("provider") not in SUPPORTED_PROVIDERS:
        raise CompileError(
            "the CAO-native compiler requires provider=codex or provider=opencode_cli"
        )
    agents = {agent["id"]: agent for agent in workflow.get("agents", [])}
    if not agents or not workflow.get("stages"):
        raise CompileError("workflow must define agents and stages")
    for agent_id in agents:
        profile_name = f"{workflow['name']}-{agent_id}"
        if not NAME_RE.fullmatch(profile_name):
            raise CompileError(f"generated profile name is invalid: {profile_name}")
    try:
        validate_cell_specs(
            workflow,
            (
                {
                    "id": agent["id"],
                    "profile": f"{workflow['name']}-{agent['id']}",
                    "skills": agent.get("skills", []),
                    "skill_sources": agent.get("skill_sources", {}),
                }
                for agent in workflow["agents"]
            ),
        )
    except AgentCellError as error:
        raise CompileError(str(error)) from error
    seen: set[str] = set()
    task_ids: set[str] = set()
    for stage in workflow["stages"]:
        if stage["id"] in seen:
            raise CompileError(f"duplicate stage id: {stage['id']}")
        if stage["task_id"] in task_ids:
            raise CompileError(f"duplicate task id: {stage['task_id']}")
        if stage["agent"] not in agents:
            raise CompileError(f"stage {stage['id']} references unknown agent {stage['agent']}")
        unresolved = set(stage.get("depends_on", [])) - seen
        if unresolved:
            raise CompileError(f"stage {stage['id']} has unresolved dependencies: {sorted(unresolved)}")
        seen.add(stage["id"])
        task_ids.add(stage["task_id"])
        for artifact in [stage["artifact"], *stage.get("required_artifacts", [])]:
            artifact_path = Path(artifact)
            if artifact_path.is_absolute() or ".." in artifact_path.parts:
                raise CompileError(f"stage {stage['id']} has unsafe artifact path: {artifact}")
        if stage.get("coverage_contract"):
            contract = stage["coverage_contract"]
            if not contract.get("module_ids") or not contract.get("artifact"):
                raise CompileError(f"stage {stage['id']} has invalid coverage contract")
        if stage.get("coverage_gate"):
            unknown = set(stage["coverage_gate"].get("audit_stages", [])) - seen
            if unknown:
                raise CompileError(
                    f"coverage gate {stage['id']} references unknown earlier stages: {sorted(unknown)}"
                )
    concurrency = workflow.get("execution", {}).get("max_concurrency", 1)
    if not isinstance(concurrency, int) or not 1 <= concurrency <= 16:
        raise CompileError("execution.max_concurrency must be between 1 and 16")
    execution = workflow.get("execution", {})
    if execution.get("no_supervisor"):
        entry_agent = execution.get("entry_agent")
        if entry_agent not in agents:
            raise CompileError("execution.entry_agent must reference an existing agent")
        entry_stages = execution.get("entry_stages", [])
        if not entry_stages or any(stage_id not in seen for stage_id in entry_stages):
            raise CompileError("execution.entry_stages must reference existing stages")
    return workflow


def execution_batches(workflow: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Compile the DAG into deterministic bounded-concurrency launch batches."""
    maximum = int(workflow.get("execution", {}).get("max_concurrency", 1))
    remaining = list(workflow["stages"])
    completed: set[str] = set()
    batches: list[list[dict[str, Any]]] = []
    while remaining:
        ready = [
            stage
            for stage in remaining
            if set(stage.get("depends_on", [])) <= completed
        ]
        if not ready:
            raise CompileError("workflow dependency graph cannot make progress")
        batch = ready[:maximum]
        batches.append(batch)
        completed.update(stage["id"] for stage in batch)
        selected = {stage["id"] for stage in batch}
        remaining = [stage for stage in remaining if stage["id"] not in selected]
    return batches


def yaml_list(values: list[str], indent: int = 2) -> str:
    prefix = " " * indent
    return "\n".join(f"{prefix}- {value}" for value in values)


def shell_double_quoted_value(value: str) -> str:
    """Escape a string for insertion inside shell double quotes."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


def agent_mcp_servers(
    workflow: dict[str, Any], agent: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    configured = agent.get("mcp_servers")
    if configured is None:
        configured = {}
        if agent.get("atlas"):
            configured["atlas"] = {
                "type": "stdio",
                "command": "atlas",
                "args": ["mcp"],
            }
        if agent.get("orchestrates"):
            configured["cao-mcp-server"] = {
                "type": "stdio",
                "command": "uv",
                "args": [
                    "--directory",
                    "${CAO_REPO}",
                    "run",
                    "cao-mcp-server",
                ],
            }
    cao_repo = str(expand_config_path(workflow["cao_repo"]))
    servers: dict[str, dict[str, Any]] = {}
    for name, raw in configured.items():
        servers[str(name)] = {
            "type": str(raw["type"]),
            "command": str(raw["command"]).replace("${CAO_REPO}", cao_repo),
            "args": [
                str(item).replace("${CAO_REPO}", cao_repo)
                for item in raw.get("args", [])
            ],
        }
    return servers


def render_mcp_servers(servers: dict[str, dict[str, Any]]) -> str:
    if not servers:
        return ""
    lines = ["", "mcpServers:"]
    for name, server in servers.items():
        lines.extend(
            [
                f"  {name}:",
                f"    type: {server['type']}",
                f"    command: {json.dumps(server['command'])}",
                "    args:",
            ]
        )
        lines.extend(f"      - {json.dumps(arg)}" for arg in server["args"])
    return "\n".join(lines) + "\n"


def render_worker_profile(workflow: dict[str, Any], agent: dict[str, Any]) -> str:
    profile_name = f"{workflow['name']}-{agent['id']}"
    provider = workflow["provider"]
    tools = ["fs_read", "fs_list", "fs_write", "execute_bash"]
    servers = agent_mcp_servers(workflow, agent)
    tools.extend(f"'@{name}'" for name in servers)
    atlas_rules = ""
    if "atlas" in servers:
        atlas_rules = """
- Use Atlas for symbol discovery, callers/callees, dependency paths, and provenance where it materially strengthens the evidence. Verify important graph claims against source.
"""
    mcp = render_mcp_servers(servers)
    instructions_file = agent.get("instructions_file")
    if instructions_file:
        role_instructions = expand_config_path(instructions_file).read_text(
            encoding="utf-8"
        ).strip()
    else:
        role_instructions = str(agent.get("mission", "")).strip()
    if not role_instructions:
        raise CompileError(f"agent {agent['id']} has no instructions")
    approved_skills = (
        ", ".join(
            f"`{runtime_skill_name(agent['id'], name)}`" for name in agent.get("skills", [])
        )
        or "none"
    )
    audit_skill_rules = ""
    if "audit-skills" in agent.get("skills", []):
        audit_runtime_name = runtime_skill_name(agent["id"], "audit-skills")
        skill_root = (
            f".opencode/skills/{audit_runtime_name}"
            if provider == "opencode_cli"
            else f".agents/skills/{audit_runtime_name}"
        )
        audit_skill_rules = f"""
- The copied Skill root is `{skill_root}/`; its component scanner is `{skill_root}/scripts/run_component_vulnerability_scan.py`. Resolve every `references/` and `scripts/` path from that root because upstream examples containing `skills/audit-skills/` do not match the isolated runtime alias.
- The Hutch task and output contracts override `audit-skills` default output paths. Put its temporary scanner workspace below `tmp/audit-skills/<task-id>/`, then translate required deliverables into the task's `artifacts/` paths.
- Do not download or install CFR, de4dot, ILSpy, or any other tool. Use a referenced tool only when it is already installed and allowed by this task.
- Treat component/version/CVE matches as leads. They are never confirmed vulnerabilities without reachable project code, controllable input, an unblocked source-to-sink path, exploitable impact, and safe reproduction evidence.
"""
    return f"""---
name: {profile_name}
description: {agent['description']}
provider: {provider}
role: reviewer
allowedTools:
{yaml_list(tools)}
{mcp}---

# Mission

{role_instructions}

# Hutch execution contract

- Read the absolute task JSON path in the CAO handoff message before doing anything else.
- Read target source from the absolute `target.path` declared in the task JSON. Never modify the target project.
- Do not use the network, run builds, execute tests, load models, or execute target code. This flow is static analysis only.
- Read only from the run directory and the declared target project. Write only the requested artifact, `outbox/<task-id>.result.json`, and temporary files below `tmp/`.
- Read every declared input. Cite repository-relative paths, exact symbols, and line numbers in evidence.
- Distinguish source-proven behavior, reasonable inference, and missing deployment or runtime facts.
- The artifact must contain every exact `##` heading listed in `acceptance.required_sections`.
- Write every path in `outputs.required_artifacts` and declare it in the result JSON.
- When `json_contracts` exists, each listed artifact must be valid JSON with the exact required schema.
- When `report_consistency` exists, copy Finding counts and dispositions exactly from the validation result; Hutch checks the Markdown metrics table against machine results.
- When `coverage_contract` exists, write its artifact as `hutch.coverage.v1`, include exactly every contracted module ID once, and use only `audited`, `deferred`, or `failed`. Every audited module requires `reviewed_file_count` plus non-empty source `evidence` entries containing `path` and `observation`; a deferred module requires a concrete reason.
- Write the result JSON last and only after the artifact is complete.{atlas_rules}
- Your Agent Cell permits only these workflow skills: {approved_skills}. Do not attempt to load any other skill.
{audit_skill_rules}

# Result contract

Write `outbox/<task-id>.result.json` as valid JSON:

```json
{{
  "schema": "hutch.result.v1",
  "task_id": "from the task document",
  "stage": "from the task document",
  "status": "done",
  "summary": "concise evidence-based summary",
  "artifacts": ["primary artifact and every required artifact from the task document"],
  "findings": [],
  "limitations": ["explicit scope or evidence limitations"]
}}
```

Every non-empty finding must satisfy `finding_contract` in the task JSON. Audit agents use status `candidate`. The validator may use `confirmed`, `likely`, `needs-info`, or `false-positive`. An empty findings array is valid and preferred over unsupported claims.
"""


def render_supervisor_profile(workflow: dict[str, Any], cao_repo: Path) -> str:
    profile_name = f"{workflow['name']}-supervisor"
    if workflow["provider"] == "codex":
        mcp_config = f"""mcpServers:
  cao-mcp-server:
    type: stdio
    command: uv
    args:
      - --directory
      - {cao_repo}
      - run
      - cao-mcp-server
"""
    else:
        mcp_config = """mcpServers:
  cao-mcp-server:
    type: stdio
    command: sh
    args:
      - -lc
      - 'uv --directory "${CAO_REPO:?set CAO_REPO to your cli-agent-orchestrator checkout}" run cao-mcp-server'
"""
    return f"""---
name: {profile_name}
description: CAO-native supervisor for the {workflow['name']} Hutch workflow.
provider: {workflow['provider']}
role: supervisor
allowedTools:
  - fs_read
  - fs_list
  - fs_write
  - execute_bash
  - '@cao-mcp-server'
{mcp_config}
---

# Role

You are the deterministic supervisor of a CAO-owned Rabbit Hutch flow. CAO created your `cao-flow-*` session. Execute the exact bounded batches and Hutch launcher commands in the rendered flow prompt.

# Non-negotiable rules

- Do not perform architecture analysis, security auditing, validation, or report writing yourself.
- Do not use native subagent/task features. All worker execution must go through CAO MCP so CAO records the worker terminals in this flow session.
- Use the exact Agent Cell workspace from the stage plan as `working_directory` for every assignment.
- Execute the rendered batches in order. Launch every worker in one batch before awaiting any worker in that batch. Never exceed the rendered concurrency bound.
- A stage may run only after every dependency has passed Hutch validation.
- After each assignment, record the CAO terminal ID and run the exact await command from the flow prompt. A worker's prose response or CAO TUI status is not completion evidence.
- Codex may report a CAO terminal as `completed` while a long-running turn still shows a minutes-form progress spinner or background terminal. Never advance, delete, or retry from CAO terminal status; only the Hutch result-file gate below is authoritative.
- On validation failure, delete the failed terminal and assign the same task once more with the validation error. Never invent, repair, or silently accept a worker artifact.
- Stop on the second failure and leave the state and evidence intact for diagnosis.
- Never modify the target project.
- Do not delete the CAO session or worker evidence. CAO owns runtime lifecycle.
"""


def render_flow(workflow: dict[str, Any], prepare_script_name: str) -> str:
    if workflow.get("execution", {}).get("no_supervisor"):
        return render_no_supervisor_flow(workflow, prepare_script_name)
    name = workflow["name"]
    supervisor = f"{name}-supervisor"
    timeout = int(workflow.get("execution", {}).get("stage_timeout_seconds", 1800))
    max_attempts = int(workflow.get("execution", {}).get("max_attempts", 2))
    maximum = int(workflow.get("execution", {}).get("max_concurrency", 1))
    batch_lines: list[str] = []
    for batch_index, batch in enumerate(execution_batches(workflow), start=1):
        batch_lines.append(f"Batch {batch_index} (launch at most {maximum} workers):")
        for stage in batch:
            profile = f"{name}-{stage['agent']}"
            dependencies = ", ".join(stage.get("depends_on", [])) or "none"
            preflight = (
                f"`python3 \"$HUTCH_REPO/scripts/hutch_flow_state.py\" coverage \"[[run_dir]]\" {stage['id']}`"
                if stage.get("coverage_gate")
                else "none"
            )
            batch_lines.append(
                f"- `{stage['id']}`: profile `{profile}`, workspace `[[run_dir]]/agents/{stage['agent']}/workspace`, task `[[run_dir]]/inbox/{stage['task_id']}.task.json`, dependencies: {dependencies}, preflight: {preflight}."
            )
    stages = "\n".join(batch_lines)
    final_artifact = workflow["stages"][-1]["artifact"]
    repo_default = shell_double_quoted_value(str(ROOT))
    return f"""---
name: {name}
schedule: "{workflow['schedule']}"
agent_profile: {supervisor}
provider: {workflow['provider']}
script: ./{prepare_script_name}
---
# CAO-native Hutch run

CAO owns this flow and the current session. Hutch prepared run `[[run_id]]` at `[[run_dir]]`.

Read these files first:

- manifest: `[[manifest]]`
- durable state: `[[state_file]]`

Before the first batch, run:

`export HUTCH_REPO="${{HUTCH_REPO:-{repo_default}}}"`

Execute these exact dependency batches in order. Concurrency limit: {maximum}.

{stages}

For every batch:

1. Read every task JSON in the batch and confirm its dependencies are `done` in `[[state_file]]`. Run a preflight only when that stage's explicit `preflight` value is not `none`; never infer or invent a preflight. Stop if an explicit preflight fails.
2. For every stage in the batch, run `python3 "$HUTCH_REPO/scripts/cao_assign_cell.py" <absolute-task-path>`. This Hutch launcher validates the Agent Cell contract, creates the worker through the CAO API in the current CAO session, forces the exact Cell `working_directory`, and submits the task. Record each returned `terminal_id`; do not await yet. Do not substitute CAO MCP `assign`.
3. Immediately after each assignment run `python3 "$HUTCH_REPO/scripts/hutch_flow_state.py" start "[[run_dir]]" <stage-id> <terminal-id>`.
4. After all workers in the batch are running, await each with `python3 "$HUTCH_REPO/scripts/hutch_flow_state.py" await "[[run_dir]]" <stage-id> --timeout {timeout}`. This file gate, not CAO's TUI status, is completion authority.
5. After successful validation, call CAO MCP `delete_terminal` for that worker terminal.
6. If await/validation fails, delete the failed terminal, remove only that stage's invalid result file if one exists, and run the same assignment once more after reporting the validator error. Maximum attempts: {max_attempts}. Stop if the final attempt fails.

After all stages validate, run:

`python3 "$HUTCH_REPO/scripts/hutch_flow_state.py" finalize "[[run_dir]]"`

Your final response must state the run directory, final state, final report path `[[run_dir]]/{final_artifact}`, and that CAO Web owns the visible flow/session records. Do not substitute your own analysis for any missing worker output.
"""


def render_no_supervisor_flow(
    workflow: dict[str, Any], prepare_script_name: str
) -> str:
    name = workflow["name"]
    execution = workflow["execution"]
    entry_agent = str(execution["entry_agent"])
    entry_stage_ids = list(execution["entry_stages"])
    stages = {stage["id"]: stage for stage in workflow["stages"]}
    timeout = int(execution.get("stage_timeout_seconds", 1800))
    maximum = int(execution.get("max_concurrency", 5))
    entry_lines = []
    for stage_id in entry_stage_ids:
        stage = stages[stage_id]
        entry_lines.append(
            f"- `{stage_id}`: launch task `[[run_dir]]/inbox/{stage['task_id']}.task.json` "
            f"with `python3 \"$HUTCH_REPO/scripts/cao_assign_cell.py\" "
            f"\"[[run_dir]]/inbox/{stage['task_id']}.task.json\"`, record it with "
            f"`python3 \"$HUTCH_REPO/scripts/hutch_flow_state.py\" start "
            f"\"[[run_dir]]\" {stage_id} <terminal-id>`, then await it with "
            f"`python3 \"$HUTCH_REPO/scripts/hutch_flow_state.py\" await "
            f"\"[[run_dir]]\" {stage_id} --timeout {timeout}`."
        )
    conditional = [
        stage for stage in workflow["stages"] if stage.get("domain_condition")
    ]
    conditional_lines = []
    for stage in conditional:
        conditional_lines.append(
            f"- `{stage['id']}` / domain `{stage['domain_condition']['domain']}`: "
            f"decision `python3 \"$HUTCH_REPO/scripts/hutch_flow_state.py\" decision "
            f"\"[[run_dir]]\" {stage['id']}`; task "
            f"`[[run_dir]]/inbox/{stage['task_id']}.task.json`."
        )
    final_stage = workflow["stages"][-1]
    repo_default = shell_double_quoted_value(str(ROOT))
    return f"""---
name: {name}
schedule: "{workflow['schedule']}"
agent_profile: {name}-{entry_agent}
provider: {workflow['provider']}
script: ./{prepare_script_name}
---
# CAO-owned end-to-end security workflow

You are the direct-flow coordinator for this workflow, not a worker. Hutch
prepared run `[[run_id]]` at `[[run_dir]]`.

Read `[[manifest]]` and `[[state_file]]`, then run:

`export HUTCH_REPO="${{HUTCH_REPO:-{repo_default}}}"`

## Recon and planning

Launch these stages in order through `cao_assign_cell.py`:

{chr(10).join(entry_lines)}

The planning JSON is authoritative. It must decide `run` or `skip` for every
configured domain based on repository languages, frameworks, interfaces,
artifacts, and threat evidence. Do not run an irrelevant domain merely because
its profile exists.
Do not execute recon or planning yourself; the worker must run in its Agent Cell
workspace so the copied workflow Skills are available.

## Conditional domain audits

Configured domains:

{chr(10).join(conditional_lines)}

Evaluate all decisions first. For each decision:

1. If `action=run`, launch the task with
   `python3 "$HUTCH_REPO/scripts/cao_assign_cell.py" <absolute-task-path>`,
   then record it with `hutch_flow_state.py start`. Launch independent selected
   domains before awaiting, up to concurrency {maximum}.
2. If `action=skip`, run
   `python3 "$HUTCH_REPO/scripts/hutch_flow_state.py" skip "[[run_dir]]" <stage-id>`.
   Hutch writes explicit skipped evidence for the final report.
3. Await every launched domain with
   `python3 "$HUTCH_REPO/scripts/hutch_flow_state.py" await "[[run_dir]]" <stage-id> --timeout {timeout}`.
4. Delete completed worker terminals with CAO MCP `delete_terminal`.

CAO's Codex terminal status is advisory only and may show `completed` while a
minutes-form progress spinner or background terminal is still active. Never
advance or delete a worker from CAO status; only Hutch's result-file validation
is completion evidence.

## Final report

After every domain is `done` or plan-skipped, launch final task
`[[run_dir]]/inbox/{final_stage['task_id']}.task.json` with
`cao_assign_cell.py`, record it with:

`python3 "$HUTCH_REPO/scripts/hutch_flow_state.py" start "[[run_dir]]" {final_stage['id']} <terminal-id>`

and await it with:

`python3 "$HUTCH_REPO/scripts/hutch_flow_state.py" await "[[run_dir]]" {final_stage['id']} --timeout {timeout}`

Then run:

`python3 "$HUTCH_REPO/scripts/hutch_flow_state.py" finalize "[[run_dir]]"`

Your final response must state the run directory, final state, and report path
`[[run_dir]]/{final_stage['artifact']}`. Never fabricate output for a selected
domain that failed or a skipped domain.
"""


def write_output(workflow_path: Path, workflow: dict[str, Any], output: Path, cao_repo: Path) -> dict[str, Any]:
    if output.exists():
        shutil.rmtree(output)
    profiles_dir = output / "profiles"
    profiles_dir.mkdir(parents=True)

    profile_paths: list[Path] = []
    no_supervisor = bool(workflow.get("execution", {}).get("no_supervisor"))
    if not no_supervisor:
        supervisor_path = profiles_dir / f"{workflow['name']}-supervisor.md"
        supervisor_path.write_text(
            render_supervisor_profile(workflow, cao_repo), encoding="utf-8"
        )
        profile_paths.append(supervisor_path)
    for agent in workflow["agents"]:
        path = profiles_dir / f"{workflow['name']}-{agent['id']}.md"
        path.write_text(render_worker_profile(workflow, agent), encoding="utf-8")
        profile_paths.append(path)

    wrapper_name = "prepare-run.sh"
    wrapper_path = output / wrapper_name
    workflow_ref = repo_relative(workflow_path)
    profiles_ref = repo_relative(profiles_dir)
    workflow_arg = workflow_ref if Path(workflow_ref).is_absolute() else f"$HUTCH_REPO/{workflow_ref}"
    profiles_arg = profiles_ref if Path(profiles_ref).is_absolute() else f"$HUTCH_REPO/{profiles_ref}"
    wrapper_path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f": \"${{HUTCH_REPO:={shell_double_quoted_value(str(ROOT))}}}\"\n"
        "export HUTCH_REPO\n"
        f"exec python3 \"$HUTCH_REPO/scripts/prepare_native_flow_run.py\" "
        f"\"{workflow_arg}\" --profiles-dir \"{profiles_arg}\"\n",
        encoding="utf-8",
    )
    wrapper_path.chmod(0o755)
    flow_path = output / f"{workflow['name']}.flow.md"
    flow_path.write_text(render_flow(workflow, wrapper_name), encoding="utf-8")
    manifest = {
        "schema": "hutch.cao-bundle.v1",
        "workflow": workflow["name"],
        "source": repo_relative(workflow_path),
        "flow": repo_relative(flow_path),
        "supervisor_profile": (
            None if no_supervisor else f"{workflow['name']}-supervisor"
        ),
        "entry_profile": (
            f"{workflow['name']}-{workflow['execution']['entry_agent']}"
            if no_supervisor
            else f"{workflow['name']}-supervisor"
        ),
        "profiles": [repo_relative(path) for path in profile_paths],
        "register_enabled": bool(workflow.get("execution", {}).get("register_enabled", False)),
    }
    (output / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def run(command: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if check and result.returncode != 0:
        raise CompileError(f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}")
    return result


def manifest_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def install_bundle(manifest: dict[str, Any], cao_repo: Path, replace: bool, disable: bool) -> None:
    cao = ["uv", "--directory", str(cao_repo), "run", "cao"]
    workflow = json.loads(manifest_path(manifest["source"]).read_text(encoding="utf-8"))
    provider = str(workflow["provider"])
    policies = {
        f"{workflow['name']}-{agent['id']}": [
            (agent["id"], agent.get("skills", []))
        ]
        for agent in workflow["agents"]
    }
    if not workflow.get("execution", {}).get("no_supervisor"):
        policies[f"{workflow['name']}-supervisor"] = [("supervisor", [])]
    for profile in manifest["profiles"]:
        profile_path = manifest_path(profile)
        result = run(cao + ["install", str(profile_path), "--provider", provider], cao_repo)
        if "Error:" in result.stdout:
            raise CompileError(result.stdout.strip())
        if provider == "opencode_cli":
            install_opencode_agent_policy(
                profile_path,
                profile_path.stem,
                policies[profile_path.stem],
                extra_read_roots=[workflow["target"]],
            )
    name = manifest["workflow"]
    if replace:
        run(cao + ["flow", "remove", name], cao_repo, check=False)
    run(cao + ["flow", "add", str(manifest_path(manifest["flow"]))], cao_repo)
    if disable:
        run(cao + ["flow", "disable", name], cao_repo)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cao-repo", type=Path)
    parser.add_argument("--install", action="store_true", help="Install profiles and register the native flow in CAO")
    parser.add_argument("--replace", action="store_true", help="Replace an existing CAO flow with the same name")
    parser.add_argument("--enable", action="store_true", help="Leave the installed flow schedule enabled")
    args = parser.parse_args()
    try:
        workflow_path = args.workflow.resolve()
        workflow = load_and_validate(workflow_path)
        cao_repo = args.cao_repo.resolve() if args.cao_repo else expand_config_path(workflow["cao_repo"])
        output = (args.output or hutch_generated_dir() / workflow["name"]).resolve()
        manifest = write_output(workflow_path, workflow, output, cao_repo)
        if args.install:
            install_bundle(manifest, cao_repo, args.replace, disable=not args.enable)
        print(json.dumps({"ok": True, "bundle": manifest, "installed": args.install}, indent=2))
        return 0
    except Exception as error:
        print(f"compile failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
