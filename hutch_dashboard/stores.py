"""Read lightweight Hutch Agent Store and Flow Store summaries."""

from __future__ import annotations

import json
import os
import re
import shlex
from http import HTTPStatus
from pathlib import Path
from typing import Any

from scripts.hutch_paths import (
    default_agents_store_source,
    default_flows_store_source,
    hutch_agents_store,
    hutch_flows_store,
)


ROLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AgentStoreEditError(ValueError):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


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


def _role_manifest(root: Path, role_id: str) -> tuple[Path, dict[str, Any]]:
    if not ROLE_ID_RE.fullmatch(role_id):
        raise AgentStoreEditError("invalid agent role id")
    role_dir = (root / role_id).resolve()
    try:
        if not role_dir.is_relative_to(root.resolve()):
            raise AgentStoreEditError("agent role escapes Agent Store")
    except OSError as error:
        raise AgentStoreEditError(f"invalid Agent Store path: {error}") from error
    manifest_path = role_dir / "manifest.json"
    if not manifest_path.is_file():
        raise AgentStoreEditError("agent role not found", HTTPStatus.NOT_FOUND)
    try:
        manifest = _json(manifest_path)
    except (OSError, json.JSONDecodeError) as error:
        raise AgentStoreEditError(f"invalid agent manifest: {error}") from error
    if manifest.get("schema") != "hutch.agent-store.v1":
        raise AgentStoreEditError("unsupported agent manifest schema")
    manifest_id = str(manifest.get("id") or role_dir.name)
    if manifest_id != role_id:
        raise AgentStoreEditError("agent role id does not match manifest")
    return role_dir, manifest


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


def _frontmatter_value(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        items = [
            _frontmatter_value(item)
            for item in value[1:-1].split(",")
            if item.strip()
        ]
        return items
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value


def _skill_header(skill_file: Path) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "path": _portable_path(skill_file),
        "available": False,
        "metadata": {},
    }
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return detail
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return detail
    header: dict[str, Any] = {}
    current_key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            detail["available"] = True
            detail["metadata"] = header
            return detail
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent and current_key and isinstance(header.get(current_key), dict):
            child_key, separator, child_value = line.strip().partition(":")
            if separator and child_key:
                header[current_key][child_key.strip()] = _frontmatter_value(child_value)
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip():
            current_key = None
            continue
        current_key = key.strip()
        header[current_key] = (
            {} if not value.strip() else _frontmatter_value(value)
        )
    return detail


