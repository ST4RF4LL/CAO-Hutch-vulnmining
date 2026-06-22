#!/usr/bin/env python3
"""Create the next CAO-visible flow in an adaptive Hutch audit campaign."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from adaptive_audit import (
    AdaptiveAuditError,
    atomic_json,
    compile_workflow,
    load_json,
    validate_audit_plan,
)
from agent_cells import AgentCellError, discover_skills


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAO_REPO = Path("/Users/wh4lter/Workspace/lab/cli-agent-orchestrator")
DEFAULT_SKILL_ROOT = Path(
    "/Users/wh4lter/Workspace/opencode_multi_agents/.opencode/skills"
)
BUNDLED_SKILL_ROOT = ROOT / "third_party" / "skills"
DEFAULT_SKILL_ROOTS = [DEFAULT_SKILL_ROOT, BUNDLED_SKILL_ROOT]


class CampaignError(RuntimeError):
    pass


def safe_name(value: str, maximum: int = 40) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    if not name:
        raise CampaignError("workflow name is empty after normalization")
    return name[:maximum].rstrip("-")


def load_completed_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir = run_dir.resolve()
    workflow = load_json(run_dir / "workflow.json")
    state = load_json(run_dir / "state.json")
    if state.get("status") != "completed":
        raise CampaignError(
            f"upstream run must be completed, got {state.get('status')!r}: {run_dir}"
        )
    return workflow, state


def seed_files(source_root: Path, destination_root: str) -> list[dict[str, str]]:
    if not source_root.is_dir():
        return []
    return [
        {
            "source": str(path.resolve()),
            "destination": f"{destination_root}/{path.relative_to(source_root).as_posix()}",
        }
        for path in sorted(source_root.rglob("*"))
        if path.is_file()
    ]


def recon_workflow(
    target: Path,
    campaign_id: str,
    *,
    name: str | None = None,
    cao_repo: Path = DEFAULT_CAO_REPO,
    skill_roots: list[Path] | None = None,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    if not (target / ".git").exists():
        raise CampaignError(f"recon target must be a Git checkout: {target}")
    campaign_id = safe_name(campaign_id)
    workflow_name = safe_name(name or f"{campaign_id}-recon")
    roots = skill_roots or DEFAULT_SKILL_ROOTS
    available_skills = discover_skills(roots)
    common_skills = [
        skill
        for skill in ("secure-code-review-common", "audit-artifact-management")
        if skill in available_skills
    ]
    repository_skills = [*common_skills]
    if "audit-skills" in available_skills:
        repository_skills.append("audit-skills")
    return {
        "schema": "hutch.cao-workflow.v1",
        "name": workflow_name,
        "version": "1.0.0",
        "description": "Builds complete repository intelligence before vulnerability-mining tasks are planned.",
        "schedule": "0 0 1 1 *",
        "provider": "opencode_cli",
        "target": str(target),
        "cao_repo": str(cao_repo.resolve()),
        "skill_roots": [str(path.resolve()) for path in roots],
        "snapshot": {"max_file_bytes": 2_097_152},
        "execution": {
            "stage_timeout_seconds": 1800,
            "max_attempts": 2,
            "max_concurrency": 1,
            "register_enabled": False,
        },
        "campaign": {
            "schema": "hutch.campaign.v1",
            "id": campaign_id,
            "phase": "recon",
        },
        "agents": [
            {
                "id": "repository-analyst",
                "description": "Maps every repository module, runtime component, business flow, and external interface.",
                "mission": "Use the deterministic module inventory as the completeness boundary. Build architecture, component, business-flow, and external-interface intelligence for every module with source paths and symbols. Do not silently prioritize away low-risk modules and do not claim vulnerabilities.",
                "atlas": True,
                "skills": repository_skills,
            },
            {
                "id": "threat-modeler",
                "description": "Builds source-grounded attack-surface, trust-boundary, and threat intelligence.",
                "mission": "Derive attacker-controlled sources, assets, trust crossings, dangerous sinks, security controls, abuse cases, and threat hypotheses from the complete architecture intelligence. Preserve module IDs and distinguish source evidence, inference, and missing deployment facts.",
                "atlas": True,
                "skills": common_skills,
            },
        ],
        "stages": [
            {
                "id": "repository-intelligence",
                "task_id": "I-0001",
                "agent": "repository-analyst",
                "depends_on": [],
                "artifact": "artifacts/intelligence/architecture.md",
                "required_artifacts": [
                    "artifacts/intelligence/architecture.json",
                    "artifacts/intelligence/business-flows.json",
                    "artifacts/intelligence/external-interfaces.json",
                ],
                "inputs": [
                    "shared/source-fingerprint.json",
                    "shared/snapshot-manifest.json",
                    "shared/repository-inventory.json",
                    "shared/modules.json",
                ],
                "required_sections": [
                    "Repository Scale and Module Map",
                    "Runtime Architecture",
                    "Business Logic and Data Flows",
                    "External Interfaces",
                    "Security-Relevant Components",
                    "Evidence and Limitations",
                ],
                "objective": "Produce complete architecture and business intelligence tied to every deterministic module ID.",
                "json_contracts": [
                    {
                        "artifact": "artifacts/intelligence/architecture.json",
                        "schema": "hutch.architecture-intelligence.v1",
                        "required_fields": ["modules", "components"],
                        "module_coverage": True,
                        "module_field": "modules",
                        "inventory": "shared/modules.json",
                    },
                    {
                        "artifact": "artifacts/intelligence/business-flows.json",
                        "schema": "hutch.business-flows.v1",
                        "required_fields": ["flows"],
                    },
                    {
                        "artifact": "artifacts/intelligence/external-interfaces.json",
                        "schema": "hutch.external-interfaces.v1",
                        "required_fields": ["interfaces"],
                    },
                ],
            },
            {
                "id": "threat-intelligence",
                "task_id": "I-0002",
                "agent": "threat-modeler",
                "depends_on": ["repository-intelligence"],
                "artifact": "artifacts/intelligence/threat-model.md",
                "required_artifacts": [
                    "artifacts/intelligence/attack-surface.json",
                    "artifacts/intelligence/trust-boundaries.json",
                    "artifacts/intelligence/threat-model.json",
                ],
                "inputs": [
                    "shared/modules.json",
                    "artifacts/intelligence/architecture.md",
                    "artifacts/intelligence/architecture.json",
                    "artifacts/intelligence/business-flows.json",
                    "artifacts/intelligence/external-interfaces.json",
                ],
                "required_sections": [
                    "Scope Assets and Adversaries",
                    "Attack Surface",
                    "Trust Boundaries",
                    "Threat Scenarios",
                    "Expected Security Controls",
                    "Audit Intelligence by Module",
                    "Evidence and Limitations",
                ],
                "objective": "Produce complete threat intelligence consumed by the separate audit-planning Flow.",
                "json_contracts": [
                    {
                        "artifact": "artifacts/intelligence/attack-surface.json",
                        "schema": "hutch.attack-surface.v1",
                        "required_fields": ["surfaces"],
                    },
                    {
                        "artifact": "artifacts/intelligence/trust-boundaries.json",
                        "schema": "hutch.trust-boundaries.v1",
                        "required_fields": ["boundaries"],
                    },
                    {
                        "artifact": "artifacts/intelligence/threat-model.json",
                        "schema": "hutch.threat-model.v1",
                        "required_fields": ["threats"],
                    },
                ],
            },
        ],
    }


def planning_workflow(
    recon_run: Path,
    *,
    name: str | None = None,
    cao_repo: Path = DEFAULT_CAO_REPO,
    skill_roots: list[Path] | None = None,
) -> dict[str, Any]:
    recon_workflow, recon_state = load_completed_run(recon_run)
    campaign = recon_state.get("campaign") or recon_workflow.get("campaign") or {}
    campaign_id = campaign.get("id") or recon_state["run_id"]
    workflow_name = safe_name(name or f"{campaign_id}-planning")
    roots = skill_roots or DEFAULT_SKILL_ROOTS
    available_skills = sorted(discover_skills(roots))
    seeds = seed_files(recon_run.resolve() / "artifacts" / "intelligence", "shared/intelligence")
    if not seeds:
        raise CampaignError("recon run has no artifacts/intelligence files")
    return {
        "schema": "hutch.cao-workflow.v1",
        "name": workflow_name,
        "version": "1.0.0",
        "description": "Plans bounded audit shards from validated repository intelligence.",
        "schedule": "0 0 1 1 *",
        "provider": "opencode_cli",
        "target": recon_workflow["target"],
        "cao_repo": str(cao_repo.resolve()),
        "skill_roots": [str(path.resolve()) for path in roots],
        "snapshot": recon_workflow.get("snapshot", {"max_file_bytes": 2_097_152}),
        "execution": {
            "stage_timeout_seconds": 1800,
            "max_attempts": 2,
            "max_concurrency": 1,
            "register_enabled": False,
        },
        "campaign": {
            "schema": "hutch.campaign.v1",
            "id": campaign_id,
            "phase": "planning",
            "parent_run_id": recon_state["run_id"],
            "intelligence_run_id": recon_state["run_id"],
        },
        "seed_artifacts": seeds,
        "agents": [
            {
                "id": "audit-planner",
                "description": "Designs a complete bounded audit plan from repository intelligence.",
                "mission": "Read every intelligence artifact and the deterministic module inventory. Choose whole_repo, sharded, or hybrid execution; bind every module to at least one task; group work by coherent attack surface and workload. Cover route/authentication and authorization, injection, deserialization, file/path/upload, SSRF and outbound network, secrets/configuration, native/FFI and memory safety where applicable, dependency/component risk, and business-logic abuse. Every task must name its vulnerability tracks and threat IDs. Request only declared skills. Never emit shell commands or runtime profiles.",
                "atlas": False,
                "skills": ["audit-skills"] if "audit-skills" in available_skills else [],
            }
        ],
        "stages": [
            {
                "id": "audit-planning",
                "task_id": "P-0001",
                "agent": "audit-planner",
                "depends_on": [],
                "artifact": "artifacts/audit-plan.md",
                "required_artifacts": ["artifacts/audit-plan.json"],
                "inputs": [
                    "shared/repository-inventory.json",
                    "shared/modules.json",
                    "shared/intelligence",
                ],
                "required_sections": [
                    "Planning Basis",
                    "Strategy and Concurrency",
                    "Shard Assignments",
                    "Module Coverage Matrix",
                    "Risks and Limitations",
                ],
                "objective": "Produce a machine-valid audit plan whose task module union covers the complete deterministic inventory.",
                "json_contracts": [
                    {
                        "artifact": "artifacts/audit-plan.json",
                        "schema": "hutch.audit-plan.v1",
                    }
                ],
                "audit_plan_contract": {
                    "inventory": "shared/modules.json",
                    "artifact": "artifacts/audit-plan.json",
                    "allowed_skills": available_skills,
                },
            }
        ],
    }


def mining_workflow(
    planning_run: Path,
    *,
    name: str | None = None,
    cao_repo: Path = DEFAULT_CAO_REPO,
    skill_roots: list[Path] | None = None,
) -> dict[str, Any]:
    planning_source, planning_state = load_completed_run(planning_run)
    inventory = load_json(planning_run.resolve() / "shared" / "modules.json")
    plan = load_json(planning_run.resolve() / "artifacts" / "audit-plan.json")
    roots = skill_roots or DEFAULT_SKILL_ROOTS
    try:
        available_skills = set(discover_skills(roots))
    except AgentCellError as error:
        raise CampaignError(str(error)) from error
    validate_audit_plan(inventory, plan, allowed_skills=available_skills)
    campaign = planning_state.get("campaign") or planning_source.get("campaign") or {}
    campaign_id = campaign.get("id") or planning_state["run_id"]
    workflow_name = safe_name(name or f"{campaign_id}-mining")
    seeds = seed_files(planning_run.resolve() / "shared" / "intelligence", "shared/intelligence")
    seeds.extend(
        [
            {
                "source": str((planning_run / "artifacts" / "audit-plan.md").resolve()),
                "destination": "shared/planning/audit-plan.md",
            }
        ]
    )
    return compile_workflow(
        inventory,
        plan,
        name=workflow_name,
        target=Path(planning_source["target"]),
        cao_repo=cao_repo,
        skill_roots=roots,
        campaign_id=campaign_id,
        intelligence_run_id=campaign.get("intelligence_run_id"),
        planning_run_id=planning_state["run_id"],
        seed_artifacts=seeds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    recon_parser = commands.add_parser("recon")
    recon_parser.add_argument("target", type=Path)
    recon_parser.add_argument("--campaign-id", required=True)
    recon_parser.add_argument("--output", type=Path, required=True)
    recon_parser.add_argument("--name")
    recon_parser.add_argument("--cao-repo", type=Path, default=DEFAULT_CAO_REPO)
    recon_parser.add_argument("--skill-root", type=Path, action="append", default=[])
    for command in ("planning", "mining"):
        command_parser = commands.add_parser(command)
        command_parser.add_argument("upstream_run", type=Path)
        command_parser.add_argument("--output", type=Path, required=True)
        command_parser.add_argument("--name")
        command_parser.add_argument("--cao-repo", type=Path, default=DEFAULT_CAO_REPO)
        command_parser.add_argument(
            "--skill-root", type=Path, action="append", default=[]
        )
    args = parser.parse_args()
    try:
        roots = args.skill_root or [DEFAULT_SKILL_ROOT]
        if args.command == "recon":
            workflow = recon_workflow(
                args.target,
                args.campaign_id,
                name=args.name,
                cao_repo=args.cao_repo,
                skill_roots=roots,
            )
        elif args.command == "planning":
            workflow = planning_workflow(
                args.upstream_run,
                name=args.name,
                cao_repo=args.cao_repo,
                skill_roots=roots,
            )
        else:
            workflow = mining_workflow(
                args.upstream_run,
                name=args.name,
                cao_repo=args.cao_repo,
                skill_roots=roots,
            )
        atomic_json(args.output.resolve(), workflow)
        print(
            json.dumps(
                {
                    "ok": True,
                    "workflow": str(args.output.resolve()),
                    "phase": workflow["campaign"]["phase"],
                    "campaign_id": workflow["campaign"]["id"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (CampaignError, AdaptiveAuditError, AgentCellError, OSError) as error:
        print(f"campaign error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
