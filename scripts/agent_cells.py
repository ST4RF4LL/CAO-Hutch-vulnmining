"""Build durable, provider-local Agent Cells for Hutch runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from hutch_paths import expand_config_path, hutch_runs_dir


CELL_LINKS = ("artifacts", "inbox", "outbox", "shared", "tmp")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OPENCODE_AGENTS_DIR = Path.home() / ".aws" / "opencode" / "agents"
HUTCH_RUNS_GLOB = f"{hutch_runs_dir().resolve()}/**"
SUPPORTED_PROVIDERS = {"codex", "opencode_cli"}


class AgentCellError(RuntimeError):
    pass


def _skill_name(skill_file: Path) -> str:
    text = skill_file.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise AgentCellError(f"skill has no frontmatter: {skill_file}")
    try:
        frontmatter = text.split("---", 2)[1]
    except IndexError as error:
        raise AgentCellError(f"skill has incomplete frontmatter: {skill_file}") from error
    match = re.search(r"(?m)^name:\s*['\"]?([^'\"\s]+)['\"]?\s*$", frontmatter)
    if not match:
        raise AgentCellError(f"skill frontmatter has no name: {skill_file}")
    name = match.group(1)
    if not NAME_RE.fullmatch(name):
        raise AgentCellError(f"invalid skill name {name!r} in {skill_file}")
    return name


def runtime_skill_name(cell_id: str, source_name: str) -> str:
    """Create a stable provider-local skill alias that cannot collide globally."""
    normalized = re.sub(r"[^a-z0-9]+", "-", f"{cell_id}-{source_name}".lower()).strip("-")
    if len(normalized) <= 64:
        return normalized
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:8]
    return f"{normalized[:55].rstrip('-')}-{digest}"


def skill_permission_rules(
    cell_skills: Iterable[tuple[str, Iterable[str]]],
) -> dict[str, str]:
    rules = {"*": "deny"}
    for cell_id, skills in cell_skills:
        rules.update({runtime_skill_name(cell_id, skill): "allow" for skill in skills})
    return rules


def _rewrite_skill_name(skill_file: Path, source_name: str, runtime_name: str) -> None:
    text = skill_file.read_text(encoding="utf-8")
    updated, count = re.subn(
        rf"(?m)^(name:\s*)['\"]?{re.escape(source_name)}['\"]?\s*$",
        rf"\g<1>{runtime_name}",
        text,
        count=1,
    )
    if count != 1:
        raise AgentCellError(f"could not rewrite skill name in {skill_file}")
    skill_file.write_text(updated, encoding="utf-8")


def _parse_profile_source(profile_source: Path) -> tuple[str, list[str], str]:
    text = profile_source.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise AgentCellError(f"CAO profile has no frontmatter: {profile_source}")
    try:
        frontmatter, body = text.split("---", 2)[1:]
    except ValueError as error:
        raise AgentCellError(f"CAO profile has incomplete frontmatter: {profile_source}") from error
    description_match = re.search(r"(?m)^description:\s*(.+?)\s*$", frontmatter)
    description = (
        description_match.group(1).strip("'\"")
        if description_match
        else f"Hutch Agent Cell compiled from {profile_source.name}"
    )
    allowed_tools: list[str] = []
    in_allowed_tools = False
    for line in frontmatter.splitlines():
        if line == "allowedTools:":
            in_allowed_tools = True
            continue
        if in_allowed_tools and line.startswith("  - "):
            allowed_tools.append(line[4:].strip().strip("'\""))
            continue
        if in_allowed_tools and line and not line.startswith(" "):
            break
    return description, allowed_tools, body.lstrip("\n")


def _opencode_permissions(
    allowed_tools: list[str],
    skill_rules: dict[str, str],
    extra_read_roots: Iterable[str | Path] = (),
) -> dict[str, Any]:
    if "*" in allowed_tools:
        permissions: dict[str, Any] = {
            name: "allow"
            for name in (
                "bash",
                "codesearch",
                "edit",
                "glob",
                "grep",
                "question",
                "read",
                "task",
                "todowrite",
                "webfetch",
                "websearch",
                "write",
            )
        }
    else:
        enabled: set[str] = set()
        mapping = {
            "execute_bash": {"bash"},
            "fs_read": {"read"},
            "fs_write": {"edit", "write"},
            "fs_list": {"glob", "grep"},
            "fs_*": {"read", "edit", "write", "glob", "grep"},
            "@builtin": {"bash", "read", "edit", "write", "glob", "grep"},
        }
        for tool in allowed_tools:
            enabled.update(mapping.get(tool, set()))
        permissions = {
            name: ("allow" if name in enabled else "deny")
            for name in ("bash", "edit", "glob", "grep", "read", "write")
        }
        permissions.update(
            {
                "codesearch": "deny",
                "question": "deny",
                "task": "deny",
                "todowrite": "allow",
                "webfetch": "deny",
                "websearch": "deny",
            }
        )
    permissions["skill"] = skill_rules
    # CAO loads its managed agent definition after project-local OpenCode
    # config. Keep the durable Hutch run tree usable even when that later
    # profile layer wins over a Cell's exact per-run external-directory rule.
    external_directory = {
        "*": "deny",
        HUTCH_RUNS_GLOB: "allow",
    }
    for root in extra_read_roots:
        root_path = expand_config_path(root)
        external_directory[f"{root_path.resolve()}/*"] = "allow"
    permissions["external_directory"] = external_directory
    # Agent Cells carry their own copied skills. Prevent a worker from escaping
    # that boundary through the globally configured skill-loader MCP.
    permissions["agent-skill-loader_*"] = "deny"
    return permissions


def write_opencode_agent(
    agent_path: Path,
    profile: str,
    profile_source: Path,
    skill_rules: dict[str, str],
    extra_read_roots: Iterable[str | Path] = (),
) -> Path:
    description, allowed_tools, body = _parse_profile_source(profile_source)
    permissions = _opencode_permissions(allowed_tools, skill_rules, extra_read_roots)
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_text(
        "---\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "mode: all\n"
        f"permission: {json.dumps(permissions, ensure_ascii=False)}\n"
        "---\n\n"
        + body.rstrip()
        + "\n",
        encoding="utf-8",
    )
    return agent_path


def install_opencode_agent_policy(
    profile_source: Path,
    profile: str,
    cell_skills: Iterable[tuple[str, Iterable[str]]],
    agents_dir: Path = OPENCODE_AGENTS_DIR,
    extra_read_roots: Iterable[str | Path] = (),
) -> Path:
    """Replace a CAO-installed OpenCode agent with Hutch's compiled skill policy."""
    return write_opencode_agent(
        agents_dir / f"{profile}.md",
        profile,
        profile_source.resolve(),
        skill_permission_rules(cell_skills),
        extra_read_roots,
    )


