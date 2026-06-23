"""Dependency-free HTTP server for the Hutch run and CAO control dashboard."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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
        return {
            "terminal_id": terminal_id,
            "session": metadata.get("session_name"),
            "window": metadata.get("name"),
            "provider": metadata.get("provider"),
            "agent_profile": metadata.get("agent_profile"),
            "status": metadata.get("status"),
            "working_directory": cwd.get("working_directory"),
            "live": True,
            "output": output.get("output", ""),
        }

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

    def flow_action(self, name: str, action: str) -> dict[str, Any]:
        name = self._name(name, "flow name")
        endpoints = {
            "start": "run",
            "enable": "enable",
            "disable": "disable",
        }
        if action not in endpoints:
            raise CaoGatewayError("unsupported flow action", HTTPStatus.BAD_REQUEST)
        result = self._request(
            "POST",
            f"/flows/{urllib.parse.quote(name, safe='')}/{endpoints[action]}",
            timeout=180.0 if action == "start" else 30.0,
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
            result = self._request(
                "POST", f"/flows/{urllib.parse.quote(flow, safe='')}/run", timeout=180.0
            )
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
