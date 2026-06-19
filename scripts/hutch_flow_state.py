#!/usr/bin/env python3
"""Validate artifacts and advance durable state for a CAO-native Hutch flow."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from run_cao_flow import append_event, atomic_json, now, source_fingerprint, validate_result


class StateError(RuntimeError):
    pass


def load_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    workflow = json.loads((run_dir / "workflow.json").read_text(encoding="utf-8"))
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    return workflow, state


def stage_by_id(workflow: dict[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in workflow["stages"]:
        if stage["id"] == stage_id:
            return stage
    raise StateError(f"unknown stage: {stage_id}")


def validate_findings(stage: dict[str, Any], run_dir: Path) -> None:
    result_path = run_dir / "outbox" / f"{stage['task_id']}.result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise StateError("result findings must be an array (empty is valid)")
    required = {"id", "title", "severity", "confidence", "status", "weakness", "evidence", "impact", "assumptions"}
    allowed = {"candidate", "confirmed", "likely", "needs-info", "false-positive"}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise StateError(f"finding {index} must be an object")
        missing = sorted(required - set(finding))
        if missing:
            raise StateError(f"finding {index} is missing fields: {missing}")
        if finding["status"] not in allowed:
            raise StateError(f"finding {index} has invalid status: {finding['status']}")
        if not isinstance(finding["evidence"], list) or not finding["evidence"]:
            raise StateError(f"finding {index} requires source evidence")


def validate_stage(run_dir: Path, stage_id: str) -> dict[str, Any]:
    workflow, state = load_run(run_dir)
    stage = stage_by_id(workflow, stage_id)
    for dependency in stage.get("depends_on", []):
        if state["stages"][dependency]["status"] != "done":
            raise StateError(f"dependency {dependency} is not validated")
    valid, reason = validate_result(stage, run_dir)
    if not valid:
        state["stages"][stage_id].update({"status": "invalid", "last_error": reason})
        atomic_json(run_dir / "state.json", state)
        append_event(run_dir, "stage_validation_failed", stage=stage_id, reason=reason)
        raise StateError(reason)
    try:
        validate_findings(stage, run_dir)
    except StateError as error:
        state["stages"][stage_id].update(
            {"status": "invalid", "last_error": str(error)}
        )
        atomic_json(run_dir / "state.json", state)
        append_event(
            run_dir, "stage_validation_failed", stage=stage_id, reason=str(error)
        )
        raise
    state["status"] = "running"
    state["current_stage"] = stage_id
    state["stages"][stage_id].update({"status": "done", "validated_at": now()})
    state["stages"][stage_id].pop("last_error", None)
    atomic_json(run_dir / "state.json", state)
    append_event(run_dir, "stage_validated", stage=stage_id, task_id=stage["task_id"])
    return {"ok": True, "stage": stage_id, "status": "done"}


def start_stage(run_dir: Path, stage_id: str, terminal_id: str) -> dict[str, Any]:
    workflow, state = load_run(run_dir)
    stage = stage_by_id(workflow, stage_id)
    for dependency in stage.get("depends_on", []):
        if state["stages"][dependency]["status"] != "done":
            raise StateError(f"dependency {dependency} is not validated")
    stage_state = state["stages"][stage_id]
    attempt = int(stage_state.get("attempt", 0)) + 1
    stage_state.update(
        {
            "status": "running",
            "attempt": attempt,
            "terminal_id": terminal_id,
            "started_at": now(),
        }
    )
    state["status"] = "running"
    state["current_stage"] = stage_id
    atomic_json(run_dir / "state.json", state)
    append_event(
        run_dir,
        "stage_assigned_by_cao",
        stage=stage_id,
        task_id=stage["task_id"],
        terminal_id=terminal_id,
        attempt=attempt,
    )
    return {"ok": True, "stage": stage_id, "status": "running", "attempt": attempt}


def await_stage(run_dir: Path, stage_id: str, timeout: int, interval: float) -> dict[str, Any]:
    workflow, _ = load_run(run_dir)
    stage = stage_by_id(workflow, stage_id)
    result_path = run_dir / "outbox" / f"{stage['task_id']}.result.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result_path.is_file():
            return validate_stage(run_dir, stage_id)
        time.sleep(interval)
    raise StateError(f"timed out after {timeout}s waiting for {result_path.name}")


def finalize(run_dir: Path) -> dict[str, Any]:
    workflow, state = load_run(run_dir)
    incomplete = [stage["id"] for stage in workflow["stages"] if state["stages"][stage["id"]]["status"] != "done"]
    if incomplete:
        raise StateError(f"cannot finalize; stages are not validated: {incomplete}")

    current = source_fingerprint(Path(workflow["target"]).resolve())
    original = state["target_fingerprint"]
    if current != original:
        state["status"] = "failed-integrity"
        state["finished_at"] = now()
        state["integrity"] = {"ok": False, "before": original, "after": current}
        atomic_json(run_dir / "state.json", state)
        append_event(run_dir, "target_integrity_failed", before=original, after=current)
        raise StateError("target Git fingerprint changed during the flow")

    findings: list[dict[str, Any]] = []
    for stage in workflow["stages"]:
        result_path = run_dir / "outbox" / f"{stage['task_id']}.result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        for finding in result.get("findings", []):
            findings.append({"stage": stage["id"], **finding})
    with (run_dir / "findings.jsonl").open("w", encoding="utf-8") as stream:
        for finding in findings:
            stream.write(json.dumps(finding, ensure_ascii=False) + "\n")

    state["status"] = "completed"
    state["current_stage"] = None
    state["finished_at"] = now()
    state["integrity"] = {"ok": True, "before": original, "after": current}
    state["finding_records"] = len(findings)
    atomic_json(run_dir / "state.json", state)
    append_event(run_dir, "run_completed", finding_records=len(findings))
    return {"ok": True, "run_id": state["run_id"], "status": "completed", "finding_records": len(findings)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("run_dir", type=Path)
    validate_parser.add_argument("stage_id")
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("run_dir", type=Path)
    start_parser.add_argument("stage_id")
    start_parser.add_argument("terminal_id")
    await_parser = subparsers.add_parser("await")
    await_parser.add_argument("run_dir", type=Path)
    await_parser.add_argument("stage_id")
    await_parser.add_argument("--timeout", type=int, default=1800)
    await_parser.add_argument("--interval", type=float, default=5.0)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("run_dir", type=Path)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            value = validate_stage(args.run_dir.resolve(), args.stage_id)
        elif args.command == "start":
            value = start_stage(args.run_dir.resolve(), args.stage_id, args.terminal_id)
        elif args.command == "await":
            value = await_stage(
                args.run_dir.resolve(), args.stage_id, args.timeout, args.interval
            )
        elif args.command == "finalize":
            value = finalize(args.run_dir.resolve())
        else:
            value = json.loads((args.run_dir.resolve() / "state.json").read_text(encoding="utf-8"))
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