def discover_skills(skill_roots: Iterable[str | Path]) -> dict[str, Path]:
    """Index skill folders by frontmatter name, rejecting ambiguous definitions."""
    discovered: dict[str, Path] = {}
    for raw_root in skill_roots:
        root = Path(raw_root).expanduser().resolve()
        if not root.is_dir():
            raise AgentCellError(f"skill root is not a directory: {root}")
        for skill_file in sorted(root.rglob("SKILL.md")):
            name = _skill_name(skill_file)
            source = skill_file.parent.resolve()
            prior = discovered.get(name)
            if prior is not None and prior != source:
                raise AgentCellError(
                    f"duplicate skill {name!r}: {prior / 'SKILL.md'} and {skill_file}"
                )
            discovered[name] = source
    return discovered


def resolve_cell_skill_sources(
    workflow: dict[str, Any], specs: Iterable[dict[str, Any]]
) -> dict[str, dict[str, Path]]:
    """Resolve role-local Skill copies before falling back to shared roots."""
    specs = list(specs)
    globally_requested = {
        skill
        for spec in specs
        for skill in spec.get("skills", [])
        if skill not in (spec.get("skill_sources") or {})
    }
    global_catalog = (
        discover_skills(workflow.get("skill_roots", []))
        if globally_requested
        else {}
    )
    resolved: dict[str, dict[str, Path]] = {}
    missing: set[str] = set()
    for spec in specs:
        cell_id = str(spec["id"])
        skills = list(spec.get("skills", []))
        explicit = spec.get("skill_sources") or {}
        if not isinstance(explicit, dict):
            raise AgentCellError(f"Agent Cell {cell_id} skill_sources must be an object")
        undeclared = set(explicit) - set(skills)
        if undeclared:
            raise AgentCellError(
                f"Agent Cell {cell_id} has sources for undeclared Skills: "
                f"{sorted(undeclared)}"
            )
        catalog: dict[str, Path] = {}
        for skill in skills:
            if skill not in explicit:
                source = global_catalog.get(skill)
                if source is None:
                    missing.add(skill)
                    continue
                catalog[skill] = source
                continue
            source = expand_config_path(str(explicit[skill]))
            skill_file = source / "SKILL.md"
            if not source.is_dir() or not skill_file.is_file():
                raise AgentCellError(
                    f"Agent Cell {cell_id} Skill source is absent: {source}"
                )
            if _skill_name(skill_file) != skill:
                raise AgentCellError(
                    f"Agent Cell {cell_id} Skill source name mismatch: {skill_file}"
                )
            catalog[skill] = source
        resolved[cell_id] = catalog
    if missing:
        raise AgentCellError(f"skills not found in skill_roots: {sorted(missing)}")
    return resolved


