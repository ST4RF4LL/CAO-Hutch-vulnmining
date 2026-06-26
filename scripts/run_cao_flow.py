#!/usr/bin/env python3
"""Run a durable, file-gated Rabbit Hutch workflow on CAO.

The workflow file is JSON-compatible YAML so the MVP has no Python package
dependencies. CAO remains an external runtime; this runner never edits its checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adaptive_audit import (
    AdaptiveAuditError,
    build_inventories,
    validate_audit_plan,
    validate_coverage_document,
)
from agent_cells import (
    AgentCellError,
    install_opencode_agent_policy,
    prepare_agent_cells,
    validate_cell_specs,
)
from hutch_paths import default_cao_repo, expand_config_path, expand_config_paths
from hutch_paths import hutch_runs_dir


ROOT = Path(__file__).resolve().parents[1]
try:
    DEFAULT_CAO_REPO = default_cao_repo()
except RuntimeError:
    DEFAULT_CAO_REPO = ROOT.parent / "cli-agent-orchestrator"
SKIP_DIRS = {
    ".atlas",
    ".git",
    ".gradle",
    ".idea",
    ".vscode",
    "build",
    "node_modules",
    "target",
}
TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cfg",
    ".conf",
    ".cpp",
    ".gradle",
    ".groovy",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".json",
    ".kt",
    ".kts",
    ".md",
    ".properties",
    ".proto",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".toml",
    ".txt",
    ".ts",
    ".tsx",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_NAMES = {
    "Dockerfile",
    "LICENSE",
    "Makefile",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "README",
    "gradlew",
    "mvnw",
}


class FlowError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_event(run_dir: Path, event: str, **fields: Any) -> None:
    record = {"ts": now(), "event": event, **fields}
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def execute(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise FlowError(f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}")
    return result


def source_fingerprint(target: Path) -> dict[str, Any]:
    return {
        "target": str(target.resolve()),
        "method": "path-only",
    }


def target_reference_inventories(target: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source = str(target.resolve())
    repository = {
        "schema": "hutch.repository-inventory.v1",
        "source": source,
        "inventory_mode": "target-reference",
        "file_count": 0,
        "source_file_count": 0,
        "bytes": 0,
        "languages": {},
        "module_count": 1,
    }
    inventory = {
        "schema": "hutch.module-inventory.v1",
        "source": source,
        "inventory_mode": "target-reference",
        "module_count": 1,
        "modules": [
            {
                "id": "root",
                "path": ".",
                "source_file_count": 0,
                "tracked_file_count": 0,
                "bytes": 0,
                "languages": {},
                "build_descriptors": [],
            }
        ],
    }
    return repository, inventory


def should_copy(path: Path, size: int, max_file_bytes: int) -> bool:
    return size <= max_file_bytes and (
        path.suffix.lower() in TEXT_SUFFIXES
        or path.name in TEXT_NAMES
        or path.name.startswith(("README.", "Dockerfile."))
    )


def create_snapshot(source: Path, destination: Path, max_file_bytes: int) -> dict[str, Any]:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    copied_files = 0
    copied_bytes = 0
    skipped_files = 0
    for root, dirs, files in os.walk(source):
        dirs[:] = sorted(directory for directory in dirs if directory not in SKIP_DIRS)
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        for filename in sorted(files):
            source_file = root_path / filename
            try:
                size = source_file.stat().st_size
            except OSError:
                skipped_files += 1
                continue
            if not should_copy(source_file, size, max_file_bytes):
                skipped_files += 1
                continue
            destination_file = destination / relative_root / filename
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination_file, follow_symlinks=True)
            copied_files += 1
            copied_bytes += size
    return {
        "copied_files": copied_files,
        "copied_bytes": copied_bytes,
        "skipped_files": skipped_files,
        "excluded_directories": sorted(SKIP_DIRS),
    }


def prepare_shared_contracts(
    workflow: dict[str, Any],
    run_dir: Path,
    source: Path | None = None,
    *,
    scan_source: bool = True,
) -> None:
    """Create deterministic inventory and safely import upstream campaign artifacts."""
    shared = run_dir / "shared"
    source = source or shared / "target-snapshot"
    repository, modules = (
        build_inventories(source)
        if scan_source
        else target_reference_inventories(source)
    )
    atomic_json(shared / "repository-inventory.json", repository)
    atomic_json(shared / "modules.json", modules)
    audit_plan = workflow.get("audit_plan")
    if audit_plan is not None:
        validate_audit_plan(modules, audit_plan)
        atomic_json(shared / "audit-plan.json", audit_plan)
    for seed in workflow.get("seed_artifacts", []):
        if not isinstance(seed, dict):
            raise FlowError("seed_artifacts entries must be objects")
        source = Path(str(seed.get("source", ""))).expanduser().resolve()
        destination_value = str(seed.get("destination", ""))
        destination = (run_dir / destination_value).resolve()
        if not source.is_file():
            raise FlowError(f"seed artifact is not a file: {source}")
        if run_dir.resolve() not in destination.parents:
            raise FlowError(f"seed artifact destination escapes run directory: {destination_value}")
        if destination_value in {
            "shared/repository-inventory.json",
            "shared/modules.json",
            "shared/audit-plan.json",
        }:
            raise FlowError(f"seed artifact cannot replace a Hutch contract: {destination_value}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def index_snapshot(run_dir: Path, snapshot: Path, analysis: str) -> None:
    shared = run_dir / "shared"
    commands = [
        ["atlas", "index", "--project", str(snapshot), "--analysis", analysis],
        ["atlas", "doctor", "--project", str(snapshot)],
        ["atlas", "status", "--project", str(snapshot)],
        ["atlas", "files", "--project", str(snapshot)],
    ]
    labels = ["index", "doctor", "status", "files"]
    outputs: dict[str, str] = {}
    for label, command in zip(labels, commands):
        result = execute(command, timeout=1800, check=False)
        outputs[label] = result.stdout
        (shared / f"atlas-{label}.txt").write_text(result.stdout, encoding="utf-8")
        if result.returncode != 0:
            raise FlowError(f"Atlas {label} failed ({result.returncode}):\n{result.stdout}")
    (shared / "atlas-status.txt").write_text(
        "# Atlas doctor\n" + outputs["doctor"] + "\n# Atlas status\n" + outputs["status"],
        encoding="utf-8",
    )


def load_workflow(path: Path) -> dict[str, Any]:
    try:
        workflow = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FlowError(f"invalid JSON-compatible workflow {path}: {error}") from error
    if workflow.get("schema") != "hutch.workflow.v1" or not workflow.get("stages"):
        raise FlowError("workflow must use hutch.workflow.v1 and define stages")
    if workflow.get("skill_roots"):
        workflow["skill_roots"] = [
            str(item) for item in expand_config_paths(workflow.get("skill_roots", []))
        ]
    stage_ids = [stage["id"] for stage in workflow["stages"]]
    if len(stage_ids) != len(set(stage_ids)):
        raise FlowError("stage ids must be unique")
    seen: set[str] = set()
    for stage in workflow["stages"]:
        missing = set(stage.get("depends_on", [])) - seen
        if missing:
            raise FlowError(f"stage {stage['id']} has unresolved dependencies: {sorted(missing)}")
        seen.add(stage["id"])
    try:
        validate_cell_specs(
            workflow,
            (
                {
                    "id": stage["id"],
                    "profile": stage["profile"],
                    "skills": stage.get("skills", []),
                    "skill_sources": stage.get("skill_sources", {}),
                }
                for stage in workflow["stages"]
            ),
        )
    except AgentCellError as error:
        raise FlowError(str(error)) from error
    return workflow


def task_document(stage: dict[str, Any], run_dir: Path, target: Path | None = None) -> dict[str, Any]:
    target_value = (
        {
            "type": "target_project",
            "path": str(target.resolve()),
            "read_only": True,
        }
        if target is not None
        else {
            "type": "source_snapshot",
            "path": "shared/target-snapshot",
            "immutable": True,
        }
    )
    document = {
        "schema": "hutch.task.v1",
        "task_id": stage["task_id"],
        "stage": stage["id"],
        "objective": stage["objective"],
        "target": target_value,
        "inputs": stage.get("inputs", []),
        "outputs": {
            "artifact": stage["artifact"],
            "required_artifacts": stage.get("required_artifacts", []),
            "result": f"outbox/{stage['task_id']}.result.json",
        },
        "acceptance": {"required_sections": stage.get("required_sections", [])},
        "constraints": {
            "no_source_modification": True,
            "no_network": True,
            "no_target_execution": True,
            "write_roots": ["artifacts", "outbox", "tmp"],
        },
        "run_directory": str(run_dir),
    }
    if stage.get("scope"):
        document["scope"] = stage["scope"]
        document["constraints"]["source_read_scope"] = stage["scope"].get("paths", [])
    if stage.get("coverage_contract"):
        document["coverage_contract"] = stage["coverage_contract"]
    if stage.get("audit_plan_contract"):
        document["audit_plan_contract"] = {
            **stage["audit_plan_contract"],
            "schema": "hutch.audit-plan.v1",
            "strategy_allowed": ["whole_repo", "sharded", "hybrid"],
            "max_concurrency_range": [1, 16],
            "task_required_fields": ["id", "module_ids", "paths", "skills"],
            "completeness_rule": "the union of task.module_ids must equal every id in the module inventory",
        }
    if stage.get("domain_plan_contract"):
        document["domain_plan_contract"] = {
            **stage["domain_plan_contract"],
            "schema": "hutch.domain-audit-plan.v1",
            "allowed_actions": ["run", "skip"],
            "decision_required_fields": ["domain", "action", "reason", "evidence"],
        }
    if stage.get("json_contracts"):
        document["json_contracts"] = stage["json_contracts"]
    if stage.get("report_consistency"):
        document["report_consistency"] = stage["report_consistency"]
    return document


def validate_result(stage: dict[str, Any], run_dir: Path) -> tuple[bool, str]:
    result_path = run_dir / "outbox" / f"{stage['task_id']}.result.json"
    if not result_path.is_file():
        return False, "result file is absent"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return False, f"result JSON is incomplete: {error}"
    if result.get("schema") != "hutch.result.v1":
        return False, "unexpected result schema"
    if result.get("task_id") != stage["task_id"] or result.get("stage") != stage["id"]:
        return False, "result task or stage does not match"
    if result.get("status") != "done":
        return False, f"result status is {result.get('status')!r}"
    artifact = run_dir / stage["artifact"]
    if not artifact.is_file() or artifact.stat().st_size == 0:
        return False, "artifact is absent or empty"
    artifact_text = artifact.read_text(encoding="utf-8")
    missing = [heading for heading in stage.get("required_sections", []) if f"## {heading}" not in artifact_text]
    if missing:
        return False, f"artifact is missing sections: {missing}"
    declared = result.get("artifacts", [])
    if stage["artifact"] not in declared:
        return False, "result does not declare the required artifact"
    for required in stage.get("required_artifacts", []):
        required_path = run_dir / required
        if not required_path.is_file() or required_path.stat().st_size == 0:
            return False, f"required artifact is absent or empty: {required}"
        if required not in declared:
            return False, f"result does not declare required artifact: {required}"
    for contract in stage.get("json_contracts", []):
        try:
            value = json.loads((run_dir / contract["artifact"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return False, f"JSON contract is invalid for {contract['artifact']}: {error}"
        if value.get("schema") != contract["schema"]:
            return False, (
                f"JSON contract {contract['artifact']} must use {contract['schema']}"
            )
        missing_fields = [field for field in contract.get("required_fields", []) if field not in value]
        if missing_fields:
            return False, (
                f"JSON contract {contract['artifact']} is missing fields: {missing_fields}"
            )
        if contract.get("module_coverage"):
            try:
                inventory = json.loads(
                    (run_dir / contract.get("inventory", "shared/modules.json")).read_text(
                        encoding="utf-8"
                    )
                )
                expected = {module["id"] for module in inventory["modules"]}
                entries = value[contract.get("module_field", "modules")]
                actual = set()
                for entry in entries:
                    module_id = (
                        entry
                        if isinstance(entry, str)
                        else entry.get("module_id", entry.get("id"))
                    )
                    if not isinstance(module_id, str) or not module_id:
                        raise KeyError("module_id or id")
                    actual.add(module_id)
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
                return False, f"JSON module coverage is invalid for {contract['artifact']}: {error}"
            if actual != expected:
                return False, (
                    f"JSON contract {contract['artifact']} module coverage mismatch; "
                    f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
                )
    valid_coverage, coverage_reason = validate_coverage_document(stage, run_dir)
    if not valid_coverage:
        return False, coverage_reason
    if stage.get("audit_plan_contract"):
        try:
            inventory = json.loads(
                (run_dir / stage["audit_plan_contract"]["inventory"]).read_text(
                    encoding="utf-8"
                )
            )
            plan = json.loads(
                (run_dir / stage["audit_plan_contract"]["artifact"]).read_text(
                    encoding="utf-8"
                )
            )
            allowed = stage["audit_plan_contract"].get("allowed_skills")
            normalized_plan = validate_audit_plan(
                inventory,
                plan,
                allowed_skills=set(allowed) if allowed is not None else None,
            )
            atomic_json(
                run_dir / stage["audit_plan_contract"]["artifact"], normalized_plan
            )
        except (OSError, json.JSONDecodeError, AdaptiveAuditError) as error:
            return False, f"audit plan is invalid: {error}"
    if stage.get("domain_plan_contract"):
        contract = stage["domain_plan_contract"]
        try:
            plan = json.loads(
                (run_dir / contract["artifact"]).read_text(encoding="utf-8")
            )
            if plan.get("schema") != "hutch.domain-audit-plan.v1":
                raise ValueError("unexpected schema")
            decisions = plan.get("decisions")
            if not isinstance(decisions, list):
                raise ValueError("decisions must be an array")
            expected = list(contract["domains"])
            indexed: dict[str, dict[str, Any]] = {}
            for decision in decisions:
                if not isinstance(decision, dict):
                    raise ValueError("each decision must be an object")
                domain = decision.get("domain")
                if domain in indexed:
                    raise ValueError(f"duplicate domain decision: {domain}")
                if domain not in expected:
                    raise ValueError(f"unknown domain decision: {domain}")
                if decision.get("action") not in {"run", "skip"}:
                    raise ValueError(f"invalid action for {domain}")
                if not str(decision.get("reason", "")).strip():
                    raise ValueError(f"missing reason for {domain}")
                evidence = decision.get("evidence")
                if not isinstance(evidence, list):
                    raise ValueError(f"evidence must be an array for {domain}")
                if decision.get("action") == "skip" and not evidence:
                    raise ValueError(f"skip decision requires evidence for {domain}")
                indexed[str(domain)] = decision
            missing = [domain for domain in expected if domain not in indexed]
            if missing:
                raise ValueError(f"missing domain decisions: {missing}")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            return False, f"domain audit plan is invalid: {error}"
    if stage.get("report_consistency"):
        contract = stage["report_consistency"]
        try:
            validation = json.loads(
                (run_dir / contract["validation_result"]).read_text(encoding="utf-8")
            )
            validated_findings = validation["findings"]
            report_findings = result["findings"]
            key = lambda finding: (
                finding.get("id"),
                finding.get("status"),
                str(finding.get("severity", "")).lower(),
            )
            if sorted(map(key, validated_findings)) != sorted(map(key, report_findings)):
                return False, "final report result findings differ from validated findings"
            status_counts = Counter(finding.get("status") for finding in validated_findings)
            raw_count = 0
            for relative in contract.get("audit_results", []):
                audit_result = json.loads((run_dir / relative).read_text(encoding="utf-8"))
                raw_count += len(audit_result.get("findings", []))
            expected_metrics = [
                (("Raw candidate findings",), raw_count),
                (("Validated findings (post-deduplication)", "Validated findings after deduplication"), len(validated_findings)),
                (("Findings confirmed", "Confirmed"), status_counts["confirmed"]),
                (("Findings likely", "Likely"), status_counts["likely"]),
                (("Findings needs-info", "Needs-info"), status_counts["needs-info"]),
            ]
            for labels, expected in expected_metrics:
                match = next(
                    (
                        candidate
                        for label in labels
                        if (
                            candidate := re.search(
                                rf"(?im)^\|\s*{re.escape(label)}\s*\|\s*(\d+)\s*\|",
                                artifact_text,
                            )
                        )
                    ),
                    None,
                )
                if not match:
                    return False, f"final report metric is absent: {labels[0]}"
                if int(match.group(1)) != expected:
                    return False, (
                        f"final report metric mismatch for {labels[0]}: "
                        f"expected {expected}, got {match.group(1)}"
                    )
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            return False, f"final report consistency check failed: {error}"
    return True, "validated"


class CaoRuntime:
    def __init__(self, repo: Path, base_url: str) -> None:
        self.repo = repo.resolve()
        self.base_url = base_url.rstrip("/")
        self.command = ["uv", "--directory", str(self.repo), "run", "cao"]
        self.launch_processes: dict[str, subprocess.Popen[str]] = {}

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        timeout: int = 10,
    ) -> Any:
        query = "?" + urllib.parse.urlencode(params) if params else ""
        request = urllib.request.Request(f"{self.base_url}{path}{query}", method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def require_healthy(self) -> None:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=5) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise FlowError(f"CAO is not healthy at {self.base_url}: {error}") from error
        if payload.get("status") != "ok":
            raise FlowError(f"CAO health response is not ok: {payload}")

    def install(self, profile_path: Path, provider: str) -> None:
        execute(
            self.command + ["install", str(profile_path.resolve()), "--provider", provider],
            cwd=self.repo,
            timeout=120,
        )

    def launch(
        self,
        profile: str,
        provider: str,
        session: str,
        working_directory: Path,
        message: str,
    ) -> str:
        short_name = re.sub(r"[^a-zA-Z0-9_-]", "-", session)
        if provider == "opencode_cli":
            return self.launch_opencode_without_status_gate(
                profile, short_name, working_directory, message
            )
        execute(
            self.command
            + [
                "launch",
                "--agents",
                profile,
                "--provider",
                provider,
                "--headless",
                "--auto-approve",
                "--async",
                "--session-name",
                short_name,
                "--working-directory",
                str(working_directory.resolve()),
                message,
            ],
            cwd=self.repo,
            timeout=120,
        )
        return short_name if short_name.startswith("cao-") else f"cao-{short_name}"

    def launch_opencode_without_status_gate(
        self,
        profile: str,
        short_name: str,
        working_directory: Path,
        message: str,
    ) -> str:
        """Dispatch through CAO REST without trusting OpenCode's raw TUI status."""
        session = short_name if short_name.startswith("cao-") else f"cao-{short_name}"
        command = self.command + [
            "launch",
            "--agents",
            profile,
            "--provider",
            "opencode_cli",
            "--headless",
            "--auto-approve",
            "--async",
            "--session-name",
            short_name,
            "--working-directory",
            str(working_directory.resolve()),
        ]
        process = subprocess.Popen(
            command,
            cwd=self.repo,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.launch_processes[session] = process
        deadline = time.monotonic() + 45
        terminal_id: str | None = None
        while time.monotonic() < deadline:
            try:
                terminals = self.request_json(
                    "GET", f"/sessions/{session}/terminals", timeout=5
                )
                matching = [
                    terminal
                    for terminal in terminals
                    if terminal.get("agent_profile") == profile
                ]
                if matching:
                    terminal_id = matching[-1]["id"]
                    output = self.request_json(
                        "GET",
                        f"/terminals/{terminal_id}/output",
                        params={"mode": "full"},
                        timeout=5,
                    ).get("output", "")
                    if f"Agent not found: {profile}" in output:
                        raise FlowError(f"OpenCode agent was not installed: {profile}")
                    if "ctrl+p" in output and "commands" in output:
                        break
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
                pass
            time.sleep(1)
        if terminal_id is None:
            raise FlowError(f"OpenCode terminal did not appear for {session}")
        self.signal_opencode_ready(terminal_id)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        self.request_json(
            "POST",
            f"/terminals/{terminal_id}/input",
            params={
                "message": message
                + " Hard stop: finish and write the contracted files within 90 seconds."
            },
            timeout=10,
        )
        return session

    @staticmethod
    def signal_opencode_ready(terminal_id: str) -> None:
        """Bridge a composited idle screen into CAO's raw FIFO status stream.

        This is intentionally isolated compatibility code for OpenCode 1.16.x.
        The caller has already verified the idle footer through CAO's output API.
        """
        fifo = (
            Path.home()
            / ".aws"
            / "cli-agent-orchestrator"
            / "fifos"
            / f"{terminal_id}.fifo"
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not fifo.exists():
            time.sleep(0.1)
        if not fifo.exists():
            raise FlowError(f"CAO compatibility FIFO is absent for terminal {terminal_id}")
        descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(descriptor, b"\r\nctrl+p commands\r\n")
        finally:
            os.close(descriptor)

    def status(self, session: str) -> str:
        result = execute(
            self.command + ["session", "status", session, "--json"],
            cwd=self.repo,
            timeout=30,
            check=False,
        )
        return result.stdout.strip()

    def shutdown(self, session: str) -> None:
        execute(
            self.command + ["shutdown", "--session", session],
            cwd=self.repo,
            timeout=60,
            check=False,
        )
        process = self.launch_processes.pop(session, None)
        if process is not None and process.poll() is None:
            process.terminate()


def prepare_run(workflow: dict[str, Any], run_dir: Path, target: Path) -> dict[str, Any]:
    for directory in ("artifacts", "inbox", "outbox", "shared", "tmp"):
        (run_dir / directory).mkdir(parents=True, exist_ok=True)
    fingerprint = source_fingerprint(target)
    atomic_json(run_dir / "shared" / "source-fingerprint.json", fingerprint)
    snapshot_stats = create_snapshot(
        target,
        run_dir / "shared" / "target-snapshot",
        int(workflow.get("snapshot", {}).get("max_file_bytes", 2_097_152)),
    )
    atomic_json(run_dir / "shared" / "snapshot-manifest.json", snapshot_stats)
    prepare_shared_contracts(workflow, run_dir)
    index_snapshot(
        run_dir,
        run_dir / "shared" / "target-snapshot",
        workflow.get("snapshot", {}).get("analysis", "structural"),
    )
    cells = prepare_agent_cells(
        workflow,
        run_dir,
        (
            {
                "id": stage["id"],
                "profile": stage["profile"],
                "skills": stage.get("skills", []),
                "skill_sources": stage.get("skill_sources", {}),
                "profile_source": ROOT / stage["profile_file"],
            }
            for stage in workflow["stages"]
        ),
    )
    for stage in workflow["stages"]:
        document = task_document(stage, run_dir)
        document["agent_profile"] = stage["profile"]
        document["agent_cell"] = cells[stage["id"]]
        atomic_json(run_dir / "inbox" / f"{stage['task_id']}.task.json", document)
    state = {
        "schema": "hutch.state.v1",
        "run_id": run_dir.name,
        "workflow": workflow["name"],
        "status": "prepared",
        "created_at": now(),
        "target_fingerprint": fingerprint,
        "snapshot": snapshot_stats,
        "campaign": workflow.get("campaign"),
        "agent_cells": cells,
        "stages": {
            stage["id"]: {
                "status": "pending",
                "task_id": stage["task_id"],
                "agent_profile": stage["profile"],
                "agent_cell": stage["id"],
                "workspace": cells[stage["id"]]["workspace"],
            }
            for stage in workflow["stages"]
        },
    }
    atomic_json(run_dir / "state.json", state)
    append_event(run_dir, "run_prepared", fingerprint=fingerprint, snapshot=snapshot_stats)
    return state


def run_stage(
    runtime: CaoRuntime,
    stage: dict[str, Any],
    default_provider: str,
    run_dir: Path,
    state: dict[str, Any],
    timeout: int,
    poll_interval: float,
) -> None:
    valid, _ = validate_result(stage, run_dir)
    if valid:
        state["stages"][stage["id"]]["status"] = "done"
        atomic_json(run_dir / "state.json", state)
        append_event(run_dir, "stage_skipped", stage=stage["id"], reason="valid result already exists")
        return
    for dependency in stage.get("depends_on", []):
        if state["stages"][dependency]["status"] != "done":
            raise FlowError(f"stage {stage['id']} dependency {dependency} is not done")
    task_path = run_dir / "inbox" / f"{stage['task_id']}.task.json"
    provider = stage.get("provider", default_provider)
    stage_state = state["stages"][stage["id"]]
    attempt = int(stage_state.get("attempt", 0)) + 1
    stage_state.update({"attempt": attempt, "status": "launching", "provider": provider})
    atomic_json(run_dir / "state.json", state)
    append_event(
        run_dir,
        "stage_launching",
        stage=stage["id"],
        task_id=stage["task_id"],
        attempt=attempt,
        provider=provider,
    )
    session = runtime.launch(
        stage["profile"],
        provider,
        f"hutch-{run_dir.name}-{stage['id']}-a{attempt}",
        Path(stage_state["workspace"]),
        (
            f"Execute Rabbit Hutch task {task_path}. Read the JSON contract exactly. "
            f"Write {stage['artifact']} and then outbox/{stage['task_id']}.result.json. "
            "The source snapshot is immutable; do not use the network or execute target code."
        ),
    )
    state["status"] = "running"
    state["current_stage"] = stage["id"]
    state["stages"][stage["id"]].update({"status": "running", "session": session, "started_at": now()})
    atomic_json(run_dir / "state.json", state)
    append_event(run_dir, "stage_started", stage=stage["id"], task_id=stage["task_id"], session=session)
    deadline = time.monotonic() + timeout
    next_status = 0.0
    last_reason = "result file is absent"
    try:
        while time.monotonic() < deadline:
            valid, last_reason = validate_result(stage, run_dir)
            if valid:
                state["stages"][stage["id"]].update({"status": "done", "finished_at": now()})
                atomic_json(run_dir / "state.json", state)
                append_event(run_dir, "stage_completed", stage=stage["id"], task_id=stage["task_id"])
                return
            if time.monotonic() >= next_status:
                status_text = runtime.status(session)
                (run_dir / "tmp" / f"{stage['id']}-cao-status.json").write_text(status_text + "\n", encoding="utf-8")
                next_status = time.monotonic() + 20
            time.sleep(poll_interval)
        raise FlowError(f"stage {stage['id']} timed out after {timeout}s; last gate: {last_reason}")
    finally:
        runtime.shutdown(session)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S-djl-security-review"))
    parser.add_argument("--runs-dir", type=Path, default=hutch_runs_dir())
    parser.add_argument("--cao-repo", type=Path, default=DEFAULT_CAO_REPO)
    parser.add_argument("--cao-url", default="http://127.0.0.1:9889")
    parser.add_argument("--stage-timeout", type=int, default=900)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    workflow_path = args.workflow.resolve()
    workflow = load_workflow(workflow_path)
    target = args.target.resolve() if args.target else expand_config_path(workflow["target"])
    if not (target / ".git").exists():
        raise FlowError(f"target is not a Git checkout: {target}")
    run_dir = args.runs_dir.resolve() / args.run_id
    if run_dir.exists() and not args.resume:
        raise FlowError(f"run already exists; pass --resume: {run_dir}")

    if args.resume:
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state.pop("error", None)
        state.pop("failed_at", None)
        atomic_json(run_dir / "workflow.json", workflow)
        cells = prepare_agent_cells(
            workflow,
            run_dir,
            (
                {
                    "id": stage["id"],
                    "profile": stage["profile"],
                    "skills": stage.get("skills", []),
                    "skill_sources": stage.get("skill_sources", {}),
                    "profile_source": ROOT / stage["profile_file"],
                }
                for stage in workflow["stages"]
            ),
        )
        state["agent_cells"] = cells
        for stage in workflow["stages"]:
            document = task_document(stage, run_dir)
            document["agent_profile"] = stage["profile"]
            document["agent_cell"] = cells[stage["id"]]
            atomic_json(
                run_dir / "inbox" / f"{stage['task_id']}.task.json",
                document,
            )
            state["stages"][stage["id"]].update(
                {
                    "agent_profile": stage["profile"],
                    "agent_cell": stage["id"],
                    "workspace": cells[stage["id"]]["workspace"],
                }
            )
        atomic_json(run_dir / "state.json", state)
    else:
        run_dir.mkdir(parents=True)
        atomic_json(run_dir / "workflow.json", workflow)
        state = prepare_run(workflow, run_dir, target)
    if args.prepare_only:
        print(run_dir)
        return 0

    runtime = CaoRuntime(args.cao_repo, args.cao_url)
    runtime.require_healthy()
    default_provider = workflow.get("provider", "opencode_cli")
    installs: dict[tuple[str, str, str], list[tuple[str, list[str]]]] = {}
    for stage in workflow["stages"]:
        key = (
            stage["profile_file"],
            stage.get("provider", default_provider),
            stage["profile"],
        )
        installs.setdefault(key, []).append((stage["id"], stage.get("skills", [])))
    for (profile_file, provider, profile), cell_skills in sorted(installs.items()):
        profile_path = ROOT / profile_file
        runtime.install(profile_path, provider)
        if provider == "opencode_cli":
            install_opencode_agent_policy(profile_path, profile, cell_skills)
    try:
        for stage in workflow["stages"]:
            run_stage(
                runtime,
                stage,
                default_provider,
                run_dir,
                state,
                args.stage_timeout,
                args.poll_interval,
            )
        after = source_fingerprint(target)
        if after != state["target_fingerprint"]:
            raise FlowError(f"target working tree changed during flow: before={state['target_fingerprint']} after={after}")
        state.update({"status": "completed", "current_stage": None, "finished_at": now(), "final_target_fingerprint": after})
        atomic_json(run_dir / "state.json", state)
        append_event(run_dir, "run_completed", fingerprint=after)
    except Exception as error:
        state.update({"status": "failed", "error": str(error), "failed_at": now()})
        atomic_json(run_dir / "state.json", state)
        append_event(run_dir, "run_failed", error=str(error))
        raise
    print(run_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FlowError as error:
        print(f"flow error: {error}", file=sys.stderr)
        raise SystemExit(1)
