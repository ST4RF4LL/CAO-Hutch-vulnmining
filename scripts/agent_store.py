"""Load and validate versioned Hutch Agent role stores."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from agent_cells import AgentCellError, discover_skills
from hutch_paths import (
    config_relative,
    default_agents_store_source,
    expand_config_path,
    hutch_agents_store,
)


NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ALLOWED_MCP_FIELDS = {"type", "command", "args"}
UNKNOWN_LICENSE = "NOASSERTION"


class AgentStoreError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AgentStoreError(f"invalid Agent Store JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise AgentStoreError(f"Agent Store JSON must be an object: {path}")
    return value


def _child(root: Path, value: str, label: str) -> Path:
    path = (root / value).resolve()
    if path != root and root not in path.parents:
        raise AgentStoreError(f"{label} escapes Agent Store role directory: {value}")
    return path


def resolve_agent_store_path(value: str | Path) -> Path:
    raw = Path(str(value))
    parts = raw.parts
    if not raw.is_absolute() and parts and parts[0] == "agents_store":
        runtime_root = hutch_agents_store()
        runtime = (runtime_root / Path(*parts[1:])).resolve()
        if os.environ.get("HUTCH_AGENTS_STORE") or runtime_root.exists():
            return runtime
        return (default_agents_store_source() / Path(*parts[1:])).resolve()
    return expand_config_path(value)


def _skill_license(skill_file: Path) -> str:
    text = skill_file.read_text(encoding="utf-8")
    match = re.search(r"(?m)^license:\s*['\"]?([^'\"\n]+)['\"]?\s*$", text)
    if not match:
        return UNKNOWN_LICENSE
    return match.group(1).strip()


def _validate_mcp(path: Path) -> dict[str, dict[str, Any]]:
    value = _json(path)
    if value.get("schema") != "hutch.agent-mcp.v1":
        raise AgentStoreError(f"unsupported Agent MCP schema: {path}")
    servers = value.get("servers")
    if not isinstance(servers, dict):
        raise AgentStoreError(f"Agent MCP servers must be an object: {path}")
    validated: dict[str, dict[str, Any]] = {}
    for name, raw in servers.items():
        if not NAME_RE.fullmatch(str(name)) or not isinstance(raw, dict):
            raise AgentStoreError(f"invalid MCP server entry {name!r} in {path}")
        unsupported = set(raw) - ALLOWED_MCP_FIELDS
        if unsupported:
            raise AgentStoreError(
                f"MCP server {name!r} contains forbidden fields: {sorted(unsupported)}"
            )
        if raw.get("type") != "stdio":
            raise AgentStoreError(f"MCP server {name!r} must use local stdio")
        command = raw.get("command")
        args = raw.get("args", [])
        if (
            not isinstance(command, str)
            or not command
            or Path(command).is_absolute()
            or not isinstance(args, list)
            or any(not isinstance(item, str) for item in args)
        ):
            raise AgentStoreError(f"invalid portable MCP command for {name!r}")
        validated[str(name)] = {
            "type": "stdio",
            "command": command,
            "args": list(args),
        }
    return validated


def load_agent_store(value: str | Path, expected_id: str | None = None) -> dict[str, Any]:
    root = resolve_agent_store_path(value)
    if not root.is_dir():
        raise AgentStoreError(f"Agent Store role directory is absent: {root}")
    manifest_path = root / "manifest.json"
    manifest = _json(manifest_path)
    if manifest.get("schema") != "hutch.agent-store.v1":
        raise AgentStoreError(f"unsupported Agent Store schema: {manifest_path}")
    role_id = str(manifest.get("id", ""))
    if not NAME_RE.fullmatch(role_id):
        raise AgentStoreError(f"invalid Agent Store role id: {role_id!r}")
    if expected_id is not None and role_id != expected_id:
        raise AgentStoreError(
            f"Agent Store role mismatch: expected {expected_id!r}, found {role_id!r}"
        )
    description = manifest.get("description")
    if not isinstance(description, str) or not description.strip():
        raise AgentStoreError(f"Agent Store role has no description: {manifest_path}")

    instructions = _child(
        root, str(manifest.get("instructions", "AGENTS.md")), "instructions"
    )
    if not instructions.is_file() or not instructions.read_text(encoding="utf-8").strip():
        raise AgentStoreError(f"Agent Store instructions are absent or empty: {instructions}")
    mcp_path = _child(root, str(manifest.get("mcp", "mcp.json")), "MCP config")
    mcp_servers = _validate_mcp(mcp_path)

    declared = manifest.get("skills")
    if not isinstance(declared, list) or any(
        not isinstance(name, str) or not NAME_RE.fullmatch(name) for name in declared
    ):
        raise AgentStoreError(f"Agent Store skills must be valid names: {manifest_path}")
    if len(declared) != len(set(declared)):
        raise AgentStoreError(f"Agent Store contains duplicate Skills: {manifest_path}")
    skills_dir = root / "skills"
    try:
        catalog = discover_skills([skills_dir]) if declared else {}
    except AgentCellError as error:
        raise AgentStoreError(str(error)) from error
    if set(catalog) != set(declared):
        raise AgentStoreError(
            f"Agent Store Skill inventory mismatch for {role_id}: "
            f"declared={sorted(declared)}, copied={sorted(catalog)}"
        )
    provenance = manifest.get("provenance") or {}
    expected_license = str(provenance.get("skills_license", ""))
    skill_licenses = provenance.get("skill_licenses", {})
    if not expected_license and not isinstance(skill_licenses, dict):
        raise AgentStoreError(f"Agent Store has no Skill license evidence: {manifest_path}")
    for skill_name, source in catalog.items():
        expected = str(skill_licenses.get(skill_name, expected_license))
        if not expected:
            raise AgentStoreError(
                f"Agent Store has no Skill license evidence for {skill_name}: {manifest_path}"
            )
        if _skill_license(source / "SKILL.md") != expected:
            raise AgentStoreError(
                f"Skill license does not match manifest for {source / 'SKILL.md'}"
            )

    return {
        "agent_store": config_relative(root),
        "description": description.strip(),
        "instructions_file": config_relative(instructions),
        "mcp_servers": mcp_servers,
        "skills": list(declared),
        "skill_sources": {
            name: config_relative(catalog[name]) for name in declared
        },
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_provenance_lock(store_root: Path) -> Path:
    store_root = store_root.resolve()
    roles: dict[str, Any] = {}
    for role_root in sorted(
        path for path in store_root.iterdir() if (path / "manifest.json").is_file()
    ):
        manifest = _json(role_root / "manifest.json")
        role_id = str(manifest["id"])
        materialized = load_agent_store(role_root, expected_id=role_id)
        provenance = manifest.get("provenance") or {}
        source_roots: list[Path] = []
        if provenance.get("skills_sources"):
            sources = provenance["skills_sources"]
            if not isinstance(sources, list) or not sources:
                raise AgentStoreError(f"invalid skills_sources for {role_id}")
            source_roots = [expand_config_path(str(source)) for source in sources]
        elif provenance.get("skills_source"):
            source_roots = [expand_config_path(str(provenance["skills_source"]))]
        else:
            raise AgentStoreError(f"missing skills provenance source for {role_id}")
        try:
            source_catalog = discover_skills(source_roots)
        except AgentCellError as error:
            raise AgentStoreError(str(error)) from error
        skills: dict[str, Any] = {}
        for name, copied_value in materialized["skill_sources"].items():
            source = source_catalog.get(name)
            if source is None:
                raise AgentStoreError(
                    f"provenance source is missing Skill {name!r} for {role_id}"
                )
            copied = expand_config_path(copied_value)
            source_hash = tree_sha256(source)
            copied_hash = tree_sha256(copied)
            if source_hash != copied_hash:
                raise AgentStoreError(
                    f"copied Skill differs from provenance source: {role_id}/{name}"
                )
            skills[name] = {
                "source": os.path.relpath(source, Path(__file__).resolve().parents[1]),
                "license": _skill_license(copied / "SKILL.md"),
                "sha256": copied_hash,
            }
        roles[role_id] = {
            "manifest_sha256": sha256(role_root / "manifest.json"),
            "instructions_sha256": sha256(
                expand_config_path(materialized["instructions_file"])
            ),
            "mcp_sha256": sha256(role_root / str(manifest.get("mcp", "mcp.json"))),
            "skills": skills,
        }
    output = store_root / "provenance-lock.json"
    value = {
        "schema": "hutch.agent-store-lock.v1",
        "roles": roles,
    }
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "lock"))
    parser.add_argument("store", type=Path, nargs="?", default=Path("agents_store"))
    args = parser.parse_args()
    try:
        roots = sorted(
            path
            for path in args.store.resolve().iterdir()
            if (path / "manifest.json").is_file()
        )
        for root in roots:
            load_agent_store(root)
        result = (
            write_provenance_lock(args.store)
            if args.command == "lock"
            else args.store.resolve()
        )
        print(json.dumps({"ok": True, "roles": len(roots), "path": str(result)}))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