def validate_cell_specs(workflow: dict[str, Any], specs: Iterable[dict[str, Any]]) -> None:
    specs = list(specs)
    seen: set[str] = set()
    for spec in specs:
        cell_id = str(spec["id"])
        if not NAME_RE.fullmatch(cell_id):
            raise AgentCellError(f"invalid Agent Cell id: {cell_id!r}")
        if cell_id in seen:
            raise AgentCellError(f"duplicate Agent Cell id: {cell_id}")
        seen.add(cell_id)
        skills = list(spec.get("skills", []))
        if len(skills) != len(set(skills)):
            raise AgentCellError(f"Agent Cell {cell_id} declares duplicate skills")
    resolve_cell_skill_sources(workflow, specs)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _ensure_run_links(workspace: Path, run_dir: Path) -> None:
    for name in CELL_LINKS:
        link = workspace / name
        target = run_dir / name
        relative_target = os.path.relpath(target, workspace)
        if link.is_symlink():
            if Path(os.path.realpath(link)) != target.resolve():
                raise AgentCellError(f"Agent Cell link points at the wrong target: {link}")
            continue
        if link.exists():
            raise AgentCellError(f"Agent Cell link path is occupied: {link}")
        link.symlink_to(relative_target, target_is_directory=True)


def _external_directory_rules(workflow: dict[str, Any], run_dir: Path) -> dict[str, str]:
    rules = {
        "*": "deny",
        f"{run_dir.resolve()}/*": "allow",
    }
    target = workflow.get("target")
    if target:
        target_path = expand_config_path(str(target))
        rules[f"{target_path.resolve()}/*"] = "allow"
    return rules


def prepare_agent_cells(
    workflow: dict[str, Any], run_dir: Path, specs: Iterable[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Create idempotent Cell workspaces and return their durable manifests."""
    specs = list(specs)
    validate_cell_specs(workflow, specs)
    provider = str(workflow.get("provider", "opencode_cli"))
    if provider not in SUPPORTED_PROVIDERS:
        raise AgentCellError(f"unsupported Agent Cell provider: {provider}")
    catalogs = resolve_cell_skill_sources(workflow, specs)
    external_directory = _external_directory_rules(workflow, run_dir)
    cells: dict[str, dict[str, Any]] = {}
    for spec in specs:
        cell_id = str(spec["id"])
        profile = str(spec["profile"])
        skills = list(spec.get("skills", []))
        cell_dir = run_dir / "agents" / cell_id
        workspace = cell_dir / "workspace"
        opencode_dir = workspace / ".opencode"
        agents_dir = opencode_dir / "agents"
        codex_dir = workspace / ".agents"
        skills_dir = (
            opencode_dir / "skills"
            if provider == "opencode_cli"
            else codex_dir / "skills"
        )
        workspace.mkdir(parents=True, exist_ok=True)
        _ensure_run_links(workspace, run_dir)
        if skills_dir.exists():
            shutil.rmtree(skills_dir)
        skills_dir.mkdir(parents=True)
        if provider == "opencode_cli":
            if agents_dir.exists():
                shutil.rmtree(agents_dir)
            agents_dir.mkdir(parents=True)
        skill_sources: dict[str, str] = {}
        runtime_skills: dict[str, str] = {}
        for skill in skills:
            source = catalogs[cell_id][skill]
            runtime_name = runtime_skill_name(cell_id, skill)
            destination = skills_dir / runtime_name
            shutil.copytree(source, destination)
            _rewrite_skill_name(destination / "SKILL.md", skill, runtime_name)
            skill_sources[skill] = str(source)
            runtime_skills[skill] = runtime_name

        rules = skill_permission_rules([(cell_id, skills)])
        profile_source_value = spec.get("profile_source")
        local_agent: Path | None = None
        if provider == "opencode_cli" and profile_source_value:
            profile_source = Path(profile_source_value).expanduser().resolve()
            if not profile_source.is_file():
                raise AgentCellError(f"CAO profile source is absent: {profile_source}")
            local_agent = write_opencode_agent(
                opencode_dir / "agents" / f"{profile}.md",
                profile,
                profile_source,
                rules,
                [workflow["target"]] if workflow.get("target") else (),
            )
        config_path: Path | None = None
        if provider == "opencode_cli":
            config_path = opencode_dir / "opencode.json"
            _write_json(
                config_path,
                {
                    "$schema": "https://opencode.ai/config.json",
                    "agent": {
                        profile: {
                            "permission": {
                                "skill": rules,
                                "external_directory": external_directory,
                                "agent-skill-loader_*": "deny",
                            }
                        }
                    },
                },
            )
        manifest = {
            "schema": "hutch.agent-cell.v1",
            "id": cell_id,
            "profile": profile,
            "provider": provider,
            "cell_dir": str(cell_dir.resolve()),
            "workspace": str(workspace.resolve()),
            "skills_dir": str(skills_dir.resolve()),
            "opencode_config": str(config_path.resolve()) if config_path else None,
            "opencode_agent": str(local_agent.resolve()) if local_agent else None,
            "skills": skills,
            "runtime_skills": runtime_skills,
            "skill_sources": skill_sources,
            "run_links": {name: str((run_dir / name).resolve()) for name in CELL_LINKS},
        }
        _write_json(cell_dir / "cell.json", manifest)
        cells[cell_id] = manifest
    return cells