def _agent_skill_details(role_dir: Path, skills: list[str]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for name in skills:
        if not ROLE_ID_RE.fullmatch(name):
            details.append(
                {
                    "name": name,
                    "path": _portable_path(role_dir / "skills" / name / "SKILL.md"),
                    "available": False,
                    "metadata": {},
                }
            )
            continue
        skill_file = (role_dir / "skills" / name / "SKILL.md").resolve()
        role_root = role_dir.resolve()
        if not skill_file.is_relative_to(role_root):
            details.append(
                {
                    "name": name,
                    "path": _portable_path(skill_file),
                    "available": False,
                    "metadata": {},
                }
            )
            continue
        detail = _skill_header(skill_file)
        detail["name"] = name
        details.append(detail)
    return details


def _agent_instructions(role_dir: Path, instructions: str) -> dict[str, Any]:
    try:
        path = (role_dir / instructions).resolve()
        if not path.is_relative_to(role_dir.resolve()):
            return {"path": _portable_path(path), "content": "", "available": False}
        content = path.read_text(encoding="utf-8")
        return {
            "path": _portable_path(path),
            "content": content,
            "available": True,
            "bytes": len(content.encode("utf-8")),
        }
    except (OSError, UnicodeDecodeError):
        return {
            "path": _portable_path(role_dir / instructions),
            "content": "",
            "available": False,
        }


def update_agent_instructions(role_id: str, content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise AgentStoreEditError("AGENTS.md content must be a string")
    if not content.strip():
        raise AgentStoreEditError("AGENTS.md content cannot be empty")
    if len(content.encode("utf-8")) > 64 * 1024:
        raise AgentStoreEditError("AGENTS.md content exceeds 65536 bytes")

    root = active_agents_store().resolve()
    role_dir, manifest = _role_manifest(root, role_id)
    instructions = str(manifest.get("instructions") or "AGENTS.md")
    if instructions != "AGENTS.md":
        raise AgentStoreEditError("only AGENTS.md editing is currently supported")
    path = (role_dir / instructions).resolve()
    if not path.is_relative_to(role_dir.resolve()):
        raise AgentStoreEditError("AGENTS.md path escapes agent role directory")
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as error:
        raise AgentStoreEditError(f"failed to write AGENTS.md: {error}") from error
    return list_agent_store()


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


def _stage_depends_on(stage: dict[str, Any]) -> list[str]:
    dependencies = stage.get("depends_on")
    if dependencies is None:
        dependencies = stage.get("needs")
    return _string_list(dependencies)


def _stage_artifacts(stage: dict[str, Any]) -> list[str]:
    artifacts: list[str] = []
    artifact = stage.get("artifact")
    if isinstance(artifact, str) and artifact:
        artifacts.append(artifact)
    artifacts.extend(_string_list(stage.get("required_artifacts")))
    return list(dict.fromkeys(artifacts))


def _workflow_stages(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        stage for stage in workflow.get("stages", []) if isinstance(stage, dict)
    ]


def _flow_graph(workflow: dict[str, Any]) -> dict[str, Any]:
    raw_stages = _workflow_stages(workflow)
    stage_ids = {
        str(stage.get("id") or "")
        for stage in raw_stages
        if str(stage.get("id") or "")
    }
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for stage in raw_stages:
        stage_id = str(stage.get("id") or "")
        if not stage_id:
            continue
        agent = str(stage.get("agent") or "")
        condition = ""
        domain_condition = stage.get("domain_condition")
        if isinstance(domain_condition, dict):
            condition = str(domain_condition.get("domain") or "")
        nodes.append(
            {
                "id": stage_id,
                "label": agent or stage_id,
                "type": "stage",
                "agent": agent,
                "task_id": str(stage.get("task_id") or ""),
                "artifact": str(stage.get("artifact") or ""),
                "artifacts": _stage_artifacts(stage),
                "inputs": _string_list(stage.get("inputs")),
                "condition": condition,
            }
        )
        for dependency in _stage_depends_on(stage):
            if dependency not in stage_ids:
                continue
            edges.append(
                {
                    "id": f"{dependency}->{stage_id}",
                    "source": dependency,
                    "target": stage_id,
                    "type": "dependency",
                }
            )
    return {"nodes": nodes, "edges": edges}


def _workflow_agent_stage_refs(workflow: dict[str, Any]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    for agent in workflow.get("agents", []):
        if not isinstance(agent, dict):
            continue
        agent_id = str(agent.get("id") or agent.get("store") or "")
        if agent_id:
            refs.setdefault(agent_id, [])
    for stage in _workflow_stages(workflow):
        agent_id = str(stage.get("agent") or "")
        stage_id = str(stage.get("id") or "")
        if agent_id and stage_id:
            refs.setdefault(agent_id, []).append(stage_id)
    return {agent_id: list(dict.fromkeys(stages)) for agent_id, stages in refs.items()}


def _flow_agent_details(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    stage_refs = _workflow_agent_stage_refs(workflow)
    details: dict[str, dict[str, Any]] = {}
    for raw in workflow.get("agents", []):
        if not isinstance(raw, dict):
            continue
        agent_id = str(raw.get("id") or raw.get("store") or "")
        if not agent_id:
            continue
        skills = _string_list(raw.get("skills"))
        details[agent_id] = {
            "id": agent_id,
            "store": str(raw.get("store") or ""),
            "description": str(raw.get("description") or ""),
            "mission": str(raw.get("mission") or ""),
            "provider": str(raw.get("provider") or ""),
            "atlas": bool(raw.get("atlas")),
            "skills": skills,
            "skill_count": len(skills),
            "stages": stage_refs.get(agent_id, []),
            "stage_count": len(stage_refs.get(agent_id, [])),
            "declared": True,
        }
    for agent_id, stages in stage_refs.items():
        details.setdefault(
            agent_id,
            {
                "id": agent_id,
                "store": "",
                "description": "",
                "mission": "",
                "provider": "",
                "atlas": False,
                "skills": [],
                "skill_count": 0,
                "stages": stages,
                "stage_count": len(stages),
                "declared": False,
            },
        )
    return [details[agent_id] for agent_id in sorted(details)]


def _flow_agent_references() -> dict[str, list[dict[str, Any]]]:
    root = active_flows_store()
    references: dict[str, list[dict[str, Any]]] = {}
    if not root.is_dir():
        return references
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
        flow_id = str(template.get("id") or flow_path.parent.name)
        description = str(template.get("description") or workflow.get("description") or "")
        for agent_id, stages in _workflow_agent_stage_refs(workflow).items():
            references.setdefault(agent_id, []).append(
                {
                    "id": flow_id,
                    "description": description,
                    "path": _portable_path(flow_path.parent),
                    "stages": stages,
                    "stage_count": len(stages),
                }
            )
    return references


def list_agent_store() -> dict[str, Any]:
    root = active_agents_store()
    flow_references = _flow_agent_references()
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
            skill_details = _agent_skill_details(role_dir, skills)
            references = flow_references.get(role_id, [])
            instructions = str(manifest.get("instructions") or "AGENTS.md")
            instructions_detail = _agent_instructions(role_dir, instructions)
            mcp_path = role_dir / str(manifest.get("mcp") or "mcp.json")
            mcp = _mcp_servers(mcp_path)
            agents.append(
                {
                    "id": role_id,
                    "description": str(manifest.get("description") or ""),
                    "path": _portable_path(role_dir),
                    "instructions": instructions,
                    "instructions_path": instructions_detail["path"],
                    "instructions_content": instructions_detail["content"],
                    "instructions_available": instructions_detail["available"],
                    "instructions_bytes": instructions_detail.get("bytes", 0),
                    "skills": skills,
                    "skill_details": skill_details,
                    "skill_count": len(skills),
                    "flow_references": references,
                    "flow_count": len(references),
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
            graph = _flow_graph(workflow)
            agent_details = _flow_agent_details(workflow)
            agents = [agent["id"] for agent in agent_details]
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
                    "graph": graph,
                    "agents": agents,
                    "agent_details": agent_details,
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
