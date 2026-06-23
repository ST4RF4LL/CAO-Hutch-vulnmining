#!/usr/bin/env python3
"""Exercise a complete adaptive Hutch campaign without invoking CAO or an LLM."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from adaptive_audit import (
    DEFAULT_MAX_MODULES_PER_TASK,
    DEFAULT_MAX_SOURCE_FILES_PER_TASK,
    atomic_json,
    build_coverage_summary,
    build_inventories,
    validate_audit_plan,
)
from agent_cells import prepare_agent_cells
from generate_cao_native_flow import (
    execution_batches,
    load_and_validate,
    write_output,
)
from hutch_campaign import mining_workflow, planning_workflow
from hutch_flow_state import finalize, start_stage, validate_stage
from hutch_paths import default_cao_repo, default_skill_roots
from run_cao_flow import create_snapshot, now, source_fingerprint


ROOT = Path(__file__).resolve().parents[1]
try:
    DEFAULT_CAO_REPO = default_cao_repo()
except RuntimeError:
    DEFAULT_CAO_REPO = ROOT.parent / "cli-agent-orchestrator"
DEFAULT_SKILL_ROOTS = default_skill_roots()


class DryRunError(RuntimeError):
    pass


def deterministic_plan(inventory: dict[str, Any], max_concurrency: int) -> dict[str, Any]:
    """Create a bounded fixture plan; production planning remains Agent-owned."""
    modules = sorted(
        inventory["modules"],
        key=lambda module: (-int(module.get("source_file_count", 0)), module["path"]),
    )
    groups: list[list[dict[str, Any]]] = []
    for module in modules:
        files = int(module.get("source_file_count", 0))
        selected: list[dict[str, Any]] | None = None
        for group in groups:
            group_files = sum(int(item.get("source_file_count", 0)) for item in group)
            if (
                len(group) < DEFAULT_MAX_MODULES_PER_TASK
                and group_files + files <= DEFAULT_MAX_SOURCE_FILES_PER_TASK
            ):
                selected = group
                break
        if selected is None:
            selected = []
            groups.append(selected)
        selected.append(module)
    tasks = []
    for index, group in enumerate(groups, start=1):
        tasks.append(
            {
                "id": f"dry-shard-{index:03d}",
                "title": f"Dry-run shard {index}",
                "module_ids": [module["id"] for module in group],
                "paths": sorted(module["path"] for module in group),
                "skills": [],
                "threat_ids": [],
                "objective": "Exercise the bounded shard contract only; do not treat fixture output as a security review.",
            }
        )
    plan = {
        "schema": "hutch.audit-plan.v1",
        "strategy": "whole_repo" if len(tasks) == 1 else "sharded",
        "max_concurrency": max_concurrency,
        "dry_run_fixture": True,
        "tasks": tasks,
    }
    validate_audit_plan(inventory, plan)
    return plan


def markdown_for(stage: dict[str, Any], note: str) -> str:
    sections = stage.get("required_sections", []) or ["Dry Run"]
    return "\n\n".join(
        f"## {heading}\n\n{note}" for heading in sections
    ) + "\n"


def result_for(stage: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "hutch.result.v1",
        "task_id": stage["task_id"],
        "stage": stage["id"],
        "status": "done",
        "summary": "Dry-run fixture validated; no security analysis was performed.",
        "artifacts": [stage["artifact"], *stage.get("required_artifacts", [])],
        "findings": [],
        "limitations": ["Infrastructure dry run only; no LLM or CAO worker was invoked."],
        "dry_run_fixture": True,
    }


def write_stage_fixture(
    run_dir: Path,
    stage: dict[str, Any],
    *,
    json_artifacts: dict[str, dict[str, Any]] | None = None,
) -> None:
    artifact = run_dir / stage["artifact"]
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        markdown_for(
            stage,
            "Contract-valid dry-run fixture. This content is not architecture, threat, or vulnerability evidence.",
        ),
        encoding="utf-8",
    )
    for relative, value in (json_artifacts or {}).items():
        atomic_json(run_dir / relative, {**value, "dry_run_fixture": True})
    atomic_json(
        run_dir / "outbox" / f"{stage['task_id']}.result.json", result_for(stage)
    )


def prepare_phase_run(
    root: Path,
    workflow: dict[str, Any],
    bundle: dict[str, Any],
    snapshot: Path,
    fingerprint: dict[str, Any],
    repository_inventory: dict[str, Any],
    module_inventory: dict[str, Any],
) -> Path:
    run_dir = root / f"{workflow['campaign']['phase']}-run"
    for directory in ("artifacts", "inbox", "outbox", "shared", "tmp"):
        (run_dir / directory).mkdir(parents=True, exist_ok=False)
    (run_dir / "shared" / "target-snapshot").symlink_to(snapshot, target_is_directory=True)
    atomic_json(run_dir / "shared/source-fingerprint.json", fingerprint)
    atomic_json(run_dir / "shared/repository-inventory.json", repository_inventory)
    atomic_json(run_dir / "shared/modules.json", module_inventory)
    atomic_json(run_dir / "workflow.json", workflow)
    profiles_dir = Path(bundle["flow"]).parent / "profiles"
    profile_names = {
        agent["id"]: f"{workflow['name']}-{agent['id']}"
        for agent in workflow["agents"]
    }
    cells = prepare_agent_cells(
        workflow,
        run_dir,
        (
            {
                "id": agent["id"],
                "profile": profile_names[agent["id"]],
                "skills": agent.get("skills", []),
                "profile_source": profiles_dir / f"{profile_names[agent['id']]}.md",
            }
            for agent in workflow["agents"]
        ),
    )
    state = {
        "schema": "hutch.cao-state.v1",
        "run_id": run_dir.name,
        "workflow": workflow["name"],
        "status": "prepared",
        "created_at": now(),
        "dry_run": True,
        "campaign": workflow["campaign"],
        "target_fingerprint": fingerprint,
        "stages": {
            stage["id"]: {
                "status": "pending",
                "task_id": stage["task_id"],
                "agent_profile": profile_names[stage["agent"]],
                "agent_cell": stage["agent"],
                "workspace": cells[stage["agent"]]["workspace"],
                "terminal_id": f"dry-run-{stage['id']}",
            }
            for stage in workflow["stages"]
        },
    }
    atomic_json(run_dir / "state.json", state)
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    return run_dir


def bundle_workflow(
    root: Path, phase: str, workflow: dict[str, Any]
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    workflow_path = root / "workflows" / f"{phase}.json"
    atomic_json(workflow_path, workflow)
    validated = load_and_validate(workflow_path)
    bundle = write_output(
        workflow_path,
        validated,
        root / "bundles" / phase,
        Path(validated["cao_repo"]),
    )
    return workflow_path, validated, bundle


def validate_fixture_stage(
    run_dir: Path,
    stage: dict[str, Any],
    *,
    json_artifacts: dict[str, dict[str, Any]] | None = None,
) -> None:
    start_stage(run_dir, stage["id"], f"dry-run-{stage['id']}")
    write_stage_fixture(run_dir, stage, json_artifacts=json_artifacts)
    validate_stage(run_dir, stage["id"])


def evidence_path(snapshot: Path, module: dict[str, Any]) -> str:
    for descriptor in module.get("build_descriptors", []):
        if (snapshot / descriptor).is_file():
            return descriptor
    module_root = snapshot if module["path"] == "." else snapshot / module["path"]
    for candidate in sorted(module_root.rglob("*")):
        if candidate.is_file():
            return candidate.relative_to(snapshot).as_posix()
    raise DryRunError(f"module has no evidence file: {module['id']}")


def run_dry_campaign(
    target: Path,
    output: Path,
    *,
    cao_repo: Path,
    skill_root: Path,
    max_concurrency: int,
) -> dict[str, Any]:
    target = target.resolve()
    if not (target / ".git").exists():
        raise DryRunError(f"target is not a Git checkout: {target}")
    if output.exists():
        raise DryRunError(f"output already exists: {output}")
    (output / "workflows").mkdir(parents=True)
    (output / "bundles").mkdir()
    fingerprint = source_fingerprint(target)
    snapshot = output / "source-snapshot"
    snapshot_stats = create_snapshot(target, snapshot, 2_097_152)
    repository_inventory, module_inventory = build_inventories(snapshot)
    atomic_json(output / "repository-inventory.json", repository_inventory)
    atomic_json(output / "modules.json", module_inventory)
    atomic_json(output / "snapshot-manifest.json", snapshot_stats)

    recon_source = json.loads(
        (ROOT / "workflows/djl-recon-threat-intelligence.yaml").read_text(encoding="utf-8")
    )
    recon_source["target"] = str(target)
    recon_source["cao_repo"] = str(cao_repo.resolve())
    recon_source["skill_roots"] = [str(skill_root.resolve())]
    recon_source["campaign"] = {
        "schema": "hutch.campaign.v1",
        "id": f"djl-dry-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "phase": "recon",
    }
    _, recon, recon_bundle = bundle_workflow(output, "recon", recon_source)
    recon_run = prepare_phase_run(
        output, recon, recon_bundle, snapshot, fingerprint, repository_inventory, module_inventory
    )
    module_entries = [
        {"module_id": module["id"], "path": module["path"]}
        for module in module_inventory["modules"]
    ]
    recon_json = {
        "repository-intelligence": {
            "artifacts/intelligence/architecture.json": {
                "schema": "hutch.architecture-intelligence.v1",
                "modules": module_entries,
                "components": [],
            },
            "artifacts/intelligence/business-flows.json": {
                "schema": "hutch.business-flows.v1",
                "flows": [],
            },
            "artifacts/intelligence/external-interfaces.json": {
                "schema": "hutch.external-interfaces.v1",
                "interfaces": [],
            },
        },
        "threat-intelligence": {
            "artifacts/intelligence/attack-surface.json": {
                "schema": "hutch.attack-surface.v1",
                "surfaces": [],
            },
            "artifacts/intelligence/trust-boundaries.json": {
                "schema": "hutch.trust-boundaries.v1",
                "boundaries": [],
            },
            "artifacts/intelligence/threat-model.json": {
                "schema": "hutch.threat-model.v1",
                "threats": [],
            },
        },
    }
    for stage in recon["stages"]:
        validate_fixture_stage(
            recon_run, stage, json_artifacts=recon_json.get(stage["id"], {})
        )
    recon_final = finalize(recon_run)

    planning_source = planning_workflow(
        recon_run,
        cao_repo=cao_repo,
        skill_roots=[skill_root],
    )
    _, planning, planning_bundle = bundle_workflow(output, "planning", planning_source)
    planning_run = prepare_phase_run(
        output,
        planning,
        planning_bundle,
        snapshot,
        fingerprint,
        repository_inventory,
        module_inventory,
    )
    intelligence_source = recon_run / "artifacts/intelligence"
    shutil.copytree(intelligence_source, planning_run / "shared/intelligence")
    plan = deterministic_plan(module_inventory, max_concurrency)
    planning_stage = planning["stages"][0]
    validate_fixture_stage(
        planning_run,
        planning_stage,
        json_artifacts={"artifacts/audit-plan.json": plan},
    )
    planning_final = finalize(planning_run)

    mining_source = mining_workflow(
        planning_run,
        cao_repo=cao_repo,
        skill_roots=[skill_root],
    )
    _, mining, mining_bundle = bundle_workflow(output, "mining", mining_source)
    mining_run = prepare_phase_run(
        output,
        mining,
        mining_bundle,
        snapshot,
        fingerprint,
        repository_inventory,
        module_inventory,
    )
    atomic_json(mining_run / "shared/audit-plan.json", plan)
    modules_by_id = {module["id"]: module for module in module_inventory["modules"]}
    for stage in mining["stages"]:
        if stage.get("coverage_contract"):
            coverage = {
                "schema": "hutch.coverage.v1",
                "task_id": stage["task_id"],
                "stage": stage["id"],
                "dry_run_fixture": True,
                "modules": [
                    {
                        "module_id": module_id,
                        "status": "audited",
                        "reviewed_file_count": 1,
                        "evidence": [
                            {
                                "path": evidence_path(snapshot, modules_by_id[module_id]),
                                "observation": "Dry-run path fixture; no security review performed.",
                            }
                        ],
                    }
                    for module_id in stage["coverage_contract"]["module_ids"]
                ],
            }
            validate_fixture_stage(
                mining_run,
                stage,
                json_artifacts={stage["coverage_contract"]["artifact"]: coverage},
            )
            continue
        if stage.get("coverage_gate"):
            build_coverage_summary(mining, mining_run, stage)
        validate_fixture_stage(mining_run, stage)
    mining_final = finalize(mining_run)

    batches = execution_batches(mining)
    summary = {
        "schema": "hutch.campaign-dry-run.v1",
        "dry_run": True,
        "target": str(target),
        "output": str(output),
        "source_fingerprint": fingerprint,
        "snapshot": snapshot_stats,
        "repository": repository_inventory,
        "module_count": module_inventory["module_count"],
        "audit_task_count": len(plan["tasks"]),
        "max_concurrency": max_concurrency,
        "mining_stage_count": len(mining["stages"]),
        "mining_batches": [[stage["id"] for stage in batch] for batch in batches],
        "profiles": {
            "recon": len(recon_bundle["profiles"]),
            "planning": len(planning_bundle["profiles"]),
            "mining": len(mining_bundle["profiles"]),
        },
        "phases": {
            "recon": recon_final,
            "planning": planning_final,
            "mining": mining_final,
        },
        "coverage_summary": json.loads(
            (mining_run / "artifacts/coverage-summary.json").read_text(encoding="utf-8")
        ),
        "cao_invoked": False,
        "llm_invoked": False,
        "limitations": [
            "This validates compilation, contracts, Agent Cell preparation, state transitions, and coverage gating only.",
            "Fixture reports contain no architecture, threat, or vulnerability conclusions.",
            "CAO API, tmux lifecycle, OpenCode execution, and runtime concurrency are not exercised.",
        ],
    }
    atomic_json(output / "dry-run-report.json", summary)
    (output / "DRY-RUN.md").write_text(
        "# Adaptive Audit Campaign Dry Run\n\n"
        f"- Target: `{target}`\n"
        f"- Git HEAD: `{fingerprint['head']}`\n"
        f"- Snapshot files: {snapshot_stats['copied_files']}\n"
        f"- Source modules: {module_inventory['module_count']}\n"
        f"- Audit shards: {len(plan['tasks'])}\n"
        f"- Mining stages: {len(mining['stages'])}\n"
        f"- Maximum concurrency: {max_concurrency}\n"
        f"- Coverage gaps: {summary['coverage_summary']['gap_count']}\n"
        "- Recon / Planning / Mining fixture states: completed / completed / completed\n"
        "- CAO invoked: no\n"
        "- LLM invoked: no\n\n"
        "This report proves the orchestration and artifact contracts only. It is not a source audit report.\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cao-repo", type=Path, default=DEFAULT_CAO_REPO)
    parser.add_argument(
        "--skill-root",
        type=Path,
        default=DEFAULT_SKILL_ROOTS[0] if DEFAULT_SKILL_ROOTS else ROOT / "third_party" / "skills",
    )
    parser.add_argument("--max-concurrency", type=int, default=8)
    args = parser.parse_args()
    output = args.output or (
        ROOT / "dry-runs" / f"djl-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    try:
        summary = run_dry_campaign(
            args.target,
            output.resolve(),
            cao_repo=args.cao_repo,
            skill_root=args.skill_root,
            max_concurrency=args.max_concurrency,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "output": summary["output"],
                    "modules": summary["module_count"],
                    "audit_tasks": summary["audit_task_count"],
                    "stages": summary["mining_stage_count"],
                    "coverage_gaps": summary["coverage_summary"]["gap_count"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as error:
        print(f"dry run failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
