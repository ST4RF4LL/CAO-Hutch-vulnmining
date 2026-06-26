#!/usr/bin/env python3
"""Prepare one immutable Hutch run for a CAO-native flow script."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_cells import prepare_agent_cells
from run_cao_flow import (
    atomic_json,
    create_snapshot,
    now,
    prepare_shared_contracts,
    source_fingerprint,
    task_document,
)
from hutch_paths import expand_config_path, expand_config_paths, hutch_generated_dir, hutch_runs_dir


ROOT = Path(__file__).resolve().parents[1]


def load_workflow(path: Path) -> dict[str, Any]:
    workflow = json.loads(path.read_text(encoding="utf-8"))
    if workflow.get("schema") != "hutch.cao-workflow.v1":
        raise ValueError("workflow must use hutch.cao-workflow.v1")
    if workflow.get("skill_roots"):
        workflow["skill_roots"] = [
            str(item) for item in expand_config_paths(workflow.get("skill_roots", []))
        ]
    return workflow


def unique_run_dir(workflow_name: str) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    return hutch_runs_dir() / f"{workflow_name}-{stamp}"


def prepare(workflow_path: Path, profiles_dir: Path | None = None) -> dict[str, Any]:
    workflow = load_workflow(workflow_path.resolve())
    target = expand_config_path(workflow["target"])
    if not (target / ".git").exists():
        raise ValueError(f"target is not a Git checkout: {target}")

    run_dir = unique_run_dir(workflow["name"])
    for directory in ("artifacts", "inbox", "outbox", "shared", "tmp"):
        (run_dir / directory).mkdir(parents=True, exist_ok=False)

    fingerprint = source_fingerprint(target)
    snapshot = create_snapshot(
        target,
        run_dir / "shared" / "target-snapshot",
        int(workflow.get("snapshot", {}).get("max_file_bytes", 2_097_152)),
    )
    atomic_json(run_dir / "shared" / "source-fingerprint.json", fingerprint)
    atomic_json(run_dir / "shared" / "snapshot-manifest.json", snapshot)
    prepare_shared_contracts(workflow, run_dir)
    atomic_json(run_dir / "workflow.json", workflow)

    generated_profiles = {
        agent["id"]: f"{workflow['name']}-{agent['id']}" for agent in workflow["agents"]
    }
    profiles_dir = (
        profiles_dir.resolve()
        if profiles_dir
        else hutch_generated_dir() / workflow["name"] / "profiles"
    )
    cells = prepare_agent_cells(
        workflow,
        run_dir,
        (
            {
                "id": agent["id"],
                "profile": generated_profiles[agent["id"]],
                "skills": agent.get("skills", []),
                "skill_sources": agent.get("skill_sources", {}),
                "profile_source": profiles_dir / f"{generated_profiles[agent['id']]}.md",
            }
            for agent in workflow["agents"]
        ),
    )
    for stage in workflow["stages"]:
        document = task_document(stage, run_dir)
        document["agent_profile"] = generated_profiles[stage["agent"]]
        document["agent_cell"] = cells[stage["agent"]]
        document["finding_contract"] = {
            "required_for_candidates": [
                "id",
                "title",
                "severity",
                "confidence",
                "status",
                "weakness",
                "evidence",
                "impact",
                "assumptions",
            ],
            "allowed_status": [
                "candidate",
                "confirmed",
                "likely",
                "needs-info",
                "false-positive",
            ],
            "evidence_fields": ["path", "line", "symbol", "observation"],
        }
        atomic_json(run_dir / "inbox" / f"{stage['task_id']}.task.json", document)

    state = {
        "schema": "hutch.cao-state.v1",
        "run_id": run_dir.name,
        "workflow": workflow["name"],
        "status": "prepared",
        "created_at": now(),
        "cao_flow": workflow["name"],
        "cao_session": f"cao-flow-{workflow['name']}",
        "target_fingerprint": fingerprint,
        "snapshot": snapshot,
        "campaign": workflow.get("campaign"),
        "stages": {
            stage["id"]: {
                "status": "pending",
                "task_id": stage["task_id"],
                "agent_profile": generated_profiles[stage["agent"]],
                "agent_cell": stage["agent"],
                "workspace": cells[stage["agent"]]["workspace"],
            }
            for stage in workflow["stages"]
        },
    }
    atomic_json(run_dir / "state.json", state)
    (run_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "ts": now(),
                "event": "run_prepared_by_cao_flow",
                "flow": workflow["name"],
                "session": f"cao-flow-{workflow['name']}",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema": "hutch.cao-run-manifest.v1",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "workflow": str(run_dir / "workflow.json"),
        "state_file": str(run_dir / "state.json"),
        "target_snapshot": str(run_dir / "shared" / "target-snapshot"),
        "agent_cells": cells,
        "campaign": workflow.get("campaign"),
        "stages": [
            {
                "id": stage["id"],
                "task_id": stage["task_id"],
                "task_file": str(run_dir / "inbox" / f"{stage['task_id']}.task.json"),
                "profile": generated_profiles[stage["agent"]],
                "agent_cell": stage["agent"],
                "workspace": cells[stage["agent"]]["workspace"],
                "depends_on": stage.get("depends_on", []),
            }
            for stage in workflow["stages"]
        ],
    }
    atomic_json(run_dir / "manifest.json", manifest)
    return {
        "execute": True,
        "output": {
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "manifest": str(run_dir / "manifest.json"),
            "state_file": str(run_dir / "state.json"),
            "target_snapshot": str(run_dir / "shared" / "target-snapshot"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    parser.add_argument("--profiles-dir", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.workflow, args.profiles_dir), ensure_ascii=False))
        return 0
    except Exception as error:
        print(f"prepare failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
