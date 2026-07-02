"""Dependency-free HTTP server for the Hutch run and CAO control dashboard."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import shlex
import signal
import socket
import ssl
import struct
import subprocess
import termios
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
from .stores import (
    AgentStoreEditError,
    list_agent_store,
    list_flow_store,
    update_agent_instructions,
)


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
QU_AGENT_SESSION = "hutch-qu-agent"
QU_AGENT_WINDOW = "codex"
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
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


def _websocket_accept(handler: SimpleHTTPRequestHandler) -> bool:
    key = handler.headers.get("Sec-WebSocket-Key")
    upgrade = handler.headers.get("Upgrade", "").lower()
    connection = handler.headers.get("Connection", "").lower()
    if not key or upgrade != "websocket" or "upgrade" not in connection:
        handler.send_error(HTTPStatus.BAD_REQUEST, "invalid websocket handshake")
        return False
    accept = base64.b64encode(
        hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    ).decode("ascii")
    handler.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    handler.close_connection = True
    return True


def _ws_send_frame(
    sock: socket.socket,
    payload: bytes,
    opcode: int = 2,
    masked: bool = False,
) -> None:
    payload = bytes(payload)
    header = bytearray([0x80 | opcode])
    length = len(payload)
    mask_bit = 0x80 if masked else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.extend([mask_bit | 126, (length >> 8) & 0xFF, length & 0xFF])
    else:
        header.append(mask_bit | 127)
        header.extend(length.to_bytes(8, "big"))
    if masked:
        mask = os.urandom(4)
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        sock.sendall(bytes(header) + mask + payload)
        return
    sock.sendall(bytes(header) + payload)


def _ws_recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("websocket closed")
        data.extend(chunk)
    return bytes(data)


def _ws_recv_frame(sock: socket.socket) -> tuple[int, bytes]:
    header = _ws_recv_exact(sock, 2)
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    masked = bool(header[1] & 0x80)
    if length == 126:
        length = int.from_bytes(_ws_recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(_ws_recv_exact(sock, 8), "big")
    mask = _ws_recv_exact(sock, 4) if masked else b""
    payload = bytearray(_ws_recv_exact(sock, length)) if length else bytearray()
    if masked:
        for index, value in enumerate(payload):
            payload[index] = value ^ mask[index % 4]
    return opcode, bytes(payload)


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

    def attach_terminal_websocket(
        self, handler: SimpleHTTPRequestHandler, terminal_id: str
    ) -> None:
        terminal_id = self._terminal_id(terminal_id)
        try:
            cao_sock = self._connect_terminal_websocket(terminal_id)
        except CaoGatewayError as error:
            handler.send_error(error.status, str(error))
            return
        if not _websocket_accept(handler):
            cao_sock.close()
            return
        try:
            self._proxy_terminal_websocket(handler.connection, cao_sock)
        finally:
            try:
                cao_sock.close()
            except OSError:
                pass

    def _connect_terminal_websocket(self, terminal_id: str) -> socket.socket:
        parsed = urllib.parse.urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if scheme == "wss" else 80)
        path = f"/terminals/{urllib.parse.quote(terminal_id, safe='')}/ws"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        try:
            raw = socket.create_connection((host, port), timeout=10.0)
            sock = (
                ssl.create_default_context().wrap_socket(raw, server_hostname=host)
                if scheme == "wss"
                else raw
            )
            sock.settimeout(None)
            host_header = f"{host}:{port}" if parsed.port else host
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("CAO closed websocket handshake")
                response.extend(chunk)
                if len(response) > 65536:
                    raise ConnectionError("CAO websocket handshake is too large")
            status_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            if " 101 " not in f" {status_line} ":
                raise ConnectionError(status_line)
            return sock
        except OSError as error:
            raise CaoGatewayError(f"CAO terminal websocket unavailable: {error}") from error
        except ConnectionError as error:
            raise CaoGatewayError(f"CAO terminal websocket failed: {error}") from error

    @staticmethod
    def _proxy_terminal_websocket(
        browser_sock: socket.socket, cao_sock: socket.socket
    ) -> None:
        try:
            while True:
                readable, _, _ = select.select([browser_sock, cao_sock], [], [], 0.25)
                if browser_sock in readable:
                    opcode, payload = _ws_recv_frame(browser_sock)
                    if opcode == 8:
                        _ws_send_frame(cao_sock, payload, opcode=8, masked=True)
                        break
                    _ws_send_frame(cao_sock, payload, opcode=opcode, masked=True)
                if cao_sock in readable:
                    opcode, payload = _ws_recv_frame(cao_sock)
                    if opcode == 8:
                        _ws_send_frame(browser_sock, payload, opcode=8)
                        break
                    _ws_send_frame(browser_sock, payload, opcode=opcode)
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                _ws_send_frame(browser_sock, b"", opcode=8)
            except OSError:
                pass

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


class QuAgentError(CaoGatewayError):
    """Local QU tmux terminal errors surfaced through dashboard JSON routes."""


class QuAgentTerminal:
    """Hutch-owned tmux terminal running codex in the Hutch workspace."""

    def __init__(
        self,
        workspace: Path | None = None,
        session_name: str = QU_AGENT_SESSION,
        window_name: str = QU_AGENT_WINDOW,
        command: str | None = None,
    ) -> None:
        self.workspace = (workspace or ROOT).resolve()
        self.session_name = self._safe_name(session_name, "session name")
        self.window_name = self._safe_name(window_name, "window name")
        self.command = command or os.environ.get("HUTCH_QU_AGENT_COMMAND", "codex")

    @staticmethod
    def _safe_name(value: str, label: str) -> str:
        if not SAFE_NAME.fullmatch(value):
            raise QuAgentError(f"invalid QU agent {label}: {value!r}", HTTPStatus.BAD_REQUEST)
        return value

    @property
    def target(self) -> str:
        return f"{self.session_name}:{self.window_name}"

    def _run(self, args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                args,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as error:
            raise QuAgentError("tmux is not installed or not on PATH") from error
        except subprocess.TimeoutExpired as error:
            raise QuAgentError(f"tmux command timed out: {' '.join(args)}") from error

    def _has_session(self) -> bool:
        return self._run(["tmux", "has-session", "-t", self.session_name]).returncode == 0

    def is_live(self) -> bool:
        return self._run(["tmux", "has-session", "-t", self.target]).returncode == 0

    def status(self) -> dict[str, Any]:
        live = self.is_live()
        return {
            "ok": True,
            "live": live,
            "terminal_id": self.session_name,
            "session": self.session_name,
            "window": self.window_name,
            "command": self.command,
            "working_directory": str(self.workspace),
            "websocket_path": "/api/qu-agent/ws" if live else None,
        }

    def start(self) -> dict[str, Any]:
        if not self.workspace.is_dir():
            raise QuAgentError(
                f"working directory does not exist: {self.workspace}",
                HTTPStatus.BAD_REQUEST,
            )
        if self.is_live():
            value = self.status()
            value["already_running"] = True
            return value

        if self._has_session():
            args = [
                "tmux",
                "new-window",
                "-d",
                "-t",
                self.session_name,
                "-n",
                self.window_name,
                "-c",
                str(self.workspace),
                self.command,
            ]
        else:
            args = [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.session_name,
                "-n",
                self.window_name,
                "-c",
                str(self.workspace),
                self.command,
            ]
        result = self._run(args, timeout=30.0)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown tmux error").strip()
            raise QuAgentError(f"failed to start QU agent terminal: {detail}")
        value = self.status()
        value["started"] = True
        return value

    def stop(self) -> dict[str, Any]:
        if not self._has_session():
            value = self.status()
            value["already_stopped"] = True
            return value
        result = self._run(["tmux", "kill-session", "-t", self.session_name])
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown tmux error").strip()
            raise QuAgentError(f"failed to stop QU agent terminal: {detail}")
        value = self.status()
        value["stopped"] = True
        return value

    def attach_websocket(self, handler: SimpleHTTPRequestHandler) -> None:
        if not self.is_live():
            handler.send_error(HTTPStatus.NOT_FOUND, "QU agent terminal is not running")
            return
        key = handler.headers.get("Sec-WebSocket-Key")
        upgrade = handler.headers.get("Upgrade", "").lower()
        connection = handler.headers.get("Connection", "").lower()
        if not key or upgrade != "websocket" or "upgrade" not in connection:
            handler.send_error(HTTPStatus.BAD_REQUEST, "invalid websocket handshake")
            return

        accept = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        handler.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept)
        handler.end_headers()
        handler.close_connection = True
        self._bridge_tmux(handler.connection)

    @staticmethod
    def _send_ws_frame(sock: socket.socket, payload: bytes, opcode: int = 2) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.extend([126, (length >> 8) & 0xFF, length & 0xFF])
        else:
            header.append(127)
            header.extend(length.to_bytes(8, "big"))
        sock.sendall(bytes(header) + payload)

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("websocket closed")
            data.extend(chunk)
        return bytes(data)

    @classmethod
    def _recv_ws_frame(cls, sock: socket.socket) -> tuple[int, bytes]:
        header = cls._recv_exact(sock, 2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        masked = bool(header[1] & 0x80)
        if length == 126:
            length = int.from_bytes(cls._recv_exact(sock, 2), "big")
        elif length == 127:
            length = int.from_bytes(cls._recv_exact(sock, 8), "big")
        mask = cls._recv_exact(sock, 4) if masked else b""
        payload = bytearray(cls._recv_exact(sock, length)) if length else bytearray()
        if masked:
            for index, value in enumerate(payload):
                payload[index] = value ^ mask[index % 4]
        return opcode, bytes(payload)

    @staticmethod
    def _set_terminal_size(fd: int, rows: int, cols: int) -> None:
        rows = min(max(int(rows), 1), 200)
        cols = min(max(int(cols), 2), 500)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    @staticmethod
    def _pty_env() -> dict[str, str]:
        env = os.environ.copy()
        if env.get("TERM", "dumb") == "dumb":
            env["TERM"] = "xterm-256color"
        return env

    def _bridge_tmux(self, sock: socket.socket) -> None:
        master_fd, slave_fd = pty.openpty()
        proc: subprocess.Popen[bytes] | None = None
        try:
            self._set_terminal_size(slave_fd, 24, 80)
            proc = subprocess.Popen(
                ["tmux", "-u", "attach-session", "-t", self.target],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=os.setsid,
                env=self._pty_env(),
            )
            os.close(slave_fd)
            slave_fd = -1
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            while True:
                readable, _, _ = select.select([sock, master_fd], [], [], 0.25)
                if master_fd in readable:
                    try:
                        data = os.read(master_fd, 65536)
                    except BlockingIOError:
                        data = b""
                    if data:
                        self._send_ws_frame(sock, data, opcode=2)
                    elif proc.poll() is not None:
                        break
                if sock in readable:
                    opcode, payload = self._recv_ws_frame(sock)
                    if opcode == 8:
                        break
                    if opcode == 9:
                        self._send_ws_frame(sock, payload, opcode=10)
                        continue
                    if opcode != 1:
                        continue
                    message = json.loads(payload.decode("utf-8"))
                    if message.get("type") == "input":
                        raw = str(message.get("data", "")).encode("utf-8")
                        for offset in range(0, len(raw), 1024):
                            os.write(master_fd, raw[offset : offset + 1024])
                    elif message.get("type") == "resize":
                        self._set_terminal_size(
                            master_fd,
                            int(message.get("rows", 24)),
                            int(message.get("cols", 80)),
                        )
                        if proc.poll() is None:
                            os.kill(proc.pid, signal.SIGWINCH)
                if proc.poll() is not None:
                    break
        except (ConnectionError, OSError, json.JSONDecodeError, ValueError):
            pass
        finally:
            try:
                self._send_ws_frame(sock, b"", opcode=8)
            except OSError:
                pass
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            if slave_fd >= 0:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            try:
                os.close(master_fd)
            except OSError:
                pass


def handler_factory(
    repository: RunRepository,
    cao: CaoGateway | None = None,
    qu_agent: QuAgentTerminal | None = None,
):
    gateway = cao or CaoGateway()
    qu_terminal = qu_agent or QuAgentTerminal()

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
            if parsed.path == "/api/stores/agents":
                self._json(list_agent_store())
                return
            if parsed.path == "/api/stores/flows":
                self._json(list_flow_store())
                return
            if parsed.path == "/api/cao/catalog":
                self._gateway(lambda: gateway.catalog())
                return
            if parsed.path == "/api/qu-agent/ws":
                qu_terminal.attach_websocket(self)
                return
            if parsed.path == "/api/qu-agent":
                self._gateway(lambda: qu_terminal.status())
                return
            if parsed.path.startswith("/api/terminals/") and parsed.path.endswith("/ws"):
                terminal_id = unquote(
                    parsed.path.removeprefix("/api/terminals/").removesuffix("/ws")
                )
                gateway.attach_terminal_websocket(self, terminal_id)
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
            if parsed.path.startswith("/api/stores/agents/") and parsed.path.endswith("/instructions"):
                role_id = unquote(
                    parsed.path.removeprefix("/api/stores/agents/").removesuffix("/instructions")
                )
                try:
                    self._json(update_agent_instructions(role_id, body.get("content")))
                except AgentStoreEditError as error:
                    self._json({"error": str(error)}, error.status)
                return
            if parsed.path == "/api/cao/execute":
                self._gateway(lambda: gateway.execute(body.get("command")))
                return
            if parsed.path == "/api/qu-agent/start":
                self._gateway(lambda: qu_terminal.start())
                return
            if parsed.path == "/api/qu-agent/stop":
                self._gateway(lambda: qu_terminal.stop())
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
