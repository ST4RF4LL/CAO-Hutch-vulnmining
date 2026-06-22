#!/usr/bin/env python3
"""Human and agent-facing control CLI for the local Hutch/CAO stack."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAO_REPO = Path("/Users/wh4lter/Workspace/lab/cli-agent-orchestrator")


class HutchCliError(RuntimeError):
    pass


class HutchClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=190) as response:
                payload = response.read()
                return json.loads(payload.decode("utf-8")) if payload else {}
        except urllib.error.HTTPError as error:
            payload = error.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(payload).get("error", payload)
            except json.JSONDecodeError:
                message = payload
            raise HutchCliError(f"Hutch API {error.code}: {message}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise HutchCliError(
                f"Hutch is unavailable at {self.base_url}: {error}"
            ) from error

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, body or {})


def quoted(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def project_open(args: argparse.Namespace, client: HutchClient) -> Any:
    path = Path(args.path).expanduser().resolve(strict=False)
    value = client.post(
        "/api/projects/open",
        {"path": str(path), "name": args.name, "id": args.project_id},
    )
    if args.browser:
        webbrowser.open(client.base_url)
    return value


def project_list(_: argparse.Namespace, client: HutchClient) -> Any:
    return client.get("/api/projects")


def project_info(args: argparse.Namespace, client: HutchClient) -> Any:
    return client.get(f"/api/projects/{quoted(args.project_id)}")


def flow_list(args: argparse.Namespace, client: HutchClient) -> Any:
    runs = client.get("/api/runs")
    if args.status:
        runs = [run for run in runs if run.get("status") == args.status]
    if args.project:
        runs = [
            run
            for run in runs
            if (run.get("project") or {}).get("id") == args.project
        ]
    return runs


def flow_info(args: argparse.Namespace, client: HutchClient) -> Any:
    return client.get(f"/api/runs/{quoted(args.run_id)}")


def flow_catalog(_: argparse.Namespace, client: HutchClient) -> Any:
    return client.get("/api/cao/catalog").get("flows", [])


def flow_action(args: argparse.Namespace, client: HutchClient) -> Any:
    return client.post(f"/api/flows/{quoted(args.flow_name)}/{args.flow_action}")


def flow_stop(args: argparse.Namespace, client: HutchClient) -> Any:
    return client.post(f"/api/runs/{quoted(args.run_id)}/stop")


def flow_compile(args: argparse.Namespace, _: HutchClient) -> Any:
    workflow = Path(args.workflow).expanduser().resolve()
    command = [
        sys.executable,
        str(ROOT / "scripts" / "generate_cao_native_flow.py"),
        str(workflow),
        "--cao-repo",
        str(Path(args.cao_repo).expanduser().resolve()),
    ]
    if args.output:
        command.extend(["--output", str(Path(args.output).expanduser().resolve())])
    if args.install:
        command.append("--install")
    if args.replace:
        command.append("--replace")
    if args.enable:
        command.append("--enable")
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise HutchCliError(result.stdout.strip() or "flow compilation failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": True, "output": result.stdout.strip()}


def agent_list(_: argparse.Namespace, client: HutchClient) -> Any:
    return client.get("/api/cao/catalog").get("profiles", [])


def agent_info(args: argparse.Namespace, client: HutchClient) -> Any:
    profiles = agent_list(args, client)
    for profile in profiles:
        name = profile if isinstance(profile, str) else profile.get("name")
        if name == args.profile_name:
            return profile
    raise HutchCliError(f"agent profile not found: {args.profile_name}")


def agent_import_external(args: argparse.Namespace, _: HutchClient) -> Any:
    source = Path(args.source).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    cao_repo = Path(args.cao_repo).expanduser().resolve()
    command = [
        "uv",
        "run",
        "--directory",
        str(cao_repo),
        "python",
        str(ROOT / "scripts" / "import_external_agents.py"),
        str(source),
        str(output),
        "--format",
        args.format,
        "--provider",
        args.provider,
        "--cao-repo",
        str(cao_repo),
    ]
    if args.prefix:
        command.extend(["--prefix", args.prefix])
    for selector in args.agent:
        command.extend(["--agent", selector])
    for selector in args.skill:
        command.extend(["--skill", selector])
    for flag in (
        "include_skills",
        "allow_write",
        "allow_shell",
        "allow_supervisor",
        "replace",
        "dry_run",
    ):
        if getattr(args, flag, False):
            command.append("--" + flag.replace("_", "-"))
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise HutchCliError(result.stdout.strip() or "external agent import failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise HutchCliError(f"importer returned invalid JSON: {result.stdout}") from error


def render(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return "No records."
        rows = []
        for item in value:
            if isinstance(item, str):
                rows.append(item)
                continue
            if "run_id" in item:
                rows.append(
                    "\t".join(
                        str(part)
                        for part in (
                            item.get("run_id", "-"),
                            item.get("status", "-"),
                            item.get("workflow", "-"),
                            (item.get("project") or {}).get("name", "-"),
                            f"{item.get('stages_done', 0)}/{item.get('stages_total', 0)}",
                        )
                    )
                )
            elif "root_path" in item:
                rows.append(
                    f"{item.get('id')}\t{item.get('name')}\t{item.get('root_path')}\t"
                    f"{item.get('service_count', 0)} services\t{item.get('flow_count', 0)} flows"
                )
            else:
                rows.append(
                    f"{item.get('name', item.get('id', '-'))}\t"
                    f"{item.get('provider', '')}\t{item.get('enabled', '')}".rstrip()
                )
        return "\n".join(rows)
    if isinstance(value, dict):
        return json.dumps(value, indent=2, ensure_ascii=False)
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hutch", description=__doc__)
    parser.add_argument(
        "--url", default=os.environ.get("HUTCH_URL", "http://127.0.0.1:9890")
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON output")
    groups = parser.add_subparsers(dest="group", required=True)

    project = groups.add_parser("project", help="manage application project roots")
    project_commands = project.add_subparsers(dest="command", required=True)
    command = project_commands.add_parser("open", help="register and inspect a project root")
    command.add_argument("path")
    command.add_argument("--name")
    command.add_argument("--id", dest="project_id")
    command.add_argument("--browser", action="store_true")
    command.set_defaults(handler=project_open)
    command = project_commands.add_parser("list")
    command.set_defaults(handler=project_list)
    command = project_commands.add_parser("info")
    command.add_argument("project_id")
    command.set_defaults(handler=project_info)

    flow = groups.add_parser("flow", help="compile and control CAO-visible flows")
    flow_commands = flow.add_subparsers(dest="command", required=True)
    command = flow_commands.add_parser("list", help="list Hutch flow instances")
    command.add_argument("--project")
    command.add_argument("--status")
    command.set_defaults(handler=flow_list)
    command = flow_commands.add_parser("info", help="show one flow instance")
    command.add_argument("run_id")
    command.set_defaults(handler=flow_info)
    command = flow_commands.add_parser("catalog", help="list CAO flow definitions")
    command.set_defaults(handler=flow_catalog)
    for action in ("start", "enable", "disable"):
        command = flow_commands.add_parser(action)
        command.add_argument("flow_name")
        command.set_defaults(handler=flow_action, flow_action=action)
    command = flow_commands.add_parser("stop", help="stop a running Hutch flow instance")
    command.add_argument("run_id")
    command.set_defaults(handler=flow_stop)
    command = flow_commands.add_parser("compile", help="compile a workflow into a native CAO bundle")
    command.add_argument("workflow")
    command.add_argument("--output")
    command.add_argument("--cao-repo", default=str(DEFAULT_CAO_REPO))
    command.add_argument("--install", action="store_true")
    command.add_argument("--replace", action="store_true")
    command.add_argument("--enable", action="store_true")
    command.set_defaults(handler=flow_compile)

    agent = groups.add_parser("agent", help="inspect and customize agent profiles")
    agent_commands = agent.add_subparsers(dest="command", required=True)
    command = agent_commands.add_parser("list")
    command.set_defaults(handler=agent_list)
    command = agent_commands.add_parser("info")
    command.add_argument("profile_name")
    command.set_defaults(handler=agent_info)
    for command_name, default_format in (
        ("construct", "auto"),
        ("import-external", "auto"),
        ("import-opencode", "opencode"),
    ):
        command = agent_commands.add_parser(command_name)
        command.add_argument("source")
        command.add_argument("--output", default=str(ROOT / "cao-profiles"))
        command.add_argument(
            "--format",
            choices=("auto", "opencode", "claude", "codex", "generic"),
            default=default_format,
        )
        command.add_argument("--provider", default="opencode_cli")
        command.add_argument("--prefix", default="")
        command.add_argument("--agent", action="append", default=[])
        command.add_argument("--skill", action="append", default=[])
        command.add_argument("--include-skills", action="store_true")
        command.add_argument("--allow-write", action="store_true")
        command.add_argument("--allow-shell", action="store_true")
        command.add_argument("--allow-supervisor", action="store_true")
        command.add_argument("--replace", action="store_true")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--cao-repo", default=str(DEFAULT_CAO_REPO))
        command.set_defaults(handler=agent_import_external)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = HutchClient(args.url)
    try:
        value = args.handler(args, client)
    except (HutchCliError, OSError, ValueError) as error:
        print(f"hutch: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
    else:
        print(render(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
