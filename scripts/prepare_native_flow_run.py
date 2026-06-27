#!/usr/bin/env python3
"""Prepare one Hutch run for a CAO-native flow script."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_cells import prepare_agent_cells
from run_cao_flow import (
    atomic_json,
    now,
    prepare_shared_contracts,
    source_fingerprint,
    task_document,
)
from hutch_paths import expand_config_path, expand_config_paths, hutch_generated_dir, hutch_runs_dir


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_VAR_RE = re.compile(r"\[\[(\w+)\]\]")
CODEX_TRUST_MARKERS = (
    "allow codex to work in this folder",
    "do you trust the contents of this directory",
    "do you trust the files in this folder",
    "2. no, quit",
)


class LaunchError(RuntimeError):
    pass


def load_workflow(path: Path) -> dict[str, Any]:
    workflow = json.loads(path.read_text(encoding="utf-8"))
    if workflow.get("schema") != "hutch.cao-workflow.v1":
        raise ValueError("workflow must use hutch.cao-workflow.v1")
    if workflow.get("skill_roots"):
        workflow["skill_roots"] = [
            str(item) for item in expand_config_paths(workflow.get("skill_roots", []))
        ]
    return workflow


def unique_run_dir(workflow_name: str) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    return hutch_runs_dir() / f"{workflow_name}-{stamp}"


def api_base() -> str:
    host = os.environ.get("CAO_API_HOST", "127.0.0.1")
    port = os.environ.get("CAO_API_PORT", "9889")
    return f"http://{host}:{port}"


def request_json(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> Any:
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request = urllib.request.Request(
        api_base() + path + query,
        data=b"" if method != "GET" else None,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise LaunchError(f"CAO {method} {path} failed ({error.code}): {body}") from error
    except urllib.error.URLError as error:
        raise LaunchError(f"CAO {method} {path} failed: {error}") from error


def parse_flow_file(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    try:
        raw_metadata, body = text.split("---", 2)[1:]
    except ValueError as error:
        raise LaunchError(f"invalid Flow frontmatter: {path}") from error
    metadata: dict[str, str] = {}
    for raw_line in raw_metadata.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition(":")
        if not separator:
            continue
        value = raw_value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value.strip('"')
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        metadata[key.strip()] = value
    return metadata, body.lstrip("\n")


def render_template(template: str, values: dict[str, Any]) -> str:
    missing = sorted(set(TEMPLATE_VAR_RE.findall(template)) - set(values))
    if missing:
        raise LaunchError(f"Flow script output is missing template variables: {missing}")

    def replace(match: re.Match[str]) -> str:
        return str(values[match.group(1)])

    return TEMPLATE_VAR_RE.sub(replace, template)


def session_terminal_records(value: Any) -> list[dict[str, Any]]:
    records = value.get("terminals", []) if isinstance(value, dict) else value
    return records if isinstance(records, list) else []


def session_terminal_ids(session_name: str) -> set[str]:
    try:
        value = request_json(
            "GET",
            f"/sessions/{urllib.parse.quote(session_name, safe='')}/terminals",
        )
    except LaunchError:
        return set()
    return {
        str(record["id"])
        for record in session_terminal_records(value)
        if isinstance(record, dict) and record.get("id")
    }


def terminal_output(terminal_id: str) -> str:
    value = request_json(
        "GET",
        f"/terminals/{terminal_id}/output",
        params={"mode": "last"},
    )
    return str(value.get("output", ""))


def accept_codex_trust_prompt(terminal_id: str) -> bool:
    metadata = request_json("GET", f"/terminals/{terminal_id}")
    if metadata.get("provider") != "codex":
        return False
    text = terminal_output(terminal_id).lower()
    if not any(marker in text for marker in CODEX_TRUST_MARKERS):
        return False
    request_json(
        "POST",
        f"/terminals/{terminal_id}/key",
        params={"key": "Enter"},
    )
    return True


def guard_codex_trust_prompt(
    session_name: str,
    existing_ids: set[str],
    stop: threading.Event,
    timeout: float = 120.0,
) -> None:
    deadline = time.monotonic() + timeout
    session_path = urllib.parse.quote(session_name, safe="")
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            value = request_json("GET", f"/sessions/{session_path}/terminals")
            for record in session_terminal_records(value):
                if not isinstance(record, dict) or not record.get("id"):
                    continue
                terminal_id = str(record["id"])
                if terminal_id in existing_ids:
                    continue
                if accept_codex_trust_prompt(terminal_id):
                    return
        except LaunchError:
            pass
        stop.wait(0.25)


def launch_entry_from_spec(spec_path: Path) -> dict[str, Any]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    flow_file = Path(spec["flow_file"]).expanduser().resolve()
    output = spec["output"]
    metadata, prompt_template = parse_flow_file(flow_file)
    name = str(metadata["name"])
    provider = str(metadata.get("provider", "opencode_cli"))
    profile = str(metadata["agent_profile"])
    workspace = Path(metadata["working_directory"]).expanduser().resolve()
    if not workspace.is_dir():
        raise LaunchError(f"Flow entry working directory does not exist: {workspace}")
    prompt = render_template(prompt_template, output)
    session_name = f"cao-flow-{name}"
    existing_ids = session_terminal_ids(session_name)
    if existing_ids:
        request_json("DELETE", f"/sessions/{urllib.parse.quote(session_name, safe='')}")
        existing_ids = set()
    stop = threading.Event()
    guard = threading.Thread(
        target=guard_codex_trust_prompt,
        args=(session_name, existing_ids, stop),
        daemon=True,
    )
    guard.start()
    try:
        terminal = request_json(
            "POST",
            "/sessions",
            params={
                "provider": provider,
                "agent_profile": profile,
                "session_name": session_name.removeprefix("cao-"),
                "working_directory": str(workspace),
            },
            timeout=180.0,
        )
    finally:
        stop.set()
        guard.join(timeout=1.0)
    terminal_id = str(terminal["id"])
    sent = request_json(
        "POST",
        f"/terminals/{terminal_id}/input",
        params={"message": prompt},
    )
    result = {
        "ok": bool(sent.get("success", True)),
        "terminal_id": terminal_id,
        "session": session_name,
        "profile": profile,
        "provider": provider,
        "working_directory": str(workspace),
    }
    run_dir = Path(output["run_dir"])
    atomic_json(run_dir / "tmp" / "entry-launch-result.json", result)
    return result


def spawn_entry_launcher(flow_file: Path, prepared: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(prepared["output"]["run_dir"]).expanduser().resolve()
    spec_path = run_dir / "tmp" / "entry-launch.json"
    log_path = run_dir / "tmp" / "entry-launch.log"
    atomic_json(
        spec_path,
        {
            "schema": "hutch.entry-launch.v1",
            "flow_file": str(flow_file.expanduser().resolve()),
            "output": prepared["output"],
        },
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as stream:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--launch-prepared", str(spec_path)],
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return {
        "pid": process.pid,
        "spec": str(spec_path),
        "log": str(log_path),
    }


def prepare(workflow_path: Path, profiles_dir: Path | None = None) -> dict[str, Any]:
    workflow = load_workflow(workflow_path.resolve())
    target = expand_config_path(workflow["target"])
    if not target.is_dir():
        raise ValueError(f"target is not a directory: {target}")

    run_dir = unique_run_dir(workflow["name"])
    for directory in ("artifacts", "inbox", "outbox", "shared", "tmp"):
        (run_dir / directory).mkdir(parents=True, exist_ok=False)

    fingerprint = source_fingerprint(target)
    atomic_json(run_dir / "shared" / "source-fingerprint.json", fingerprint)
    atomic_json(
        run_dir / "shared" / "target-project.json",
        {
            "schema": "hutch.target-project.v1",
            "path": str(target.resolve()),
            "read_only": True,
        },
    )
    prepare_shared_contracts(workflow, run_dir, target, scan_source=False)
    atomic_json(run_dir / "workflow.json", workflow)

    generated_profiles = {
        agent["id"]: f"{workflow['name']}-{agent['id']}" for agent in workflow["agents"]
    }
    profiles_dir = (
        profiles_dir.resolve()
        if profiles_dir
        else hutch_generated_dir() / workflow["name"] / "profiles"
    )
    cells = prepare_agent_cells(
        workflow,
        run_dir,
        (
            {
                "id": agent["id"],
                "profile": generated_profiles[agent["id"]],
                "skills": agent.get("skills", []),
                "skill_sources": agent.get("skill_sources", {}),
                "profile_source": profiles_dir / f"{generated_profiles[agent['id']]}.md",
                "agent_store": agent.get("agent_store"),
            }
            for agent in workflow["agents"]
        ),
    )
    for stage in workflow["stages"]:
        document = task_document(stage, run_dir, target)
        document["agent_profile"] = generated_profiles[stage["agent"]]
        document["agent_cell"] = cells[stage["agent"]]
        document["finding_contract"] = {
            "required_for_candidates": [
                "id",
                "title",
                "severity",
                "confidence",
                "status",
                "weakness",
                "evidence",
                "impact",
                "assumptions",
            ],
            "allowed_status": [
                "candidate",
                "confirmed",
                "likely",
                "needs-info",
                "false-positive",
            ],
            "evidence_fields": ["path", "line", "symbol", "observation"],
        }
        atomic_json(run_dir / "inbox" / f"{stage['task_id']}.task.json", document)

    state = {
        "schema": "hutch.cao-state.v1",
        "run_id": run_dir.name,
        "workflow": workflow["name"],
        "status": "prepared",
        "created_at": now(),
        "cao_flow": workflow["name"],
        "cao_session": f"cao-flow-{workflow['name']}",
        "target_fingerprint": fingerprint,
        "target": str(target.resolve()),
        "campaign": workflow.get("campaign"),
        "stages": {
            stage["id"]: {
                "status": "pending",
                "task_id": stage["task_id"],
                "agent_profile": generated_profiles[stage["agent"]],
                "agent_cell": stage["agent"],
                "workspace": cells[stage["agent"]]["workspace"],
            }
            for stage in workflow["stages"]
        },
    }
    atomic_json(run_dir / "state.json", state)
    (run_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "ts": now(),
                "event": "run_prepared_by_cao_flow",
                "flow": workflow["name"],
                "session": f"cao-flow-{workflow['name']}",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema": "hutch.cao-run-manifest.v1",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "workflow": str(run_dir / "workflow.json"),
        "state_file": str(run_dir / "state.json"),
        "target": str(target.resolve()),
        "agent_cells": cells,
        "campaign": workflow.get("campaign"),
        "stages": [
            {
                "id": stage["id"],
                "task_id": stage["task_id"],
                "task_file": str(run_dir / "inbox" / f"{stage['task_id']}.task.json"),
                "profile": generated_profiles[stage["agent"]],
                "agent_cell": stage["agent"],
                "workspace": cells[stage["agent"]]["workspace"],
                "depends_on": stage.get("depends_on", []),
            }
            for stage in workflow["stages"]
        ],
    }
    atomic_json(run_dir / "manifest.json", manifest)
    return {
        "execute": True,
        "output": {
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "manifest": str(run_dir / "manifest.json"),
            "state_file": str(run_dir / "state.json"),
            "target": str(target.resolve()),
        },
    }


def prepare_for_flow_service(
    workflow_path: Path,
    profiles_dir: Path | None,
    flow_file: Path | None,
    launch_entry: bool,
) -> dict[str, Any]:
    prepared = prepare(workflow_path, profiles_dir)
    if not launch_entry:
        return prepared
    if flow_file is None:
        raise ValueError("--flow-file is required with --launch-entry")
    launcher = spawn_entry_launcher(flow_file, prepared)
    prepared["execute"] = False
    prepared["output"]["entry_launcher"] = launcher
    return prepared


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path, nargs="?")
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--flow-file", type=Path)
    parser.add_argument("--launch-entry", action="store_true")
    parser.add_argument("--launch-prepared", type=Path)
    args = parser.parse_args()
    try:
        if args.launch_prepared:
            print(
                json.dumps(
                    launch_entry_from_spec(args.launch_prepared),
                    ensure_ascii=False,
                )
            )
            return 0
        if args.workflow is None:
            raise ValueError("workflow is required")
        print(
            json.dumps(
                prepare_for_flow_service(
                    args.workflow,
                    args.profiles_dir,
                    args.flow_file,
                    args.launch_entry,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as error:
        print(f"prepare failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
