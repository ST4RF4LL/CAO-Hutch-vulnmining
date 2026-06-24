#!/usr/bin/env python3
"""Instantiate generic Hutch workflow templates for a concrete Git checkout."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

from adaptive_audit import atomic_json
from agent_cells import AgentCellError, discover_skills
from hutch_paths import (
    config_relative,
    default_cao_repo,
    default_skill_roots,
    expand_config_path,
    expand_config_paths,
    hutch_workflows_dir,
    repo_relative,
)


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "template" / "flows"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SUPPORTED_PROVIDERS = ("codex", "opencode_cli")


class TemplateError(RuntimeError):
    pass


def safe_name(value: str, maximum: int = 36) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower()
    if not name:
        raise TemplateError("workflow name is empty after normalization")
    return name[:maximum].rstrip("-")


def template_path(template: str) -> Path:
    path = Path(template)
    if path.is_file():
        return path.resolve()
    candidate = TEMPLATE_ROOT / f"{template}.json"
    if candidate.is_file():
        return candidate.resolve()
    raise TemplateError(f"template not found: {template}")


def load_template(template: str) -> tuple[Path, dict[str, Any]]:
    path = template_path(template)
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "hutch.cao-workflow-template.v1":
        raise TemplateError(f"unsupported template schema: {path}")
    if not NAME_RE.fullmatch(str(value.get("id", ""))):
        raise TemplateError(f"invalid template id: {path}")
    workflow = value.get("workflow")
    if not isinstance(workflow, dict):
        raise TemplateError(f"template has no workflow object: {path}")
    return path, value


def list_templates() -> list[dict[str, Any]]:
    if not TEMPLATE_ROOT.is_dir():
        return []
    values = []
    for path in sorted(TEMPLATE_ROOT.glob("*.json")):
        _, template = load_template(str(path))
        workflow = template["workflow"]
        values.append(
            {
                "id": template["id"],
                "path": config_relative(path),
                "description": template.get("description") or workflow.get("description"),
                "stages": len(workflow.get("stages", [])),
                "agents": len(workflow.get("agents", [])),
            }
        )
    return values


def requested_skills(workflow: dict[str, Any]) -> set[str]:
    return {
        str(skill)
        for agent in workflow.get("agents", [])
        for skill in agent.get("skills", [])
    }


def template_skill_roots(source: dict[str, Any]) -> list[Path]:
    """Return optional template-specific skill roots that exist on this host."""
    return [
        path
        for path in expand_config_paths(source.get("skill_roots", []))
        if path.is_dir()
    ]


def normalize_template_skills(
    workflow: dict[str, Any], skill_roots: list[Path], *, strict: bool
) -> dict[str, list[str]]:
    requested = requested_skills(workflow)
    if not requested:
        return {}
    available = set(discover_skills(skill_roots)) if skill_roots else set()
    missing = sorted(requested - available)
    if strict and missing:
        raise TemplateError(f"template skills not found in skill_roots: {missing}")
    removed: dict[str, list[str]] = {}
    if missing:
        missing_set = set(missing)
        for agent in workflow.get("agents", []):
            before = list(agent.get("skills", []))
            after = [skill for skill in before if skill not in missing_set]
            if after != before:
                agent["skills"] = after
                removed[str(agent["id"])] = sorted(set(before) - set(after))
    return removed


def instantiate_template(
    template: str,
    target: Path,
    *,
    name: str | None = None,
    cao_repo: Path | None = None,
    skill_roots: list[Path] | None = None,
    strict_skills: bool = False,
    provider: str | None = None,
) -> tuple[dict[str, Any], dict[str, list[str]], Path]:
    path, source = load_template(template)
    target = expand_config_path(target)
    if not target.is_dir():
        raise TemplateError(f"target is not a directory: {target}")
    if not (target / ".git").exists():
        raise TemplateError(f"target is not a Git checkout: {target}")
    resolved_cao = cao_repo.expanduser().resolve() if cao_repo else default_cao_repo()
    configured_roots = (
        [*default_skill_roots(), *template_skill_roots(source)]
        if skill_roots is None
        else skill_roots
    )
    roots = [root.expanduser().resolve() for root in configured_roots]
    suffix = str(source.get("name_suffix") or source["id"])
    workflow_name = safe_name(name or f"{target.name}-{suffix}")

    workflow = copy.deepcopy(source["workflow"])
    if provider is not None:
        if provider not in SUPPORTED_PROVIDERS:
            raise TemplateError(f"unsupported provider: {provider}")
        workflow["provider"] = provider
    workflow.update(
        {
            "schema": "hutch.cao-workflow.v1",
            "name": workflow_name,
            "version": str(source.get("version") or workflow.get("version") or "1.0.0"),
            "target": config_relative(target),
            "cao_repo": config_relative(resolved_cao),
            "skill_roots": [config_relative(root) for root in roots],
            "template": {
                "schema": source["schema"],
                "id": source["id"],
                "source": config_relative(path),
            },
        }
    )
    removed = normalize_template_skills(workflow, roots, strict=strict_skills)
    return workflow, removed, path


def default_output_for(workflow: dict[str, Any]) -> Path:
    return hutch_workflows_dir() / f"{workflow['name']}.generated.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list built-in workflow templates")

    render = commands.add_parser("render", help="render one template for a Git checkout")
    render.add_argument("target", type=Path)
    render.add_argument("--template", default="one-run")
    render.add_argument("--name")
    render.add_argument("--output", type=Path)
    render.add_argument("--cao-repo", type=Path)
    render.add_argument("--skill-root", type=Path, action="append", default=[])
    render.add_argument("--strict-skills", action="store_true")
    render.add_argument("--provider", choices=SUPPORTED_PROVIDERS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "list":
            print(json.dumps({"ok": True, "templates": list_templates()}, ensure_ascii=False))
            return 0
        workflow, removed, source = instantiate_template(
            args.template,
            args.target,
            name=args.name,
            cao_repo=args.cao_repo,
            skill_roots=args.skill_root or None,
            strict_skills=args.strict_skills,
            provider=args.provider,
        )
        output = (args.output or default_output_for(workflow)).expanduser().resolve()
        atomic_json(output, workflow)
        print(
            json.dumps(
                {
                    "ok": True,
                    "template": args.template,
                    "template_path": repo_relative(source),
                    "workflow": repo_relative(output),
                    "name": workflow["name"],
                    "target": workflow["target"],
                    "removed_optional_skills": removed,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (
        TemplateError,
        AgentCellError,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        print(f"template error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
