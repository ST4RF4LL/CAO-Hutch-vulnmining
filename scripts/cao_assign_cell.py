"""Launch one Hutch Agent Cell through CAO with a deterministic working directory."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class AssignError(RuntimeError):
    pass


READY_STATUSES = {"idle", "completed"}
CODEX_STARTUP_MARKERS = (
    "Starting MCP server",
    "Starting MCP servers",
)
CODEX_TRUST_MARKERS = (
    "allow codex to work in this folder",
    "do you trust the contents of this directory",
    "do you trust the files in this folder",
    "2. no, quit",
)


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


def _terminal_output(terminal_id: str) -> str:
    value = _request(
        "GET",
        f"/terminals/{terminal_id}/output",
        params={"mode": "last"},
    )
    return str(value.get("output", ""))


def _accept_codex_trust_prompt(terminal_id: str, provider: str, output: str) -> bool:
    normalized = output.lower()
    if provider != "codex" or not any(
        marker in normalized for marker in CODEX_TRUST_MARKERS
    ):
        return False
    _request(
        "POST",
        f"/terminals/{terminal_id}/key",
        params={"key": "Enter"},
    )
    return True


def _session_terminal_ids(session: str) -> set[str]:
    values = _request(
        "GET",
        f"/sessions/{urllib.parse.quote(session, safe='')}/terminals",
    )
    return {str(value["id"]) for value in values}


def _guard_codex_trust_prompt(
    session: str,
    existing_ids: set[str],
    profile: str,
    discovered_ids: set[str],
    stop: threading.Event,
    timeout: float,
) -> None:
    """Accept a worker's trust prompt while its blocking create request runs."""
    if timeout <= 0:
        return
    deadline = time.monotonic() + timeout
    session_path = urllib.parse.quote(session, safe="")
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            values = _request("GET", f"/sessions/{session_path}/terminals")
            for value in values:
                terminal_id = str(value["id"])
                if terminal_id in existing_ids or value.get("agent_profile") != profile:
                    continue
                discovered_ids.add(terminal_id)
                output = _terminal_output(terminal_id)
                if _accept_codex_trust_prompt(terminal_id, "codex", output):
                    return
        except (AssignError, KeyError, TypeError):
            pass
        stop.wait(0.25)


def _input_ready(provider: str, status: str, output: str) -> bool:
    if status not in READY_STATUSES:
        return False
    if provider == "codex" and any(marker in output for marker in CODEX_STARTUP_MARKERS):
        return False
    return True


def _wait_until_input_ready(
    terminal_id: str,
    provider: str,
    timeout: float,
) -> str:
    """Wait for a stable post-initialization prompt before pasting the task."""
    deadline = time.monotonic() + timeout
    ready_samples = 0
    status = "unknown"
    output = ""
    while time.monotonic() < deadline:
        status = str(_request("GET", f"/terminals/{terminal_id}").get("status", "unknown"))
        output = _terminal_output(terminal_id)
        if _accept_codex_trust_prompt(terminal_id, provider, output):
            ready_samples = 0
            time.sleep(0.5)
            continue
        if _input_ready(provider, status, output):
            ready_samples += 1
            if ready_samples >= 3:
                return status
        else:
            ready_samples = 0
        time.sleep(0.5)
    startup = next(
        (marker for marker in CODEX_STARTUP_MARKERS if marker in output),
        "none",
    )
    raise AssignError(
        f"CAO terminal {terminal_id} did not become input-ready; "
        f"last status={status}, startup_marker={startup}"
    )


def assign(task_path: Path, ready_timeout: float) -> dict[str, Any]:
    task, task_path, workspace, profile = _load_contract(task_path)
    supervisor_id = os.environ.get("CAO_TERMINAL_ID")
    if not supervisor_id:
        raise AssignError("CAO_TERMINAL_ID is required; run inside a CAO supervisor")

    supervisor = _request("GET", f"/terminals/{supervisor_id}")
    session = str(supervisor["session_name"])
    provider = str(task["agent_cell"].get("provider", "opencode_cli"))
    terminal: dict[str, Any] | None = None
    discovered_ids: set[str] = set()
    trust_stop = threading.Event()
    trust_thread: threading.Thread | None = None
    try:
        if provider == "codex":
            existing_ids = _session_terminal_ids(session)
            trust_thread = threading.Thread(
                target=_guard_codex_trust_prompt,
                args=(
                    session,
                    existing_ids,
                    profile,
                    discovered_ids,
                    trust_stop,
                    ready_timeout,
                ),
                daemon=True,
            )
            trust_thread.start()
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
        _wait_until_input_ready(terminal_id, provider, ready_timeout)

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
        cleanup_ids = set(discovered_ids)
        if terminal and terminal.get("id"):
            cleanup_ids.add(str(terminal["id"]))
        for terminal_id in cleanup_ids:
            try:
                _request("DELETE", f"/terminals/{terminal_id}")
            except Exception:
                pass
        raise
    finally:
        trust_stop.set()
        if trust_thread is not None:
            trust_thread.join(timeout=1.0)


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
