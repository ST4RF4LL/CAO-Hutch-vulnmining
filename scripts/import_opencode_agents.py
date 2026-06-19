#!/usr/bin/env python3
"""Convert project-local OpenCode agents into installable CAO profiles."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def cao_mcp(cao_repo: Path) -> dict:
    """Use the checked-out CAO source instead of a network-dependent uvx install."""
    return {
        "cao-mcp-server": {
            "type": "stdio",
            "command": "uv",
            "args": ["--directory", str(cao_repo.resolve()), "run", "cao-mcp-server"],
        }
    }


def read_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"missing YAML frontmatter: {path}")
    _, raw_metadata, body = text.split("---", 2)
    metadata = yaml.safe_load(raw_metadata) or {}
    return metadata, body.lstrip("\n")


def mapped_tools(metadata: dict, is_supervisor: bool) -> list[str]:
    permission = metadata.get("permission") or {}
    tools = ["fs_read", "fs_list"]
    if permission.get("edit") not in (None, "deny"):
        tools.append("fs_write")
    if permission.get("bash") not in (None, "deny"):
        tools.append("execute_bash")
    if is_supervisor:
        tools.append("@cao-mcp-server")
    return tools


def render_profile(source: Path, cao_repo: Path) -> str:
    metadata, body = read_frontmatter(source)
    name = source.stem
    is_supervisor = metadata.get("mode") == "primary"
    profile = {
        "name": name,
        "description": metadata.get("description", f"Imported OpenCode agent {name}"),
        "provider": "opencode_cli",
        "role": "supervisor" if is_supervisor else "reviewer",
        "allowedTools": mapped_tools(metadata, is_supervisor),
    }
    if is_supervisor:
        profile["mcpServers"] = cao_mcp(cao_repo)
        body = (
            "# CAO Adapter Rules\n\n"
            "- This profile is running under CAO. Do not use OpenCode's native `task` tool.\n"
            "- Delegate with CAO MCP `assign` or `handoff` to the worker profile names "
            "listed in this prompt.\n"
            "- Treat the target repository as read-only. Write only under the validation "
            "workspace's `reports/` and `tmp/` directories.\n"
            "- CAO owns runtime/session orchestration; durable workflow state remains an "
            "external Rabbit Hutch responsibility.\n\n"
            + body
        )
    else:
        body = (
            "# CAO Adapter Rules\n\n"
            "- Treat the assigned target repository as read-only.\n"
            "- Write generated artifacts only to the task-designated `reports/` or `tmp/` paths.\n"
            "- Return a concise completion summary to the assigning CAO supervisor.\n\n"
            + body
        )
    return f"---\n{yaml.safe_dump(profile, sort_keys=False).rstrip()}\n---\n\n{body.rstrip()}\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="opencode_multi_agents repository")
    parser.add_argument("output", type=Path, help="directory for generated CAO profiles")
    parser.add_argument("--cao-repo", type=Path, required=True, help="local CAO checkout")
    args = parser.parse_args()

    agents_dir = args.source.resolve() / ".opencode" / "agents"
    if not agents_dir.is_dir():
        raise SystemExit(f"OpenCode agents directory not found: {agents_dir}")

    args.output.mkdir(parents=True, exist_ok=True)
    generated = []
    for source in sorted(agents_dir.glob("*.md")):
        destination = args.output / source.name
        destination.write_text(render_profile(source, args.cao_repo), encoding="utf-8")
        generated.append(destination)

    print(f"generated {len(generated)} CAO profiles in {args.output.resolve()}")
    for destination in generated:
        print(destination.name)


if __name__ == "__main__":
    main()
