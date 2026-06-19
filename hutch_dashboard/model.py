"""Build a stable dashboard view from Hutch run directories and CAO snapshots."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


class RunNotFound(KeyError):
    pass


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    except (OSError, json.JSONDecodeError):
        pass
    return records


def duration_seconds(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
    except ValueError:
        return None


class RunRepository:
    """Read-only projection over on-disk run evidence."""

    def __init__(
        self,
        runs_dir: Path,
        terminal_logs: Path | None = None,
        cao_db: Path | None = None,
    ) -> None:
        self.runs_dir = runs_dir.resolve()
        self.terminal_logs = (
            terminal_logs
            or Path.home() / ".aws" / "cli-agent-orchestrator" / "logs" / "terminal"
        ).resolve()
        self.cao_db = (
            cao_db
            or Path.home()
            / ".aws"
            / "cli-agent-orchestrator"
            / "db"
            / "cli-agent-orchestrator.db"
        ).resolve()

    def list_runs(self, status: str | None = None) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        if not self.runs_dir.is_dir():
            return runs
        for state_path in self.runs_dir.glob("*/state.json"):
            state = read_json(state_path, {})
            if not isinstance(state, dict) or not state.get("run_id"):
                continue
            if status and state.get("status") != status:
                continue
            stages = state.get("stages", {})
            done = sum(1 for item in stages.values() if item.get("status") == "done")
            runs.append(
                {
                    "run_id": state["run_id"],
                    "workflow": state.get("workflow", state_path.parent.name),
                    "status": state.get("status", "unknown"),
                    "created_at": state.get("created_at"),
                    "finished_at": state.get("finished_at"),
                    "duration_seconds": duration_seconds(
                        state.get("created_at"), state.get("finished_at")
                    ),
                    "stages_done": done,
                    "stages_total": len(stages),
                    "cao_session": state.get("cao_session"),
                    "finding_records": state.get("finding_records"),
                }
            )
        runs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return runs

    def get_run(self, run_id: str) -> dict[str, Any]:
        candidates = {item["run_id"] for item in self.list_runs()}
        if run_id not in candidates:
            raise RunNotFound(run_id)
        run_dir = (self.runs_dir / run_id).resolve()
        if run_dir.parent != self.runs_dir:
            raise RunNotFound(run_id)

        state = read_json(run_dir / "state.json", {})
        workflow = read_json(run_dir / "workflow.json", {})
        manifest = read_json(run_dir / "manifest.json", {})
        events = read_json_lines(run_dir / "events.jsonl")
        stage_defs = {
            stage["id"]: stage
            for stage in workflow.get("stages", [])
            if isinstance(stage, dict) and stage.get("id")
        }
        agents = [self._agent(run_dir, state, workflow, stage_id, stage, events) for stage_id, stage in stage_defs.items()]
        supervisor = self._supervisor(state, workflow, agents)
        if supervisor:
            agents.insert(0, supervisor)

        deliverables = self._flow_deliverables(run_dir, workflow)
        final_stage = workflow.get("stages", [])[-1]["id"] if workflow.get("stages") else None
        summary = next(
            (
                agent.get("result_summary")
                for agent in agents
                if agent.get("stage") == final_stage and agent.get("result_summary")
            ),
            None,
        )
        return {
            "run_id": state.get("run_id", run_id),
            "workflow": state.get("workflow", workflow.get("name", run_id)),
            "workflow_version": workflow.get("version"),
            "status": state.get("status", "unknown"),
            "created_at": state.get("created_at"),
            "finished_at": state.get("finished_at"),
            "duration_seconds": duration_seconds(state.get("created_at"), state.get("finished_at")),
            "cao_flow": state.get("cao_flow"),
            "cao_session": state.get("cao_session"),
            "target": state.get("target_fingerprint", {}).get("target") or workflow.get("target"),
            "target_head": state.get("target_fingerprint", {}).get("head"),
            "integrity": state.get("integrity") or {
                "ok": state.get("target_fingerprint") == state.get("final_target_fingerprint")
                if state.get("final_target_fingerprint")
                else None
            },
            "summary": summary,
            "stage_count": len(stage_defs),
            "agents": agents,
            "deliverables": deliverables,
            "event_count": len(events),
            "manifest_present": bool(manifest),
        }

    def _agent(
        self,
        run_dir: Path,
        state: dict[str, Any],
        workflow: dict[str, Any],
        stage_id: str,
        stage: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        stage_state = state.get("stages", {}).get(stage_id, {})
        task_id = stage.get("task_id") or stage_state.get("task_id")
        task = read_json(run_dir / "inbox" / f"{task_id}.task.json", {}) if task_id else {}
        result_path = run_dir / "outbox" / f"{task_id}.result.json" if task_id else None
        result = read_json(result_path, {}) if result_path else {}
        profile = (
            task.get("agent_profile")
            or stage_state.get("agent_profile")
            or stage.get("profile")
            or stage.get("agent")
        )
        assignments = self._assignments(stage_id, stage_state, events, state.get("cao_session"))
        findings = result.get("findings", []) if isinstance(result, dict) else []
        statuses = Counter(
            finding.get("status", "unknown")
            for finding in findings
            if isinstance(finding, dict)
        )
        artifact_path = stage.get("artifact") or result.get("artifacts", [None])[0]
        outputs: list[dict[str, Any]] = []
        if artifact_path:
            output = self._deliverable(run_dir, artifact_path, "artifact", stage_id)
            if output:
                outputs.append(output)
        if result_path and result_path.is_file():
            output = self._deliverable(
                run_dir, str(result_path.relative_to(run_dir)), "result", stage_id
            )
            if output:
                outputs.append(output)
        return {
            "stage": stage_id,
            "task_id": task_id,
            "profile": profile,
            "provider": stage_state.get("provider") or stage.get("provider") or workflow.get("provider"),
            "status": stage_state.get("status", result.get("status", "unknown")),
            "depends_on": stage.get("depends_on", []),
            "inputs": task.get("inputs", stage.get("inputs", [])),
            "started_at": stage_state.get("started_at"),
            "finished_at": stage_state.get("validated_at") or stage_state.get("finished_at"),
            "assignments": assignments,
            "result_summary": result.get("summary"),
            "finding_count": len(findings),
            "finding_statuses": dict(statuses),
            "deliverables": outputs,
        }

    def _assignments(
        self,
        stage_id: str,
        stage_state: dict[str, Any],
        events: list[dict[str, Any]],
        default_session: str | None,
    ) -> list[dict[str, Any]]:
        raw: list[dict[str, Any]] = []
        for event in events:
            if event.get("stage") != stage_id:
                continue
            if event.get("event") == "stage_assigned_by_cao" and event.get("terminal_id"):
                raw.append(
                    {
                        "terminal_id": event["terminal_id"],
                        "session": default_session,
                        "attempt": event.get("attempt"),
                        "started_at": event.get("ts"),
                    }
                )
            elif event.get("event") == "stage_started" and event.get("session"):
                raw.append(
                    {
                        "terminal_id": None,
                        "session": event["session"],
                        "attempt": event.get("attempt"),
                        "started_at": event.get("ts"),
                    }
                )
        if not raw and (stage_state.get("terminal_id") or stage_state.get("session")):
            raw.append(
                {
                    "terminal_id": stage_state.get("terminal_id"),
                    "session": stage_state.get("session") or default_session,
                    "attempt": stage_state.get("attempt"),
                    "started_at": stage_state.get("started_at"),
                }
            )
        seen: set[tuple[Any, Any]] = set()
        assignments: list[dict[str, Any]] = []
        for item in raw:
            key = (item.get("terminal_id"), item.get("session"))
            if key in seen:
                continue
            seen.add(key)
            metadata = self._terminal_metadata(item.get("terminal_id"))
            assignments.append(
                {
                    **item,
                    "session": metadata.get("session_name") or item.get("session"),
                    "window": metadata.get("window_name"),
                    "provider": metadata.get("provider"),
                    "working_directory": metadata.get("working_directory"),
                    "caller_id": metadata.get("caller_id"),
                    "snapshot_available": metadata.get("snapshot_available", False),
                    "scrollback_available": metadata.get("scrollback_available", False),
                }
            )
        return assignments

    def _terminal_metadata(self, terminal_id: str | None) -> dict[str, Any]:
        if not terminal_id:
            return {}
        snapshot_path = self.terminal_logs / f"{terminal_id}.snapshot.json"
        snapshot = read_json(snapshot_path, {})
        if isinstance(snapshot, dict) and snapshot:
            snapshot["snapshot_available"] = True
            snapshot["scrollback_available"] = (
                self.terminal_logs / f"{terminal_id}.scrollback"
            ).is_file()
            return snapshot
        if self.cao_db.is_file():
            try:
                with sqlite3.connect(self.cao_db) as connection:
                    row = connection.execute(
                        "SELECT id, tmux_session, tmux_window, provider, agent_profile "
                        "FROM terminals WHERE id = ?",
                        (terminal_id,),
                    ).fetchone()
                if row:
                    return {
                        "terminal_id": row[0],
                        "session_name": row[1],
                        "window_name": row[2],
                        "provider": row[3],
                        "agent_profile": row[4],
                        "snapshot_available": False,
                        "scrollback_available": False,
                    }
            except sqlite3.Error:
                pass
        return {}

    def _supervisor(
        self,
        state: dict[str, Any],
        workflow: dict[str, Any],
        agents: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        session = state.get("cao_session")
        if not session:
            return None
        caller_ids = {
            assignment.get("caller_id")
            for agent in agents
            for assignment in agent.get("assignments", [])
            if assignment.get("caller_id")
        }
        assignments = []
        for terminal_id in sorted(caller_ids):
            metadata = self._terminal_metadata(terminal_id)
            assignments.append(
                {
                    "terminal_id": terminal_id,
                    "session": metadata.get("session_name") or session,
                    "window": metadata.get("window_name"),
                    "provider": metadata.get("provider") or workflow.get("provider"),
                    "working_directory": metadata.get("working_directory"),
                    "caller_id": None,
                    "snapshot_available": metadata.get("snapshot_available", False),
                    "scrollback_available": metadata.get("scrollback_available", False),
                }
            )
        return {
            "stage": "flow-supervisor",
            "task_id": None,
            "profile": f"{state.get('workflow', workflow.get('name'))}-supervisor",
            "provider": workflow.get("provider"),
            "status": state.get("status"),
            "depends_on": [],
            "inputs": ["manifest.json", "state.json", "workflow.json"],
            "started_at": state.get("created_at"),
            "finished_at": state.get("finished_at"),
            "assignments": assignments or [{"terminal_id": None, "session": session}],
            "result_summary": "Coordinates CAO worker terminals and advances Hutch artifact gates.",
            "finding_count": 0,
            "finding_statuses": {},
            "deliverables": [],
        }

    def _flow_deliverables(
        self, run_dir: Path, workflow: dict[str, Any]
    ) -> list[dict[str, Any]]:
        deliverables: list[dict[str, Any]] = []
        stages = workflow.get("stages", [])
        final_stage = stages[-1].get("id") if stages else None
        stage_paths: set[str] = set()
        for stage in stages:
            stage_id = stage.get("id")
            task_id = stage.get("task_id")
            paths = [stage.get("artifact")]
            if task_id:
                paths.append(f"outbox/{task_id}.result.json")
            for relative in paths:
                if not relative:
                    continue
                stage_paths.add(relative)
                value = self._deliverable(
                    run_dir,
                    relative,
                    "final" if stage_id == final_stage else "intermediate",
                    stage_id,
                )
                if value:
                    deliverables.append(value)
        for relative, kind in (
            ("findings.jsonl", "aggregate"),
            ("state.json", "control"),
            ("events.jsonl", "control"),
            ("manifest.json", "control"),
            ("workflow.json", "control"),
            ("shared/source-fingerprint.json", "evidence"),
            ("shared/snapshot-manifest.json", "evidence"),
        ):
            if relative in stage_paths:
                continue
            value = self._deliverable(run_dir, relative, kind, None)
            if value:
                deliverables.append(value)
        return deliverables

    @staticmethod
    def _deliverable(
        run_dir: Path, relative: str, kind: str, stage: str | None
    ) -> dict[str, Any] | None:
        path = (run_dir / relative).resolve()
        try:
            path.relative_to(run_dir)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return {
            "path": str(path.relative_to(run_dir)),
            "kind": kind,
            "stage": stage,
            "bytes": path.stat().st_size,
            "content": content,
        }
