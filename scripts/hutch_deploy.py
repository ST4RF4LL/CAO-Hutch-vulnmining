#!/usr/bin/env python3
"""Quick local deployment helper for the Hutch + CAO stack.

The script manages only local runtime state below ``~/.hutch`` by default. It
does not patch CAO, does not modify audited source repositories, and does not
force Codex or OpenCode as a global default provider.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from hutch_paths import (
    default_cao_repo,
    hutch_generated_dir,
    hutch_home,
    hutch_projects_file,
    hutch_runs_dir,
    hutch_workflows_dir,
)


ROOT = Path(__file__).resolve().parents[1]
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class DeployError(RuntimeError):
    pass


def safe_name(value: str, maximum: int = 48) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower()
    if not name:
        name = "hutch-flow"
    return name[:maximum].rstrip("-")


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def configure_environment(args: argparse.Namespace) -> None:
    if getattr(args, "hutch_home", None):
        os.environ["HUTCH_HOME"] = str(Path(args.hutch_home).expanduser().resolve())
    if getattr(args, "cao_repo", None):
        os.environ["CAO_REPO"] = str(Path(args.cao_repo).expanduser().resolve())
    os.environ["CAO_API_HOST"] = args.cao_host
    os.environ["CAO_API_PORT"] = str(args.cao_port)
    os.environ["HUTCH_URL"] = hutch_url(args)


def runtime_paths() -> dict[str, Path]:
    home = hutch_home()
    return {
        "home": home,
        "logs": home / "logs",
        "pids": home / "pids",
        "runs": hutch_runs_dir(),
        "generated": hutch_generated_dir(),
        "workflows": hutch_workflows_dir(),
        "projects_file": hutch_projects_file(),
        "env": home / "env",
        "cao_pid": home / "pids" / "cao-server.pid",
        "dashboard_pid": home / "pids" / "hutch-dashboard.pid",
        "cao_log": home / "logs" / "cao-server.log",
        "dashboard_log": home / "logs" / "hutch-dashboard.log",
    }


def cao_url(args: argparse.Namespace) -> str:
    return f"http://{args.cao_host}:{args.cao_port}"


def hutch_url(args: argparse.Namespace) -> str:
    return f"http://{args.host}:{args.dashboard_port}"


def resolve_cao_repo() -> Path:
    try:
        repo = default_cao_repo()
    except RuntimeError as error:
        raise DeployError(str(error)) from error
    if not (repo / "pyproject.toml").is_file():
        raise DeployError(f"CAO_REPO is not a Python project: {repo}")
    return repo


def health_json(url: str, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
            return json.loads(payload) if payload else {}
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def health_ok(url: str, timeout: float = 2.0) -> bool:
    return health_json(url, timeout=timeout) is not None


def read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def process_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def remove_stale_pid(path: Path) -> None:
    pid = read_pid(path)
    if pid is not None and not process_running(pid):
        path.unlink(missing_ok=True)


def wait_for_health(url: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_ok(url):
            return True
        time.sleep(1)
    return False


def check_records(args: argparse.Namespace) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str = "") -> None:
        records.append({"name": name, "status": status, "detail": detail})

    add("python", "OK", sys.executable)
    uv_path = shutil.which("uv")
    add("uv", "OK" if uv_path else "FAIL", uv_path or "required to run CAO/FastMCP")
    git_path = shutil.which("git")
    add("git", "OK" if git_path else "WARN", git_path or "not required for serving")

    codex = shutil.which("codex")
    opencode = shutil.which("opencode")
    if codex and opencode:
        add("agent-cli", "OK", f"codex={codex}; opencode={opencode}")
    elif codex:
        add("agent-cli", "OK", f"codex={codex}; opencode not found")
    elif opencode:
        add("agent-cli", "OK", f"opencode={opencode}; codex not found")
    else:
        add("agent-cli", "FAIL", "install at least one of codex or opencode")

    try:
        repo = resolve_cao_repo()
        add("cao-repo", "OK", str(repo))
    except DeployError as error:
        add("cao-repo", "FAIL", str(error))

    add(
        "cao-health",
        "OK" if health_ok(f"{cao_url(args)}/health") else "WARN",
        f"{cao_url(args)}/health",
    )
    add(
        "hutch-health",
        "OK" if health_ok(f"{hutch_url(args)}/api/health") else "WARN",
        f"{hutch_url(args)}/api/health",
    )
    return records


def has_failures(records: list[dict[str, str]]) -> bool:
    return any(record["status"] == "FAIL" for record in records)


def init_runtime(args: argparse.Namespace) -> dict[str, Any]:
    repo = resolve_cao_repo()
    paths = runtime_paths()
    for key in ("home", "logs", "pids", "runs", "generated", "workflows"):
        paths[key].mkdir(parents=True, exist_ok=True)
    if not paths["projects_file"].exists():
        atomic_json(
            paths["projects_file"],
            {"schema": "hutch.projects.v1", "projects": []},
        )
    env_text = "\n".join(
        [
            f"HUTCH_HOME={paths['home']}",
            f"HUTCH_URL={hutch_url(args)}",
            f"CAO_REPO={repo}",
            f"CAO_API_HOST={args.cao_host}",
            f"CAO_API_PORT={args.cao_port}",
            "",
        ]
    )
    atomic_text(paths["env"], env_text)
    return {
        "ok": True,
        "hutch_home": str(paths["home"]),
        "env": str(paths["env"]),
        "projects_file": str(paths["projects_file"]),
    }


def child_env(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    env["HUTCH_HOME"] = str(hutch_home())
    env["HUTCH_URL"] = hutch_url(args)
    env["CAO_REPO"] = str(resolve_cao_repo())
    env["CAO_API_HOST"] = args.cao_host
    env["CAO_API_PORT"] = str(args.cao_port)
    return env


def start_process(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    pid_path: Path,
    health_url: str,
    timeout: int,
) -> dict[str, Any]:
    remove_stale_pid(pid_path)
    if health_ok(health_url):
        return {"name": name, "status": "already-running", "health": health_url}
    pid = read_pid(pid_path)
    if process_running(pid):
        raise DeployError(
            f"{name} has live pid {pid} but health check failed: {health_url}. "
            f"Inspect {log_path} or run stop first."
        )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as stream:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    atomic_text(pid_path, f"{process.pid}\n")
    if not wait_for_health(health_url, timeout):
        raise DeployError(
            f"{name} did not become healthy within {timeout}s: {health_url}; log={log_path}"
        )
    return {
        "name": name,
        "status": "started",
        "pid": process.pid,
        "health": health_url,
        "log": str(log_path),
    }


def start_services(args: argparse.Namespace) -> dict[str, Any]:
    init_runtime(args)
    paths = runtime_paths()
    repo = resolve_cao_repo()
    env = child_env(args)
    uv = shutil.which("uv")
    if not uv:
        raise DeployError("uv is required to start CAO")
    cao = start_process(
        "cao-server",
        [uv, "--directory", str(repo), "run", "cao-server"],
        cwd=repo,
        env=env,
        log_path=paths["cao_log"],
        pid_path=paths["cao_pid"],
        health_url=f"{cao_url(args)}/health",
        timeout=args.timeout,
    )
    dashboard = start_process(
        "hutch-dashboard",
        [
            sys.executable,
            str(ROOT / "scripts" / "run_hutch_dashboard.py"),
            "--host",
            args.host,
            "--port",
            str(args.dashboard_port),
            "--runs-dir",
            str(paths["runs"]),
            "--projects-file",
            str(paths["projects_file"]),
            "--cao-url",
            cao_url(args),
        ],
        cwd=ROOT,
        env=env,
        log_path=paths["dashboard_log"],
        pid_path=paths["dashboard_pid"],
        health_url=f"{hutch_url(args)}/api/health",
        timeout=args.timeout,
    )
    return {"ok": True, "services": [cao, dashboard], "hutch_url": hutch_url(args)}


def stop_pid(name: str, pid_path: Path, timeout: int = 10) -> dict[str, Any]:
    pid = read_pid(pid_path)
    if not process_running(pid):
        pid_path.unlink(missing_ok=True)
        return {"name": name, "status": "not-running"}
    assert pid is not None
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_running(pid):
            pid_path.unlink(missing_ok=True)
            return {"name": name, "status": "stopped", "pid": pid}
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    pid_path.unlink(missing_ok=True)
    return {"name": name, "status": "killed", "pid": pid}


def stop_services(_: argparse.Namespace) -> dict[str, Any]:
    paths = runtime_paths()
    # Stop dashboard first so it stops proxying to CAO before CAO exits.
    return {
        "ok": True,
        "services": [
            stop_pid("hutch-dashboard", paths["dashboard_pid"]),
            stop_pid("cao-server", paths["cao_pid"]),
        ],
    }


def count_json_projects(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        projects = value.get("projects", [])
        return len(projects) if isinstance(projects, list) else 0
    except (OSError, json.JSONDecodeError):
        return 0


def status_report(args: argparse.Namespace) -> dict[str, Any]:
    paths = runtime_paths()
    run_count = 0
    if paths["runs"].is_dir():
        run_count = sum(1 for path in paths["runs"].glob("*/state.json"))
    return {
        "ok": True,
        "hutch_home": str(paths["home"]),
        "hutch_url": hutch_url(args),
        "cao_url": cao_url(args),
        "cao_server": {
            "pid": read_pid(paths["cao_pid"]),
            "pid_running": process_running(read_pid(paths["cao_pid"])),
            "healthy": health_ok(f"{cao_url(args)}/health"),
            "log": str(paths["cao_log"]),
        },
        "hutch_dashboard": {
            "pid": read_pid(paths["dashboard_pid"]),
            "pid_running": process_running(read_pid(paths["dashboard_pid"])),
            "healthy": health_ok(f"{hutch_url(args)}/api/health"),
            "log": str(paths["dashboard_log"]),
        },
        "projects_file": str(paths["projects_file"]),
        "project_count": count_json_projects(paths["projects_file"]),
        "run_count": run_count,
    }


def run_command(command: list[str], *, env: dict[str, str]) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise DeployError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": True, "output": result.stdout.strip()}


def provider_available(provider: str) -> tuple[bool, str]:
    if provider == "opencode_cli":
        path = shutil.which("opencode")
        return (path is not None, path or "opencode not found")
    if provider == "codex":
        path = shutil.which("codex")
        return (path is not None, path or "codex not found")
    return (True, f"provider {provider!r} has no deploy-time CLI check")


def register_project(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any] | None:
    if not args.project_root:
        return None
    command = [
        str(ROOT / "bin" / "hutch"),
        "--json",
        "project",
        "open",
        str(Path(args.project_root).expanduser()),
    ]
    if args.project_name:
        command.extend(["--name", args.project_name])
    if args.project_id:
        command.extend(["--id", args.project_id])
    return run_command(command, env=env)


def render_and_install_flow(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any] | None:
    if not args.target_repo:
        if args.install_flow or args.start_flow:
            raise DeployError("--target-repo is required when installing or starting a flow")
        return None
    target = Path(args.target_repo).expanduser()
    flow_name = args.flow_name or safe_name(f"{target.name}-{args.template}")
    render_command = [
        str(ROOT / "bin" / "hutch"),
        "--json",
        "flow",
        "from-template",
        str(target),
        "--template",
        args.template,
        "--name",
        flow_name,
    ]
    if args.strict_skills:
        render_command.append("--strict-skills")
    for root in args.skill_root:
        render_command.extend(["--skill-root", str(Path(root).expanduser())])
    rendered = run_command(render_command, env=env)
    workflow_path = Path(str(rendered["workflow"])).expanduser()
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    provider = str(workflow.get("provider", ""))
    ok, detail = provider_available(provider)
    if not ok:
        raise DeployError(
            f"rendered workflow {workflow_path} requires provider={provider}, but {detail}. "
            "Install the matching CLI or render a workflow for an available provider."
        )
    if not args.install_flow and not args.start_flow:
        return {"rendered": rendered, "provider_check": detail}
    compile_command = [
        str(ROOT / "bin" / "hutch"),
        "--json",
        "flow",
        "compile",
        str(workflow_path),
        "--install",
        "--replace",
    ]
    compiled = run_command(compile_command, env=env)
    started = None
    if args.start_flow:
        started = run_command(
            [str(ROOT / "bin" / "hutch"), "--json", "flow", "start", flow_name],
            env=env,
        )
    return {
        "rendered": rendered,
        "provider_check": detail,
        "compiled": compiled,
        "started": started,
    }


def cmd_check(args: argparse.Namespace) -> tuple[int, Any]:
    records = check_records(args)
    return (2 if has_failures(records) else 0), {"ok": not has_failures(records), "checks": records}


def cmd_init(args: argparse.Namespace) -> tuple[int, Any]:
    return 0, init_runtime(args)


def cmd_start(args: argparse.Namespace) -> tuple[int, Any]:
    return 0, start_services(args)


def cmd_stop(args: argparse.Namespace) -> tuple[int, Any]:
    return 0, stop_services(args)


def cmd_status(args: argparse.Namespace) -> tuple[int, Any]:
    return 0, status_report(args)


def cmd_all(args: argparse.Namespace) -> tuple[int, Any]:
    records = check_records(args)
    if has_failures(records):
        return 2, {"ok": False, "checks": records}
    initialized = init_runtime(args)
    services = None if args.skip_start else start_services(args)
    env = child_env(args)
    project = register_project(args, env)
    flow = render_and_install_flow(args, env)
    return 0, {
        "ok": True,
        "checks": records,
        "initialized": initialized,
        "services": services,
        "project": project,
        "flow": flow,
        "status": status_report(args),
    }


def render_text(value: Any) -> str:
    if isinstance(value, dict) and "checks" in value:
        lines = []
        for record in value["checks"]:
            detail = f" - {record['detail']}" if record.get("detail") else ""
            lines.append(f"{record['status']:<5} {record['name']}{detail}")
        rest = {key: item for key, item in value.items() if key != "checks"}
        if rest:
            lines.append(json.dumps(rest, indent=2, ensure_ascii=False))
        return "\n".join(lines)
    return json.dumps(value, indent=2, ensure_ascii=False)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hutch-home", help="runtime root; default ~/.hutch")
    parser.add_argument("--cao-repo", help="local cli-agent-orchestrator checkout")
    parser.add_argument("--host", default="127.0.0.1", help="Hutch dashboard bind host")
    parser.add_argument("--dashboard-port", type=int, default=9890)
    parser.add_argument("--cao-host", default="127.0.0.1")
    parser.add_argument("--cao-port", type=int, default=9889)
    parser.add_argument("--timeout", type=int, default=60, help="service health wait seconds")
    parser.add_argument("--json", action="store_true", help="emit JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hutch-deploy", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    handlers = {
        "check": cmd_check,
        "init": cmd_init,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
    }
    for name, handler in handlers.items():
        command = commands.add_parser(name)
        add_common_arguments(command)
        command.set_defaults(handler=handler)

    command = commands.add_parser("all")
    add_common_arguments(command)
    command.add_argument("--skip-start", action="store_true", help="initialize and optional config only")
    command.add_argument("--project-root")
    command.add_argument("--project-id")
    command.add_argument("--project-name")
    command.add_argument("--target-repo")
    command.add_argument("--template", default="one-run")
    command.add_argument("--flow-name")
    command.add_argument("--install-flow", action="store_true")
    command.add_argument("--start-flow", action="store_true")
    command.add_argument("--strict-skills", action="store_true")
    command.add_argument("--skill-root", action="append", default=[])
    command.set_defaults(handler=cmd_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_environment(args)
    try:
        exit_code, value = args.handler(args)
    except (DeployError, OSError, json.JSONDecodeError, ValueError) as error:
        value = {"ok": False, "error": str(error)}
        exit_code = 2
    if args.json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
    else:
        print(render_text(value))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
