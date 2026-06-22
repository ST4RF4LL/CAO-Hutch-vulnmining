"""Launch one Hutch Agent Cell through CAO with a deterministic working directory."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class AssignError(RuntimeError):
    pass


def _api_base() -> str:
    host = os.environ.get("CAO_API_HOST", "127.0.0.1")
    port = os.environ.get("CAO_API_PORT", "9889")
    return f"http://{host}:{port}"


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> Any:
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request = urllib.request.Request(
        _api_base() + path + query,
        data=b"" if method != "GET" else None,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise AssignError(f"CAO {method} {path} failed ({error.code}): {body}") from error
    except urllib.error.URLError as error:
        raise AssignError(f"CAO {method} {path} failed: {error}") from error


def _load_contract(task_path: Path) -> tuple[dict[str, Any], Path, Path, str]:
    task_path = task_path.expanduser().resolve()
    task = json.loads(task_path.read_text(encoding="utf-8"))
    if task.get("schema") != "hutch.task.v1":
        raise AssignError(f"unsupported task schema: {task.get('schema')!r}")

    run_dir = Path(task["run_directory"]).expanduser().resolve()
    if task_path.parent != run_dir / "inbox":
        raise AssignError(f"task is outside its declared run inbox: {task_path}")

    cell = task.get("agent_cell") or {}
    workspace = Path(cell["workspace"]).expanduser().resolve()
    expected_root = run_dir / "agents" / str(cell["id"])
    if workspace != expected_root / "workspace" or not workspace.is_dir():
        raise AssignError(f"invalid or absent Agent Cell workspace: {workspace}")

    profile = str(task["agent_profile"])
    if profile != cell.get("profile"):
        raise AssignError("task agent_profile does not match Agent Cell profile")
    return task, task_path, workspace, profile


def assign(task_path: Path, ready_timeout: float) -> dict[str, Any]:
    task, task_path, workspace, profile = _load_contract(task_path)
    supervisor_id = os.environ.get("CAO_TERMINAL_ID")
    if not supervisor_id:
        raise AssignError("CAO_TERMINAL_ID is required; run inside a CAO supervisor")

    supervisor = _request("GET", f"/terminals/{supervisor_id}")
    session = str(supervisor["session_name"])
    provider = str(task["agent_cell"].get("provider", "opencode_cli"))
    terminal: dict[str, Any] | None = None
    try:
        terminal = _request(
            "POST",
            f"/sessions/{urllib.parse.quote(session, safe='')}/terminals",
            params={
                "provider": provider,
                "agent_profile": profile,
                "working_directory": str(workspace),
                "caller_id": supervisor_id,
            },
            timeout=max(ready_timeout, 30.0),
        )
        terminal_id = str(terminal["id"])
        deadline = time.monotonic() + ready_timeout
        status = "unknown"
        while time.monotonic() < deadline:
            status = str(_request("GET", f"/terminals/{terminal_id}").get("status", "unknown"))
            if status in {"idle", "completed"}:
                break
            time.sleep(0.5)
        else:
            raise AssignError(
                f"CAO terminal {terminal_id} did not become ready; last status={status}"
            )

        message = (
            f"Execute task {task_path}. Read the JSON contract exactly. "
            "Write the requested Markdown artifact, then write the result JSON last. "
            "The source snapshot is immutable."
        )
        sent = _request(
            "POST",
            f"/terminals/{terminal_id}/input",
            params={
                "message": message,
                "sender_id": supervisor_id,
                "orchestration_type": "assign",
            },
        )
        if not sent.get("success"):
            raise AssignError(f"CAO rejected task input for terminal {terminal_id}")
        return {
            "ok": True,
            "terminal_id": terminal_id,
            "session": session,
            "profile": profile,
            "working_directory": str(workspace),
        }
    except Exception:
        if terminal and terminal.get("id"):
            try:
                _request("DELETE", f"/terminals/{terminal['id']}")
            except Exception:
                pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("--ready-timeout", type=float, default=120.0)
    args = parser.parse_args()
    try:
        print(json.dumps(assign(args.task, args.ready_timeout), ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
