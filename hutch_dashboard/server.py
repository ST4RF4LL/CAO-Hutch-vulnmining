"""Dependency-free HTTP server for the Hutch run dashboard."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .model import RunNotFound, RunRepository


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"


def handler_factory(repository: RunRepository):
    class DashboardHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self._json({"status": "ok", "service": "hutch-dashboard"})
                return
            if parsed.path == "/api/runs":
                self._json(repository.list_runs(status="completed"))
                return
            if parsed.path.startswith("/api/runs/"):
                run_id = unquote(parsed.path.removeprefix("/api/runs/"))
                try:
                    self._json(repository.get_run(run_id))
                except RunNotFound:
                    self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
                return
            super().do_GET()

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
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "runs")
    parser.add_argument("--cao-home", type=Path, default=Path.home() / ".aws" / "cli-agent-orchestrator")
    args = parser.parse_args()
    repository = RunRepository(
        args.runs_dir,
        args.cao_home / "logs" / "terminal",
        args.cao_home / "db" / "cli-agent-orchestrator.db",
    )
    server = ThreadingHTTPServer((args.host, args.port), handler_factory(repository))
    print(f"Hutch Dashboard: http://{args.host}:{args.port}", flush=True)
    print(f"Runs: {args.runs_dir.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
