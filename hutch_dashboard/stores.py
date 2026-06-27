"""Read lightweight Hutch Agent Store and Flow Store summaries."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from scripts.hutch_paths import (
    default_agents_store_source,
    default_flows_store_source,
    hutch_agents_store,
    hutch_flows_store,
)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _active_store(runtime: Path, source: Path, env_name: str) -> Path:
    if os.environ.get(env_name) or runtime.exists():
        return runtime
    return source


def active_agents_store() -> Path:
    return _active_store(
        hutch_agents_store(),
        default_agents_store_source(),
        "HUTCH_AGENTS_STORE",
    )


def active_flows_store() -> Path:
    return _active_store(
        hutch_flows_store(),
        default_flows_store_source(),
        "HUTCH_FLOWS_STORE",
    )


def _portable_path(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _mcp_servers(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        value = _json(path)
    except (OSError, json.JSONDecodeError):
        return []
    servers = value.get("servers")
    if not isinstance(servers, dict):
        return []
    records: list[dict[str, Any]] = []
    for name, raw in sorted(servers.items()):
        if not isinstance(raw, dict):
            continue
        command = str(raw.get("command") or "")
        args = _string_list(raw.get("args"))
        summary = " ".join(shlex.quote(part) for part in [command, *args] if part)
        records.append(
            {
                "name": str(name),
                "type": str(raw.get("type") or ""),
                "command": summary,
            }
        )
    return records


def list_agent_store() -> dict[str, Any]:
    root = active_agents_store()
    agents: list[dict[str, Any]] = []
    if root.is_dir():
        for manifest_path in sorted(root.glob("*/manifest.json")):
            try:
                manifest = _json(manifest_path)
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("schema") != "hutch.agent-store.v1":
                continue
            role_dir = manifest_path.parent
            role_id = str(manifest.get("id") or role_dir.name)
            skills = _string_list(manifest.get("skills"))
            mcp_path = role_dir / str(manifest.get("mcp") or "mcp.json")
            mcp = _mcp_servers(mcp_path)
            agents.append(
                {
                    "id": role_id,
                    "description": str(manifest.get("description") or ""),
                    "path": _portable_path(role_dir),
                    "instructions": str(manifest.get("instructions") or "AGENTS.md"),
                    "skills": skills,
                    "skill_count": len(skills),
                    "mcp_servers": mcp,
                    "mcp_count": len(mcp),
                }
            )
    return {
        "path": _portable_path(root),
        "exists": root.is_dir(),
        "agents": agents,
        "count": len(agents),
    }


def list_flow_store() -> dict[str, Any]:
    root = active_flows_store()
    flows: list[dict[str, Any]] = []
    if root.is_dir():
        for flow_path in sorted(root.glob("*/flow.json")):
            try:
                template = _json(flow_path)
            except (OSError, json.JSONDecodeError):
                continue
            if template.get("schema") != "hutch.cao-workflow-template.v1":
                continue
            workflow = template.get("workflow")
            if not isinstance(workflow, dict):
                workflow = {}
            execution = workflow.get("execution")
            if not isinstance(execution, dict):
                execution = {}
            agents = [
                str(agent.get("id") or agent.get("store") or "")
                for agent in workflow.get("agents", [])
                if isinstance(agent, dict)
            ]
            agents = [agent for agent in agents if agent]
            stages = [
                str(stage.get("id") or "")
                for stage in workflow.get("stages", [])
                if isinstance(stage, dict)
            ]
            stages = [stage for stage in stages if stage]
            flows.append(
                {
                    "id": str(template.get("id") or flow_path.parent.name),
                    "version": str(template.get("version") or ""),
                    "description": str(
                        template.get("description") or workflow.get("description") or ""
                    ),
                    "path": _portable_path(flow_path.parent),
                    "provider": str(workflow.get("provider") or ""),
                    "schedule": str(workflow.get("schedule") or ""),
                    "name_suffix": str(template.get("name_suffix") or ""),
                    "execution": {
                        "max_concurrency": execution.get("max_concurrency"),
                        "stage_timeout_seconds": execution.get("stage_timeout_seconds"),
                        "max_attempts": execution.get("max_attempts"),
                        "no_supervisor": bool(execution.get("no_supervisor")),
                    },
                    "agents": agents,
                    "agent_count": len(agents),
                    "stages": stages,
                    "stage_count": len(stages),
                }
            )
    return {
        "path": _portable_path(root),
        "exists": root.is_dir(),
        "flows": flows,
        "count": len(flows),
    }
