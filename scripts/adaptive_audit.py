#!/usr/bin/env python3
"""Build and validate adaptive audit campaigns without invoking an LLM.

Hutch owns inventory, plan validation, workflow compilation, and coverage gates.
CAO remains the only runtime for the generated supervisor and worker agents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from agent_cells import discover_skills


SOURCE_SUFFIXES = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".php": "php",
    ".proto": "protobuf",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scala": "scala",
    ".sh": "shell",
    ".ts": "typescript",
    ".tsx": "typescript",
}
BUILD_FILES = {
    "build.gradle",
    "build.gradle.kts",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "setup.py",
}
SKIP_DIRS = {
    ".atlas",
    ".git",
    ".gradle",
    ".idea",
    ".vscode",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
STRATEGIES = {"whole_repo", "sharded", "hybrid"}
MODULE_STATUSES = {"audited", "deferred", "failed"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_MAX_MODULES_PER_TASK = 8
DEFAULT_MAX_SOURCE_FILES_PER_TASK = 800


class AdaptiveAuditError(RuntimeError):
    pass


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AdaptiveAuditError(f"invalid JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise AdaptiveAuditError(f"JSON root must be an object: {path}")
    return value


def normalized_relative_path(value: str, *, label: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise AdaptiveAuditError(f"{label} must be a safe relative path: {value!r}")
    normalized = path.as_posix().strip("/") or "."
    return normalized


def stable_module_id(relative_path: str) -> str:
    if relative_path == ".":
        return "root"
    slug = re.sub(r"[^a-z0-9]+", "-", relative_path.lower()).strip("-")
    digest = hashlib.sha256(relative_path.encode()).hexdigest()[:8]
    return f"{slug[:43].rstrip('-')}-{digest}"


def discover_module_roots(source: Path, files: list[Path]) -> set[Path]:
    roots: set[Path] = {Path(".")}
    file_set = {path.as_posix() for path in files}
    for relative in files:
        if relative.name in BUILD_FILES:
            roots.add(relative.parent)
    for relative in files:
        parts = relative.parts
        for marker in (("src", "main"), ("src", "lib")):
            for index in range(len(parts) - 1):
                if tuple(parts[index : index + 2]) == marker:
                    roots.add(Path(*parts[:index]) if index else Path("."))
                    break
    # A root build file by itself is repository metadata, not evidence that child
    # module roots should be discarded.
    return {root for root in roots if root == Path(".") or any(
        candidate == (root / name).as_posix() for name in BUILD_FILES for candidate in file_set
    ) or any(path.parts[: len(root.parts)] == root.parts for path in files)}


def build_inventories(source: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source = source.resolve()
    if not source.is_dir():
        raise AdaptiveAuditError(f"source directory does not exist: {source}")
    source_files: list[Path] = []
    all_files = 0
    total_bytes = 0
    language_totals: Counter[str] = Counter()
    source_bytes: Counter[str] = Counter()
    for root, dirs, names in os.walk(source):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        root_path = Path(root)
        for name in sorted(names):
            absolute = root_path / name
            try:
                size = absolute.stat().st_size
            except OSError:
                continue
            relative = absolute.relative_to(source)
            all_files += 1
            total_bytes += size
            language = SOURCE_SUFFIXES.get(relative.suffix.lower())
            if language:
                source_files.append(relative)
                language_totals[language] += 1
                source_bytes[language] += size
            elif name in BUILD_FILES:
                source_files.append(relative)

    module_roots = discover_module_roots(source, source_files)
    ordered_roots = sorted(module_roots, key=lambda path: (len(path.parts), path.as_posix()))
    assignments: dict[Path, list[Path]] = {root: [] for root in ordered_roots}
    for relative in source_files:
        candidates = [
            root
            for root in ordered_roots
            if root == Path(".") or relative.parts[: len(root.parts)] == root.parts
        ]
        assignments[max(candidates, key=lambda path: len(path.parts))].append(relative)

    modules: list[dict[str, Any]] = []
    for root in ordered_roots:
        assigned = assignments[root]
        # Retain the root module even if all sources belong to child modules. It
        # accounts for repository-wide build/configuration and trust controls.
        if root != Path(".") and not assigned:
            continue
        language_counts: Counter[str] = Counter()
        byte_count = 0
        for relative in assigned:
            absolute = source / relative
            try:
                byte_count += absolute.stat().st_size
            except OSError:
                pass
            language = SOURCE_SUFFIXES.get(relative.suffix.lower())
            if language:
                language_counts[language] += 1
        build_descriptors = sorted(
            (root / name).as_posix()
            for name in BUILD_FILES
            if (source / root / name).is_file()
        )
        relative_root = root.as_posix()
        modules.append(
            {
                "id": stable_module_id(relative_root),
                "path": relative_root,
                "source_file_count": sum(language_counts.values()),
                "tracked_file_count": len(assigned),
                "bytes": byte_count,
                "languages": dict(sorted(language_counts.items())),
                "build_descriptors": build_descriptors,
            }
        )

    repository = {
        "schema": "hutch.repository-inventory.v1",
        "source": str(source),
        "file_count": all_files,
        "source_file_count": sum(language_totals.values()),
        "bytes": total_bytes,
        "languages": {
            language: {"files": count, "bytes": source_bytes[language]}
            for language, count in sorted(language_totals.items())
        },
        "module_count": len(modules),
    }
    inventory = {
        "schema": "hutch.module-inventory.v1",
        "source": str(source),
        "module_count": len(modules),
        "modules": modules,
    }
    validate_module_inventory(inventory)
    return repository, inventory


def validate_module_inventory(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if inventory.get("schema") != "hutch.module-inventory.v1":
        raise AdaptiveAuditError("module inventory must use hutch.module-inventory.v1")
    modules = inventory.get("modules")
    if not isinstance(modules, list) or not modules:
        raise AdaptiveAuditError("module inventory must contain at least one module")
    indexed: dict[str, dict[str, Any]] = {}
    paths: set[str] = set()
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            raise AdaptiveAuditError(f"module {index} must be an object")
        module_id = str(module.get("id", ""))
        if not ID_RE.fullmatch(module_id) or module_id in indexed:
            raise AdaptiveAuditError(f"invalid or duplicate module id: {module_id!r}")
        path = normalized_relative_path(str(module.get("path", "")), label="module path")
        if path in paths:
            raise AdaptiveAuditError(f"duplicate module path: {path}")
        module["path"] = path
        indexed[module_id] = module
        paths.add(path)
    if inventory.get("module_count") != len(modules):
        raise AdaptiveAuditError("module_count does not match modules")
    return indexed


def validate_audit_plan(
    inventory: dict[str, Any],
    plan: dict[str, Any],
    *,
    allowed_skills: set[str] | None = None,
    max_concurrency_limit: int = 16,
    max_modules_per_task: int = DEFAULT_MAX_MODULES_PER_TASK,
    max_source_files_per_task: int = DEFAULT_MAX_SOURCE_FILES_PER_TASK,
) -> dict[str, Any]:
    modules = validate_module_inventory(inventory)
    normalizations = plan.setdefault("hutch_normalizations", [])

    def record_normalization(value: dict[str, Any]) -> None:
        if value not in normalizations:
            normalizations.append(value)

    if plan.get("schema") != "hutch.audit-plan.v1":
        raise AdaptiveAuditError("audit plan must use hutch.audit-plan.v1")
    if plan.get("strategy") not in STRATEGIES:
        raise AdaptiveAuditError(f"unsupported audit strategy: {plan.get('strategy')!r}")
    concurrency = plan.get("max_concurrency")
    if concurrency is None and plan.get("concurrency") is not None:
        concurrency = plan["concurrency"]
        plan["max_concurrency"] = concurrency
        record_normalization(
            {
                "field": "concurrency",
                "effective_field": "max_concurrency",
                "value": concurrency,
                "reason": "accepted planner field alias",
            }
        )
    if not isinstance(concurrency, int) or concurrency < 1:
        raise AdaptiveAuditError("max_concurrency must be a positive integer")
    if concurrency > max_concurrency_limit:
        plan["requested_max_concurrency"] = concurrency
        plan["max_concurrency"] = max_concurrency_limit
        record_normalization(
            {
                "field": "max_concurrency",
                "requested": concurrency,
                "effective": max_concurrency_limit,
                "reason": "Hutch bounded-concurrency limit",
            }
        )
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise AdaptiveAuditError("audit plan must contain between 1 and 128 tasks")
    expanded_tasks: list[dict[str, Any]] = []
    for raw_index, raw_task in enumerate(tasks, start=1):
        if not isinstance(raw_task, dict):
            raise AdaptiveAuditError(f"task {raw_index} must be an object")
        raw_module_ids = raw_task.get("module_ids")
        if not isinstance(raw_module_ids, list) or not raw_module_ids:
            expanded_tasks.append(raw_task)
            continue
        unknown = set(raw_module_ids) - set(modules)
        if unknown:
            expanded_tasks.append(raw_task)
            continue
        chunks: list[list[str]] = []
        current: list[str] = []
        current_files = 0
        for module_id in raw_module_ids:
            module_files = int(modules[module_id].get("source_file_count", 0))
            if current and (
                len(current) >= max_modules_per_task
                or current_files + module_files > max_source_files_per_task
            ):
                chunks.append(current)
                current = []
                current_files = 0
            current.append(module_id)
            current_files += module_files
        if current:
            chunks.append(current)
        if len(chunks) == 1:
            expanded_tasks.append(raw_task)
            continue
        parent_id = str(raw_task.get("id", f"audit-{raw_index:03d}"))
        for chunk_index, chunk in enumerate(chunks, start=1):
            child = dict(raw_task)
            child["id"] = f"{parent_id}-{chunk_index:02d}"
            child["parent_task_id"] = parent_id
            child["module_ids"] = chunk
            child["paths"] = sorted(modules[module_id]["path"] for module_id in chunk)
            expanded_tasks.append(child)
        record_normalization(
            {
                "field": f"tasks.{parent_id}",
                "requested_module_count": len(raw_module_ids),
                "effective_task_count": len(chunks),
                "reason": "split to Hutch module/file workload bounds",
            }
        )
    plan["tasks"] = expanded_tasks
    tasks = expanded_tasks
    if len(tasks) > 128:
        raise AdaptiveAuditError("audit plan must contain between 1 and 128 tasks")
    task_ids: set[str] = set()
    covered: set[str] = set()
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise AdaptiveAuditError(f"task {index} must be an object")
        task_id = str(task.get("id", f"audit-{index:03d}"))
        if not ID_RE.fullmatch(task_id) or task_id in task_ids:
            raise AdaptiveAuditError(f"invalid or duplicate task id: {task_id!r}")
        task["id"] = task_id
        task_ids.add(task_id)
        module_ids = task.get("module_ids")
        if not isinstance(module_ids, list) or not module_ids:
            raise AdaptiveAuditError(f"task {task_id} must assign at least one module")
        if len(module_ids) != len(set(module_ids)):
            raise AdaptiveAuditError(f"task {task_id} has duplicate module ids")
        unknown = set(module_ids) - set(modules)
        if unknown:
            raise AdaptiveAuditError(
                f"task {task_id} references unknown modules: {sorted(unknown)}"
            )
        source_files = sum(
            int(modules[module_id].get("source_file_count", 0))
            for module_id in module_ids
        )
        if len(module_ids) > max_modules_per_task:
            raise AdaptiveAuditError(
                f"task {task_id} exceeds Hutch workload bound: {len(module_ids)} modules "
                f"> {max_modules_per_task}"
            )
        if source_files > max_source_files_per_task and len(module_ids) > 1:
            raise AdaptiveAuditError(
                f"task {task_id} exceeds Hutch workload bound: {source_files} source files "
                f"> {max_source_files_per_task}; split the module group"
            )
        task["workload"] = {
            "module_count": len(module_ids),
            "source_file_count": source_files,
        }
        expected_paths = {modules[module_id]["path"] for module_id in module_ids}
        declared_paths = task.get("paths", sorted(expected_paths))
        if not isinstance(declared_paths, list) or not declared_paths:
            raise AdaptiveAuditError(f"task {task_id} paths must be a non-empty array")
        normalized_paths = {
            normalized_relative_path(str(path), label=f"task {task_id} path")
            for path in declared_paths
        }
        if normalized_paths != expected_paths:
            record_normalization(
                {
                    "field": f"tasks.{task_id}.paths",
                    "requested": sorted(normalized_paths),
                    "effective": sorted(expected_paths),
                    "reason": "module IDs are authoritative for source scope",
                }
            )
            normalized_paths = expected_paths
        task["paths"] = sorted(normalized_paths)
        skills = task.get("skills", [])
        if not isinstance(skills, list) or any(
            not isinstance(skill, str) or not ID_RE.fullmatch(skill) for skill in skills
        ):
            raise AdaptiveAuditError(f"task {task_id} skills must be valid names")
        if allowed_skills is not None:
            unknown_skills = set(skills) - allowed_skills
            if unknown_skills:
                raise AdaptiveAuditError(
                    f"task {task_id} requests non-allowlisted skills: {sorted(unknown_skills)}"
                )
        threat_ids = task.get("threat_ids", [])
        if not threat_ids:
            threat_ids = task.get("threats", task.get("threats_addressed", []))
            if threat_ids:
                task["threat_ids"] = threat_ids
        if not task.get("objective") and task.get("description"):
            task["objective"] = task["description"]
        if not isinstance(threat_ids, list) or any(
            not isinstance(threat_id, str) or not ID_RE.fullmatch(threat_id)
            for threat_id in threat_ids
        ):
            raise AdaptiveAuditError(f"task {task_id} threat_ids must be valid names")
        covered.update(module_ids)
    missing = set(modules) - covered
    if missing:
        raise AdaptiveAuditError(
            f"audit plan does not cover every module; missing: {sorted(missing)}"
        )
    return plan


def validate_coverage_document(
    stage: dict[str, Any], run_dir: Path
) -> tuple[bool, str]:
    contract = stage.get("coverage_contract")
    if not contract:
        return True, "not a coverage-producing stage"
    path = run_dir / contract["artifact"]
    if not path.is_file():
        return False, f"coverage artifact is absent: {contract['artifact']}"
    try:
        coverage = load_json(path)
    except AdaptiveAuditError as error:
        return False, str(error)
    if coverage.get("schema") != "hutch.coverage.v1":
        return False, "coverage must use hutch.coverage.v1"
    normalizations = coverage.setdefault("hutch_normalizations", [])

    def normalize(field: str, reason: str) -> None:
        value = {"field": field, "reason": reason}
        if value not in normalizations:
            normalizations.append(value)

    if coverage.get("task_id") is None:
        coverage["task_id"] = stage["task_id"]
        normalize("task_id", "inferred from immutable stage contract")
    if coverage.get("stage") is None:
        coverage["stage"] = stage["id"]
        normalize("stage", "inferred from immutable stage contract")
    raw_entries = coverage.get("modules")
    if raw_entries is None and isinstance(coverage.get("module_coverage"), list):
        coverage["modules"] = coverage["module_coverage"]
        raw_entries = coverage["modules"]
        normalize("module_coverage", "accepted modules field alias")
    if raw_entries is None and coverage.get("module_id") is not None:
        coverage["modules"] = [
            {
                key: coverage[key]
                for key in (
                    "module_id",
                    "status",
                    "reviewed_file_count",
                    "evidence",
                    "source_evidence",
                    "source",
                    "reason",
                )
                if key in coverage
            }
        ]
        raw_entries = coverage["modules"]
        normalize("module_id", "converted single-module coverage to modules array")
    if isinstance(raw_entries, dict):
        coverage["modules"] = [
            {"module_id": module_id, **entry}
            for module_id, entry in raw_entries.items()
            if isinstance(entry, dict)
        ]
        raw_entries = coverage["modules"]
        normalize("modules", "converted module-id mapping to entry array")
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("module_id") is None and entry.get("id") is not None:
                entry["module_id"] = entry["id"]
                normalize("modules[].id", "accepted module_id field alias")
            if entry.get("evidence") is None and entry.get("source_evidence") is not None:
                entry["evidence"] = entry["source_evidence"]
                normalize("modules[].source_evidence", "accepted evidence field alias")
            if entry.get("evidence") is None and entry.get("source") is not None:
                entry["evidence"] = entry["source"]
                normalize("modules[].source", "accepted evidence field alias")
    if normalizations:
        atomic_json(path, coverage)
    if coverage.get("task_id") != stage["task_id"] or coverage.get("stage") != stage["id"]:
        return False, "coverage task or stage does not match"
    entries = coverage.get("modules")
    if not isinstance(entries, list):
        return False, "coverage modules must be an array"
    expected = set(contract["module_ids"])
    module_paths = contract.get("module_paths", {})
    source_file_counts = contract.get("source_file_counts", {})
    actual: set[str] = set()
    failed: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return False, "coverage module entries must be objects"
        module_id = entry.get("module_id")
        if module_id in actual:
            return False, f"duplicate coverage module: {module_id}"
        if module_id not in expected:
            return False, f"coverage claims module outside task contract: {module_id}"
        status = entry.get("status")
        if status not in MODULE_STATUSES:
            return False, f"invalid coverage status for {module_id}: {status!r}"
        if status == "deferred" and not str(entry.get("reason", "")).strip():
            return False, f"deferred module requires a reason: {module_id}"
        if status == "audited":
            reviewed = entry.get("reviewed_file_count")
            minimum = 1 if int(source_file_counts.get(module_id, 0)) > 0 else 0
            if not isinstance(reviewed, int) or reviewed < minimum:
                return False, (
                    f"audited module {module_id} requires reviewed_file_count >= {minimum}"
                )
            evidence = entry.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                return False, f"audited module requires non-empty evidence: {module_id}"
            module_root = str(module_paths.get(module_id, "."))
            for item in evidence:
                if not isinstance(item, dict) or not str(item.get("observation", "")).strip():
                    return False, f"coverage evidence is invalid for module {module_id}"
                try:
                    evidence_path = normalized_relative_path(
                        str(item.get("path", "")), label="coverage evidence path"
                    )
                except AdaptiveAuditError as error:
                    return False, str(error)
                if module_root != "." and not (
                    evidence_path == module_root
                    or evidence_path.startswith(module_root.rstrip("/") + "/")
                ):
                    prefixed = normalized_relative_path(
                        f"{module_root}/{evidence_path}",
                        label="module-relative coverage evidence path",
                    )
                    if (run_dir / "shared/target-snapshot" / prefixed).exists():
                        item["path"] = prefixed
                        evidence_path = prefixed
                        normalize(
                            "modules[].evidence[].path",
                            "expanded module-relative path to repository-relative path",
                        )
                    else:
                        return False, (
                            f"coverage evidence path is outside module {module_id}: {evidence_path}"
                        )
        if status == "failed":
            failed.append(str(module_id))
        actual.add(module_id)
    missing = expected - actual
    if missing:
        return False, f"coverage omits contracted modules: {sorted(missing)}"
    if failed:
        return False, f"coverage reports failed modules and requires retry: {sorted(failed)}"
    if normalizations:
        atomic_json(path, coverage)
    return True, "validated"


def build_coverage_summary(
    workflow: dict[str, Any], run_dir: Path, gate_stage: dict[str, Any]
) -> dict[str, Any]:
    gate = gate_stage.get("coverage_gate")
    if not gate:
        raise AdaptiveAuditError(f"stage {gate_stage['id']} is not a coverage gate")
    inventory = load_json(run_dir / gate["inventory"])
    modules = validate_module_inventory(inventory)
    claims: dict[str, list[dict[str, Any]]] = {module_id: [] for module_id in modules}
    stages = {stage["id"]: stage for stage in workflow["stages"]}
    for stage_id in gate["audit_stages"]:
        stage = stages.get(stage_id)
        if not stage or not stage.get("coverage_contract"):
            raise AdaptiveAuditError(f"coverage gate references invalid audit stage: {stage_id}")
        valid, reason = validate_coverage_document(stage, run_dir)
        if not valid:
            raise AdaptiveAuditError(f"coverage from {stage_id} is invalid: {reason}")
        coverage = load_json(run_dir / stage["coverage_contract"]["artifact"])
        for entry in coverage["modules"]:
            claims[entry["module_id"]].append(
                {
                    "stage": stage_id,
                    "status": entry["status"],
                    "reason": entry.get("reason", ""),
                }
            )
    summary_modules: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []
    for module_id, module in modules.items():
        module_claims = claims[module_id]
        if any(claim["status"] == "audited" for claim in module_claims):
            status = "audited"
            reason = ""
        elif any(
            claim["status"] == "deferred" and claim["reason"].strip()
            for claim in module_claims
        ):
            status = "deferred"
            reason = "; ".join(
                claim["reason"]
                for claim in module_claims
                if claim["status"] == "deferred" and claim["reason"].strip()
            )
        else:
            status = "gap"
            reason = "no successful or justified deferred audit claim"
            gaps.append({"module_id": module_id, "reason": reason})
        summary_modules.append(
            {
                "module_id": module_id,
                "path": module["path"],
                "status": status,
                "reason": reason,
                "claims": module_claims,
            }
        )
    summary = {
        "schema": "hutch.coverage-summary.v1",
        "module_count": len(modules),
        "audited_count": sum(item["status"] == "audited" for item in summary_modules),
        "deferred_count": sum(item["status"] == "deferred" for item in summary_modules),
        "gap_count": len(gaps),
        "modules": summary_modules,
        "gaps": gaps,
    }
    atomic_json(run_dir / gate["artifact"], summary)
    if gaps:
        raise AdaptiveAuditError(
            "coverage gate found unaudited modules: "
            + ", ".join(gap["module_id"] for gap in gaps)
        )
    return summary


def compile_workflow(
    inventory: dict[str, Any],
    plan: dict[str, Any],
    *,
    name: str,
    target: Path,
    cao_repo: Path,
    skill_roots: list[Path],
    campaign_id: str,
    intelligence_run_id: str | None = None,
    planning_run_id: str | None = None,
    seed_artifacts: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    validate_audit_plan(inventory, plan)
    indexed_modules = validate_module_inventory(inventory)
    if not ID_RE.fullmatch(name) or len(name) > 40:
        raise AdaptiveAuditError("compiled workflow name must be 1-40 safe characters")
    available_skills = set(discover_skills(skill_roots)) if skill_roots else set()
    audit_skill_enabled = "audit-skills" in available_skills
    agents: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    audit_stage_ids: list[str] = []
    component_stage_id = "component-risk-intelligence"
    if audit_skill_enabled:
        agents.append(
            {
                "id": "component-risk-analyst",
                "description": "Builds component-version and deployment-artifact risk intelligence.",
                "mission": "Use the bundled audit-skills capability inside the Agent Cell. For Java targets, run its component rule validation and component scan against shared/target-snapshot with a workspace below tmp/, then translate hits into artifacts/component-risk.json and artifacts/component-risk.md. Treat every version/CVE hit only as a lead until entry-point reachability, configuration, and exploitability are proven. For .NET deployment artifacts, inventory assemblies and use already-installed decompilation tools only; never download or install tools. For other languages, document applicable dependency manifests and explicit capability limits. Do not modify or execute the target.",
                "atlas": False,
                "skills": ["audit-skills"],
            }
        )
        stages.append(
            {
                "id": component_stage_id,
                "task_id": "C-0001",
                "agent": "component-risk-analyst",
                "depends_on": [],
                "artifact": "artifacts/component-risk.md",
                "required_artifacts": ["artifacts/component-risk.json"],
                "inputs": [
                    "shared/target-snapshot",
                    "shared/repository-inventory.json",
                    "shared/modules.json",
                    "shared/intelligence",
                ],
                "required_sections": [
                    "Scope and Method",
                    "Dependency and Deployment Inventory",
                    "Component Risk Leads",
                    "Reachability Requirements",
                    "Evidence and Limitations",
                ],
                "objective": "Produce deterministic component and deployment-artifact leads without promoting version matches to confirmed vulnerabilities.",
                "json_contracts": [
                    {
                        "artifact": "artifacts/component-risk.json",
                        "schema": "hutch.component-risk.v1",
                        "required_fields": ["leads", "limitations"],
                    }
                ],
            }
        )
    for index, task in enumerate(plan["tasks"], start=1):
        agent_id = f"miner-{index:03d}"
        stage_id = f"audit-{index:03d}"
        task_id = f"A-{index:04d}"
        audit_stage_ids.append(stage_id)
        task_skills = list(task.get("skills", []))
        if audit_skill_enabled and "audit-skills" not in task_skills:
            task_skills.append("audit-skills")
        task_mission = task.get(
            "mission",
            "Audit every contracted module and path against the supplied threat intelligence. "
            "Trace attacker-controlled input to sensitive operations, inspect controls, record "
            "negative results, and never silently omit a contracted module.",
        )
        if audit_skill_enabled:
            task_mission += (
                " Apply audit-skills validity rules: a confirmed vulnerability requires a real "
                "entry point, controllable input, an unblocked source-to-sink chain, exploitable "
                "impact, and safe reproducibility evidence. Otherwise report a candidate or "
                "needs-info lead. Use only non-destructive payloads and placeholder credentials."
            )
        agents.append(
            {
                "id": agent_id,
                "description": f"Audits bounded repository shard {task['id']}.",
                "mission": task_mission,
                "atlas": bool(task.get("atlas", True)),
                "skills": task_skills,
            }
        )
        report = f"artifacts/shards/{task_id.lower()}-audit.md"
        coverage = f"artifacts/shards/{task_id.lower()}-coverage.json"
        stages.append(
            {
                "id": stage_id,
                "task_id": task_id,
                "agent": agent_id,
                "depends_on": [component_stage_id] if audit_skill_enabled else [],
                "artifact": report,
                "required_artifacts": [coverage],
                "inputs": [
                    "shared/repository-inventory.json",
                    "shared/modules.json",
                    "shared/audit-plan.json",
                    "shared/intelligence",
                    *(
                        [
                            "artifacts/component-risk.md",
                            "artifacts/component-risk.json",
                        ]
                        if audit_skill_enabled
                        else []
                    ),
                ],
                "required_sections": [
                    "Scope and Method",
                    "Module Coverage",
                    "Candidate Findings",
                    "Reviewed Hotspots and Negative Results",
                    "Evidence and Limitations",
                ],
                "objective": task.get("objective", task.get("title", task["id"])),
                "scope": {
                    "module_ids": task["module_ids"],
                    "paths": task["paths"],
                    "threat_ids": task.get("threat_ids", []),
                },
                "coverage_contract": {
                    "artifact": coverage,
                    "module_ids": task["module_ids"],
                    "module_paths": {
                        module_id: indexed_modules[module_id]["path"]
                        for module_id in task["module_ids"]
                    },
                    "source_file_counts": {
                        module_id: indexed_modules[module_id].get("source_file_count", 0)
                        for module_id in task["module_ids"]
                    },
                },
            }
        )
    gate_id = "coverage-gate"
    agents.extend(
        [
            {
                "id": "coverage-reviewer",
                "description": "Explains Hutch's deterministic module coverage calculation.",
                "mission": "Read the deterministic coverage summary. Explain audited and justified deferred modules, gaps, and limitations without changing the summary or inventing coverage.",
                "atlas": False,
                "skills": [],
            },
            {
                "id": "finding-validator",
                "description": "Validates and deduplicates candidates from every audit shard.",
                "mission": "Re-read source evidence for every candidate, reject unsupported claims, deduplicate shared root causes, and retain complete shard provenance. Confirm only when reachability, controllability, an unblocked source-to-sink path, exploitability, safe reproducibility evidence, and impact are established. For HTTP findings require a safe payload and a placeholder-safe raw request when the protocol and route are known; otherwise use likely or needs-info.",
                "atlas": True,
                "skills": ["audit-skills"] if audit_skill_enabled else [],
            },
            {
                "id": "report-writer",
                "description": "Aggregates the complete campaign audit report.",
                "mission": "Produce one evidence-linked report covering every inventory module, all validated findings, negative results, deferred work, and explicit limitations.",
                "atlas": False,
                "skills": [],
            },
        ]
    )
    audit_stages = [stage for stage in stages if stage["id"] in audit_stage_ids]
    shard_reports = [stage["artifact"] for stage in audit_stages]
    shard_results = [f"outbox/{stage['task_id']}.result.json" for stage in audit_stages]
    stages.append(
        {
            "id": gate_id,
            "task_id": "G-0001",
            "agent": "coverage-reviewer",
            "depends_on": audit_stage_ids,
            "artifact": "artifacts/coverage-report.md",
            "inputs": ["artifacts/coverage-summary.json", *shard_reports],
            "required_sections": [
                "Coverage Summary",
                "Audited Modules",
                "Deferred Modules",
                "Coverage Limitations",
            ],
            "objective": "Explain the deterministic coverage gate result for the complete module inventory.",
            "coverage_gate": {
                "inventory": "shared/modules.json",
                "audit_stages": audit_stage_ids,
                "artifact": "artifacts/coverage-summary.json",
            },
        }
    )
    stages.append(
        {
            "id": "finding-validation",
            "task_id": "V-0001",
            "agent": "finding-validator",
            "depends_on": [gate_id],
            "artifact": "artifacts/finding-validation.md",
            "inputs": ["artifacts/coverage-summary.json", *shard_reports, *shard_results],
            "required_sections": [
                "Validation Method",
                "Candidate Disposition",
                "Validated Findings",
                "Rejected Candidates",
                "Evidence and Limitations",
            ],
            "objective": "Validate and deduplicate every shard candidate against the immutable source snapshot.",
        }
    )
    if audit_skill_enabled:
        stages[-1]["inputs"].extend(
            ["artifacts/component-risk.md", "artifacts/component-risk.json"]
        )
    stages.append(
        {
            "id": "final-report",
            "task_id": "R-0001",
            "agent": "report-writer",
            "depends_on": ["finding-validation"],
            "artifact": "artifacts/final-report.md",
            "inputs": [
                "shared/repository-inventory.json",
                "shared/modules.json",
                "shared/audit-plan.json",
                "artifacts/coverage-summary.json",
                "artifacts/coverage-report.md",
                "artifacts/finding-validation.md",
                *(
                    ["artifacts/component-risk.md", "artifacts/component-risk.json"]
                    if audit_skill_enabled
                    else []
                ),
                *shard_reports,
            ],
            "required_sections": [
                "Executive Summary",
                "Scope and Constraints",
                "Architecture and Attack Surface",
                "Module Coverage",
                "Validated Findings",
                "Rejected Candidates and Leads",
                "Remediation Priorities",
                "Evidence and Limitations",
            ],
            "objective": "Aggregate the complete audit campaign without silently dropping a module, shard, candidate, or limitation.",
            "report_consistency": {
                "validation_result": "outbox/V-0001.result.json",
                "audit_results": shard_results,
            },
        }
    )
    campaign = {
        "schema": "hutch.campaign.v1",
        "id": campaign_id,
        "phase": "mining",
        "intelligence_run_id": intelligence_run_id,
        "planning_run_id": planning_run_id,
    }
    return {
        "schema": "hutch.cao-workflow.v1",
        "name": name,
        "version": "1.0.0",
        "description": "Hutch-compiled adaptive vulnerability-mining workflow.",
        "schedule": "0 0 1 1 *",
        "provider": "opencode_cli",
        "target": str(target.resolve()),
        "cao_repo": str(cao_repo.resolve()),
        "skill_roots": [str(path.resolve()) for path in skill_roots],
        "snapshot": {"max_file_bytes": 2_097_152},
        "execution": {
            "stage_timeout_seconds": 1800,
            "max_attempts": 2,
            "max_concurrency": plan["max_concurrency"],
            "register_enabled": False,
        },
        "campaign": campaign,
        "audit_plan": plan,
        "methodology": {
            "schema": "hutch.vulnerability-mining-methodology.v1",
            "tracks": [
                "routes-authentication-authorization",
                "injection-and-code-execution",
                "deserialization-and-parser-boundaries",
                "file-path-upload-and-archive",
                "ssrf-network-and-external-services",
                "secrets-configuration-and-cryptography",
                "native-ffi-and-memory-safety",
                "dependencies-components-and-supply-chain",
                "business-logic-and-tenant-isolation",
            ],
            "confirmation": [
                "reachable-entry",
                "controllable-input",
                "unblocked-source-to-sink",
                "exploitable-impact",
                "safe-reproduction",
                "evidence-path-symbol-line",
            ],
            "integrations": ["audit-skills"] if audit_skill_enabled else [],
        },
        "seed_artifacts": seed_artifacts or [],
        "agents": agents,
        "stages": stages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory_parser = commands.add_parser("inventory")
    inventory_parser.add_argument("source", type=Path)
    inventory_parser.add_argument("--output-dir", type=Path, required=True)
    validate_parser = commands.add_parser("validate-plan")
    validate_parser.add_argument("--modules", type=Path, required=True)
    validate_parser.add_argument("--plan", type=Path, required=True)
    compile_parser = commands.add_parser("compile")
    compile_parser.add_argument("--modules", type=Path, required=True)
    compile_parser.add_argument("--plan", type=Path, required=True)
    compile_parser.add_argument("--name", required=True)
    compile_parser.add_argument("--target", type=Path, required=True)
    compile_parser.add_argument("--cao-repo", type=Path, required=True)
    compile_parser.add_argument("--skill-root", type=Path, action="append", default=[])
    compile_parser.add_argument("--campaign-id", required=True)
    compile_parser.add_argument("--intelligence-run-id")
    compile_parser.add_argument("--planning-run-id")
    compile_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "inventory":
            repository, modules = build_inventories(args.source)
            atomic_json(args.output_dir / "repository-inventory.json", repository)
            atomic_json(args.output_dir / "modules.json", modules)
            result: Any = {
                "repository": str(args.output_dir / "repository-inventory.json"),
                "modules": str(args.output_dir / "modules.json"),
                "module_count": modules["module_count"],
            }
        elif args.command == "validate-plan":
            result = validate_audit_plan(load_json(args.modules), load_json(args.plan))
        else:
            inventory = load_json(args.modules)
            plan = load_json(args.plan)
            result = compile_workflow(
                inventory,
                plan,
                name=args.name,
                target=args.target,
                cao_repo=args.cao_repo,
                skill_roots=args.skill_root,
                campaign_id=args.campaign_id,
                intelligence_run_id=args.intelligence_run_id,
                planning_run_id=args.planning_run_id,
            )
            atomic_json(args.output, result)
            result = {"workflow": str(args.output), "stages": len(result["stages"])}
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        return 0
    except (AdaptiveAuditError, OSError) as error:
        print(f"adaptive audit error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
