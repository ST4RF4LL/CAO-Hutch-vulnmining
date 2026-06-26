"""Portable path helpers for Hutch configuration files."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
UNRESOLVED_ENV_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*")


def _with_hutch_repo(value: str) -> str:
    return value.replace("${HUTCH_REPO}", str(ROOT)).replace("$HUTCH_REPO", str(ROOT))


def expand_config_path(value: str | Path, *, base: Path = ROOT) -> Path:
    """Expand env/user references and resolve relative paths from the repo root."""
    text = os.path.expandvars(_with_hutch_repo(str(value))).strip()
    if not text:
        raise ValueError("empty path value")
    if UNRESOLVED_ENV_RE.search(text):
        raise ValueError(f"unresolved environment variable in path: {value}")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def expand_config_paths(values: Iterable[str | Path], *, base: Path = ROOT) -> list[Path]:
    """Expand a list of config paths, supporting PATH-style env var lists."""
    expanded: list[Path] = []
    for raw in values:
        text = os.path.expandvars(_with_hutch_repo(str(raw))).strip()
        if not text or UNRESOLVED_ENV_RE.search(text):
            continue
        parts = text.split(os.pathsep) if os.pathsep in text else [text]
        for part in parts:
            if part:
                expanded.append(expand_config_path(part, base=base))
    return expanded


def default_cao_repo() -> Path:
    value = os.environ.get("CAO_REPO")
    if value:
        return expand_config_path(value)
    for candidate in (
        ROOT.parent / "cli-agent-orchestrator",
        ROOT.parent / "lab" / "cli-agent-orchestrator",
    ):
        if (candidate / "pyproject.toml").is_file():
            return candidate.resolve()
    raise RuntimeError(
        "CAO_REPO is not set and no adjacent cli-agent-orchestrator checkout was found"
    )


def hutch_home() -> Path:
    """Return Hutch's mutable runtime root.

    The repository stores source workflows, templates, and code. Files that may
    change during normal operation live below ``~/.hutch`` by default so they do
    not dirty or accidentally enter the Git checkout. Operators can override the
    root with ``HUTCH_HOME``.
    """
    value = os.environ.get("HUTCH_HOME")
    if value:
        return expand_config_path(value)
    return (Path.home() / ".hutch").resolve()


def hutch_runtime_dir(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError(f"invalid Hutch runtime directory name: {name!r}")
    return hutch_home() / name


def hutch_runs_dir() -> Path:
    return hutch_runtime_dir("runs")


def hutch_generated_dir() -> Path:
    return hutch_runtime_dir("generated")


def hutch_workflows_dir() -> Path:
    return hutch_runtime_dir("workflows")


def default_agents_store_source() -> Path:
    return ROOT / "agents_store"


def default_flows_store_source() -> Path:
    return ROOT / "flows_store"


def hutch_agents_store() -> Path:
    value = os.environ.get("HUTCH_AGENTS_STORE")
    if value:
        return expand_config_path(value)
    return hutch_runtime_dir("agents_store")


def hutch_flows_store() -> Path:
    value = os.environ.get("HUTCH_FLOWS_STORE")
    if value:
        return expand_config_path(value)
    return hutch_runtime_dir("flows_store")


def hutch_projects_file() -> Path:
    value = os.environ.get("HUTCH_PROJECTS_FILE")
    if value:
        return expand_config_path(value)
    return hutch_runtime_dir("projects") / "projects.json"


def default_skill_roots() -> list[Path]:
    configured = os.environ.get("HUTCH_SKILL_ROOTS") or os.environ.get("HUTCH_SKILL_ROOT")
    roots = expand_config_paths([configured]) if configured else []
    for candidate in (
        ROOT.parent / "opencode_multi_agents" / ".opencode" / "skills",
        ROOT / "third_party" / "skills",
    ):
        if candidate.is_dir():
            roots.append(candidate.resolve())
    return roots


def repo_relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def config_relative(path: Path) -> str:
    """Return a path that Hutch config can resolve from the repository root."""
    return Path(os.path.relpath(path.resolve(), ROOT)).as_posix()
