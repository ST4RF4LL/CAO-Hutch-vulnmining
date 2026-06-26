"""Dependency-free HTTP server for the Hutch run and CAO control dashboard."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from scripts.hutch_paths import hutch_projects_file, hutch_runs_dir
from .model import (
    CampaignNotFound,
    ProjectNotFound,
    RunDeleteConflict,
    RunNotFound,
    RunRepository,
)


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CODEX_TRUST_MARKERS = (
    "allow codex to work in this folder",
    "do you trust the contents of this directory",
    "do you trust the files in this folder",
    "2. no, quit",
)
CODEX_ACTIVE_PROGRESS_RE = re.compile(
    r"\((?:(?:\d+h\s+)?(?:\d+m\s+)?)\d+s\s*[•·]\s*esc to interrupt\)",
    re.IGNORECASE,
)
CODEX_BACKGROUND_RUNNING_RE = re.compile(
    r"\b\d+\s+background terminals?\s+running\b",
    re.IGNORECASE,
)
ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)
TEMPLATE_VAR_RE = re.compile(r"\[\[(\w+)\]\]")


class CaoGatewayError(RuntimeError):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_GATEWAY):
        super().__init__(message)
        self.status = status


class CaoGateway:
    """Narrow localhost gateway over CAO's tmux-backed terminal API."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or self._default_url()).rstrip("/")

    @staticmethod
    def _default_url() -> str:
        host = os.environ.get("CAO_API_HOST", "127.0.0.1")
        port = os.environ.get("CAO_API_PORT", "9889")
        return f"http://{host}:{port}"

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        query = "?" + urllib.parse.urlencode(params) if params else ""
        request = urllib.request.Request(
            self.base_url + path + query,
            data=b"" if method != "GET" else None,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                return json.loads(body.decode("utf-8")) if body else {}
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            status = HTTPStatus.NOT_FOUND if error.code == 404 else HTTPStatus.BAD_GATEWAY
            raise CaoGatewayError(
                f"CAO {method} {path} failed ({error.code}): {body}", status
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise CaoGatewayError(f"CAO is unavailable: {error}") from error

    @staticmethod
    def _terminal_id(value: str) -> str:
        if not SAFE_NAME.fullmatch(value):
            raise CaoGatewayError("invalid terminal id", HTTPStatus.BAD_REQUEST)
        return value

    def terminal(self, terminal_id: str) -> dict[str, Any]:
        terminal_id = self._terminal_id(terminal_id)
        metadata = self._request("GET", f"/terminals/{terminal_id}")
        output = self._request(
            "GET", f"/terminals/{terminal_id}/output", {"mode": "full"}
        )
        try:
            cwd = self._request("GET", f"/terminals/{terminal_id}/working-directory")
        except CaoGatewayError:
            cwd = {}
        raw_status = metadata.get("status")
        output_text = str(output.get("output", ""))
        status = self._effective_terminal_status(
            metadata.get("provider"), raw_status, output_text
        )
        return {
            "terminal_id": terminal_id,
            "session": metadata.get("session_name"),
            "window": metadata.get("name"),
            "provider": metadata.get("provider"),
            "agent_profile": metadata.get("agent_profile"),
            "status": status,
            "raw_status": raw_status,
            "working_directory": cwd.get("working_directory"),
            "live": True,
            "output": output_text,
        }

    @staticmethod
    def _effective_terminal_status(
        provider: Any, raw_status: Any, output: str
    ) -> Any:
        """Correct known Codex TUI false-completed frames for Hutch consumers."""
        if provider != "codex" or raw_status != "completed":
            return raw_status
        clean = ANSI_ESCAPE_RE.sub("", output)
        tail = "\n".join(clean.splitlines()[-40:])
        if CODEX_ACTIVE_PROGRESS_RE.search(tail):
            return "processing"
        if CODEX_BACKGROUND_RUNNING_RE.search(tail):
            return "processing"
        return raw_status

    def send_input(self, terminal_id: str, message: str) -> dict[str, Any]:
        terminal_id = self._terminal_id(terminal_id)
        if not isinstance(message, str) or not message or len(message) > 16_000:
            raise CaoGatewayError("input must contain 1-16000 characters", HTTPStatus.BAD_REQUEST)
        return self._request(
            "POST", f"/terminals/{terminal_id}/input", {"message": message}
        )

    def send_key(self, terminal_id: str, key: str) -> dict[str, Any]:
        terminal_id = self._terminal_id(terminal_id)
        allowed = {"Enter", "Escape", "Up", "Down", "Left", "Right", "C-c", "C-d"}
        if key not in allowed:
            raise CaoGatewayError("unsupported terminal key", HTTPStatus.BAD_REQUEST)
        return self._request("POST", f"/terminals/{terminal_id}/key", {"key": key})

    def catalog(self) -> dict[str, Any]:
        return {
            "flows": self._request("GET", "/flows"),
            "profiles": self._request("GET", "/agents/profiles"),
            "providers": self._request("GET", "/agents/providers"),
        }

    def active_sessions(self) -> set[str]:
        value = self._request("GET", "/sessions")
        records = value.get("sessions", []) if isinstance(value, dict) else value
        if not isinstance(records, list):
            raise CaoGatewayError("CAO sessions response is not a list")
        sessions: set[str] = set()
        for item in records:
            if isinstance(item, str):
                sessions.add(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("session_name")
                if isinstance(name, str) and name:
                    sessions.add(name)
        return sessions

    @staticmethod
    def _session_terminal_records(value: Any) -> list[dict[str, Any]]:
        records = value.get("terminals", []) if isinstance(value, dict) else value
        return records if isinstance(records, list) else []

    def _session_terminal_ids(self, session_name: str) -> set[str]:
        try:
            value = self._request(
                "GET",
                f"/sessions/{urllib.parse.quote(session_name, safe='')}/terminals",
            )
        except CaoGatewayError as error:
            if error.status == HTTPStatus.NOT_FOUND:
                return set()
            raise
        return {
            str(record["id"])
            for record in self._session_terminal_records(value)
            if isinstance(record, dict) and record.get("id")
        }

    def _accept_codex_trust_prompt(self, terminal_id: str) -> bool:
        metadata = self._request("GET", f"/terminals/{terminal_id}")
        if metadata.get("provider") != "codex":
            return False
        output = self._request(
            "GET",
            f"/terminals/{terminal_id}/output",
            {"mode": "last"},
        )
        text = str(output.get("output", "")).lower()
        if not any(marker in text for marker in CODEX_TRUST_MARKERS):
            return False
        self._request("POST", f"/terminals/{terminal_id}/key", {"key": "Enter"})
        return True

    def _guard_codex_flow_trust_prompt(
        self,
        session_name: str,
        existing_ids: set[str],
        stop: threading.Event,
        timeout: float = 90.0,
    ) -> None:
        """Accept the conductor Codex trust prompt while CAO starts a Flow."""
        deadline = time.monotonic() + timeout
        session_path = urllib.parse.quote(session_name, safe="")
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                value = self._request("GET", f"/sessions/{session_path}/terminals")
                for record in self._session_terminal_records(value):
                    if not isinstance(record, dict) or not record.get("id"):
                        continue
                    terminal_id = str(record["id"])
                    if terminal_id in existing_ids:
                        continue
                    if self._accept_codex_trust_prompt(terminal_id):
                        return
            except CaoGatewayError:
                pass
            stop.wait(0.25)

    @staticmethod
    def _parse_flow_file(path: Path) -> tuple[dict[str, str], str]:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return {}, text
        try:
            raw_metadata, body = text.split("---", 2)[1:]
        except ValueError as error:
            raise CaoGatewayError(f"invalid Flow frontmatter: {path}") from error
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

    @staticmethod
    def _render_template(template: str, values: dict[str, Any]) -> str:
        missing = sorted(set(TEMPLATE_VAR_RE.findall(template)) - set(values))
        if missing:
            raise CaoGatewayError(
                f"Flow script output is missing template variables: {missing}"
            )

        def replace(match: re.Match[str]) -> str:
            return str(values[match.group(1)])

        return TEMPLATE_VAR_RE.sub(replace, template)

    def _start_hutch_native_flow(self, name: str, flow: dict[str, Any]) -> Any | None:
        if not flow.get("file_path"):
            return None
        file_path = Path(str(flow.get("file_path", ""))).expanduser()
        if not file_path.is_file():
            return None
        metadata, prompt_template = self._parse_flow_file(file_path)
        working_directory = metadata.get("working_directory")
        if not working_directory:
            return None
        workspace = Path(working_directory).expanduser().resolve()
        if not workspace.is_dir():
            raise CaoGatewayError(
                f"Flow working directory does not exist: {workspace}",
                HTTPStatus.BAD_REQUEST,
            )
        script = str(flow.get("script") or metadata.get("script") or "")
        output = {"execute": True, "output": {}}
        if script:
            script_path = Path(script)
            if not script_path.is_absolute():
                script_path = file_path.parent / script_path
            if not script_path.is_file():
                raise CaoGatewayError(
                    f"Flow prepare script does not exist: {script_path}",
                    HTTPStatus.BAD_REQUEST,
                )
            try:
                result = subprocess.run(
                    [str(script_path)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=600,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise CaoGatewayError(
                    f"Flow prepare script timed out: {script_path}",
                    HTTPStatus.GATEWAY_TIMEOUT,
                ) from error
            if result.returncode:
                raise CaoGatewayError(
                    f"Flow prepare script failed ({result.returncode}): {result.stderr.strip()}",
                    HTTPStatus.BAD_GATEWAY,
                )
            try:
                output = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise CaoGatewayError(
                    f"Flow prepare script returned invalid JSON: {error}",
                    HTTPStatus.BAD_GATEWAY,
                ) from error
        if not output.get("execute"):
            return {"executed": False}
        payload = output.get("output")
        if not isinstance(payload, dict):
            raise CaoGatewayError("Flow script output must be an object")
        prompt = self._render_template(prompt_template, payload)
        session_name = f"cao-flow-{name}"
        existing_ids = self._session_terminal_ids(session_name)
        if existing_ids:
            try:
                self._request(
                    "DELETE",
                    f"/sessions/{urllib.parse.quote(session_name, safe='')}",
                    timeout=60.0,
                )
            except CaoGatewayError as error:
                if error.status != HTTPStatus.NOT_FOUND:
                    raise
            existing_ids = set()
        stop = threading.Event()
        guard = threading.Thread(
            target=self._guard_codex_flow_trust_prompt,
            args=(session_name, existing_ids, stop),
            daemon=True,
        )
        guard.start()
        try:
            terminal = self._request(
                "POST",
                "/sessions",
                {
                    "provider": str(flow.get("provider") or metadata.get("provider") or "opencode_cli"),
                    "agent_profile": str(flow.get("agent_profile") or metadata.get("agent_profile")),
                    "session_name": session_name.removeprefix("cao-"),
                    "working_directory": str(workspace),
                },
                timeout=180.0,
            )
        finally:
            stop.set()
            guard.join(timeout=1.0)
        terminal_id = str(terminal.get("id", ""))
        if not terminal_id:
            raise CaoGatewayError("CAO did not return a conductor terminal id")
        self._request(
            "POST",
            f"/terminals/{urllib.parse.quote(terminal_id, safe='')}/input",
            {"message": prompt},
            timeout=30.0,
        )
        return {"executed": True, "terminal": terminal, "working_directory": str(workspace)}

    def _start_flow(self, name: str) -> Any:
        try:
            flow = self._request("GET", f"/flows/{urllib.parse.quote(name, safe='')}")
            result = self._start_hutch_native_flow(name, flow)
            if result is not None:
                return result
        except CaoGatewayError:
            raise
        session_name = f"cao-flow-{name}"
        existing_ids = self._session_terminal_ids(session_name)
        stop = threading.Event()
        guard = threading.Thread(
            target=self._guard_codex_flow_trust_prompt,
            args=(session_name, existing_ids, stop),
            daemon=True,
        )
        guard.start()
        try:
            return self._request(
                "POST",
                f"/flows/{urllib.parse.quote(name, safe='')}/run",
                timeout=180.0,
            )
        finally:
            stop.set()
            guard.join(timeout=1.0)

    def flow_action(self, name: str, action: str) -> dict[str, Any]:
        name = self._name(name, "flow name")
        endpoints = {
            "start": "run",
            "enable": "enable",
            "disable": "disable",
        }
        if action not in endpoints:
            raise CaoGatewayError("unsupported flow action", HTTPStatus.BAD_REQUEST)
        result = (
            self._start_flow(name)
            if action == "start"
            else self._request(
                "POST",
                f"/flows/{urllib.parse.quote(name, safe='')}/{endpoints[action]}",
            )
        )
        return {"ok": True, "flow": name, "action": action, "result": result}

    def stop_session(self, session_name: str) -> dict[str, Any]:
        session_name = self._name(session_name, "session name")
        try:
            result = self._request(
                "DELETE", f"/sessions/{urllib.parse.quote(session_name, safe='')}"
            )
            return {"ok": True, "session": session_name, "result": result}
        except CaoGatewayError as error:
            if error.status == HTTPStatus.NOT_FOUND:
                return {"ok": True, "session": session_name, "already_stopped": True}
            raise

    @staticmethod
    def _name(value: str, label: str) -> str:
        if not SAFE_NAME.fullmatch(value):
            raise CaoGatewayError(f"invalid {label}: {value!r}", HTTPStatus.BAD_REQUEST)
        return value

    def execute(self, command: str) -> dict[str, Any]:
        if not isinstance(command, str) or len(command) > 4096:
            raise CaoGatewayError("command is too long", HTTPStatus.BAD_REQUEST)
        try:
            parts = shlex.split(command)
        except ValueError as error:
            raise CaoGatewayError(f"invalid command: {error}", HTTPStatus.BAD_REQUEST) from error
        if parts[:3] == ["cao", "flow", "run"] and len(parts) == 4:
            flow = self._name(parts[3], "flow name")
            result = self._start_flow(flow)
            return {"ok": True, "command": command, "kind": "flow", "result": result}
        if parts[:2] == ["cao", "launch"] and len(parts) >= 3:
            profile = self._name(parts[2], "agent profile")
            options: dict[str, str] = {"agent_profile": profile}
            supported = {
                "--provider": "provider",
                "--session": "session_name",
                "--working-directory": "working_directory",
            }
            index = 3
            while index < len(parts):
                flag = parts[index]
                if flag not in supported or index + 1 >= len(parts):
                    raise CaoGatewayError(
                        f"unsupported or incomplete launch option: {flag}",
                        HTTPStatus.BAD_REQUEST,
                    )
                value = parts[index + 1]
                options[supported[flag]] = value
                index += 2
            options["provider"] = self._name(
                options.get("provider", "opencode_cli"), "provider"
            )
            if "session_name" in options:
                self._name(options["session_name"], "session name")
            if "working_directory" in options:
                path = Path(options["working_directory"]).expanduser().resolve()
                if not path.is_dir():
                    raise CaoGatewayError(
                        f"working directory does not exist: {path}", HTTPStatus.BAD_REQUEST
                    )
                options["working_directory"] = str(path)
            result = self._request("POST", "/sessions", options, timeout=180.0)
            return {"ok": True, "command": command, "kind": "agent", "result": result}
        raise CaoGatewayError(
            "only `cao flow run <flow>` and `cao launch <profile> [--provider ...] "
            "[--session ...] [--working-directory ...]` are supported",
            HTTPStatus.BAD_REQUEST,
        )


def handler_factory(repository: RunRepository, cao: CaoGateway | None = None):
    gateway = cao or CaoGateway()

    def active_sessions() -> set[str] | None:
        try:
            return gateway.active_sessions()
        except (CaoGatewayError, AttributeError):
            return None

    class DashboardHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self._json({"status": "ok", "service": "hutch-dashboard"})
                return
            if parsed.path == "/api/runs":
                self._json(repository.list_runs(active_sessions=active_sessions()))
                return
            if parsed.path == "/api/projects":
                self._json(repository.list_projects(active_sessions=active_sessions()))
                return
            if parsed.path == "/api/campaigns":
                self._json(repository.list_campaigns(active_sessions=active_sessions()))
                return
            if parsed.path == "/api/cao/catalog":
                self._gateway(lambda: gateway.catalog())
                return
            if parsed.path.startswith("/api/terminals/"):
                terminal_id = unquote(parsed.path.removeprefix("/api/terminals/"))
                try:
                    self._json(gateway.terminal(terminal_id))
                except CaoGatewayError as error:
                    snapshot = repository.get_terminal_snapshot(terminal_id)
                    if snapshot:
                        self._json(snapshot)
                    else:
                        self._json({"error": str(error)}, error.status)
                return
            if parsed.path.startswith("/api/projects/"):
                project_id = unquote(parsed.path.removeprefix("/api/projects/"))
                try:
                    self._json(
                        repository.get_project(
                            project_id, active_sessions=active_sessions()
                        )
                    )
                except ProjectNotFound:
                    self._json({"error": "project not found"}, HTTPStatus.NOT_FOUND)
                return
            if parsed.path.startswith("/api/campaigns/"):
                instance_id = unquote(parsed.path.removeprefix("/api/campaigns/"))
                try:
                    self._json(
                        repository.get_campaign(
                            instance_id, active_sessions=active_sessions()
                        )
                    )
                except CampaignNotFound:
                    self._json({"error": "campaign not found"}, HTTPStatus.NOT_FOUND)
                return
            if parsed.path.startswith("/api/runs/"):
                run_id = unquote(parsed.path.removeprefix("/api/runs/"))
                try:
                    self._json(repository.get_run(run_id, active_sessions=active_sessions()))
                except RunNotFound:
                    self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
                return
            super().do_GET()

        def end_headers(self) -> None:
            if not self.path.startswith("/api/"):
                self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                body = self._body()
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/cao/execute":
                self._gateway(lambda: gateway.execute(body.get("command")))
                return
            if parsed.path == "/api/projects/open":
                try:
                    project = repository.open_project(
                        str(body.get("path", "")),
                        body.get("name"),
                        body.get("id"),
                    )
                    self._json(project, HTTPStatus.CREATED)
                except ValueError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path.startswith("/api/flows/"):
                remainder = parsed.path.removeprefix("/api/flows/")
                flow_value, separator, action = remainder.rpartition("/")
                if separator and action in {"start", "enable", "disable"}:
                    flow_name = unquote(flow_value)
                    before = {
                        run["run_id"] for run in repository.list_runs()
                    } if action == "start" else set()
                    try:
                        result = gateway.flow_action(flow_name, action)
                        if action == "start":
                            created = [
                                run
                                for run in repository.list_runs()
                                if run["run_id"] not in before
                                and run.get("workflow") == flow_name
                            ]
                            if created:
                                created.sort(
                                    key=lambda run: run.get("created_at") or "",
                                    reverse=True,
                                )
                                result["run"] = created[0]
                        self._json(result)
                    except CaoGatewayError as error:
                        self._json({"error": str(error)}, error.status)
                    return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/stop"):
                run_id = unquote(
                    parsed.path.removeprefix("/api/runs/").removesuffix("/stop")
                )
                try:
                    detail = repository.get_run(
                        run_id, active_sessions=active_sessions()
                    )
                    if detail["raw_status"] not in {"prepared", "launching", "running"}:
                        raise RunDeleteConflict(
                            f"flow {run_id} cannot be stopped from {detail['raw_status']}"
                        )
                    session = detail.get("cao_session")
                    cao_result = (
                        gateway.stop_session(session)
                        if session
                        else {"ok": True, "session": None, "already_stopped": True}
                    )
                    result = repository.stop_run(run_id)
                    result["cao"] = cao_result
                    self._json(result)
                except RunNotFound:
                    self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
                except RunDeleteConflict as error:
                    self._json({"error": str(error)}, HTTPStatus.CONFLICT)
                except CaoGatewayError as error:
                    self._json({"error": str(error)}, error.status)
                return
            if parsed.path.startswith("/api/terminals/"):
                remainder = parsed.path.removeprefix("/api/terminals/")
                terminal_value, separator, action = remainder.partition("/")
                terminal_id = unquote(terminal_value)
                if separator and action == "input":
                    self._gateway(lambda: gateway.send_input(terminal_id, body.get("message")))
                    return
                if separator and action == "key":
                    self._gateway(lambda: gateway.send_key(terminal_id, body.get("key")))
                    return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/runs/"):
                run_id = unquote(parsed.path.removeprefix("/api/runs/"))
                try:
                    self._json(
                        repository.delete_run(
                            run_id, active_sessions=active_sessions()
                        )
                    )
                except RunNotFound:
                    self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
                except RunDeleteConflict as error:
                    self._json({"error": str(error)}, HTTPStatus.CONFLICT)
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def _gateway(self, action) -> None:
            try:
                self._json(action())
            except CaoGatewayError as error:
                self._json({"error": str(error)}, error.status)

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid content length") from error
            if length <= 0 or length > 64 * 1024:
                raise ValueError("JSON body must contain 1-65536 bytes")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("invalid JSON body") from error
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value

        def _json(self, value, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, message: str, *args) -> None:
            print(f"{self.address_string()} - {message % args}")

    return DashboardHandler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9890)
    parser.add_argument("--runs-dir", type=Path, default=hutch_runs_dir())
    parser.add_argument(
        "--projects-file",
        type=Path,
        default=hutch_projects_file(),
        help="JSON registry of non-Git application project roots",
    )
    parser.add_argument(
        "--cao-home", type=Path, default=Path.home() / ".aws" / "cli-agent-orchestrator"
    )
    parser.add_argument("--cao-url", default=None)
    args = parser.parse_args()
    repository = RunRepository(
        args.runs_dir,
        args.cao_home / "logs" / "terminal",
        args.cao_home / "db" / "cli-agent-orchestrator.db",
        args.projects_file,
    )
    server = ThreadingHTTPServer(
        (args.host, args.port), handler_factory(repository, CaoGateway(args.cao_url))
    )
    print(f"Hutch Dashboard: http://{args.host}:{args.port}", flush=True)
    print(f"Runs: {args.runs_dir.resolve()}", flush=True)
    print(f"Projects: {args.projects_file.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
