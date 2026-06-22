"""Build a stable dashboard view from Hutch run directories and CAO snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


class RunNotFound(KeyError):
    pass


class ProjectNotFound(KeyError):
    pass


class CampaignNotFound(KeyError):
    pass


class RunDeleteConflict(RuntimeError):
    pass


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    except (OSError, json.JSONDecodeError):
        pass
    return records


def duration_seconds(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
    except ValueError:
        return None


class RunRepository:
    """Read-only projection over on-disk run evidence."""

    def __init__(
        self,
        runs_dir: Path,
        terminal_logs: Path | None = None,
        cao_db: Path | None = None,
        projects_file: Path | None = None,
    ) -> None:
        self.runs_dir = runs_dir.resolve()
        self.terminal_logs = (
            terminal_logs
            or Path.home() / ".aws" / "cli-agent-orchestrator" / "logs" / "terminal"
        ).resolve()
        self.cao_db = (
            cao_db
            or Path.home()
            / ".aws"
            / "cli-agent-orchestrator"
            / "db"
            / "cli-agent-orchestrator.db"
        ).resolve()
        self.projects_file = projects_file.resolve() if projects_file else None
        self._projects_lock = threading.Lock()
        self._configured_projects = self._load_projects()

    def list_runs(
        self,
        status: str | None = None,
        active_sessions: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        if not self.runs_dir.is_dir():
            return runs
        for state_path in self.runs_dir.glob("*/state.json"):
            state = read_json(state_path, {})
            if not isinstance(state, dict) or not state.get("run_id"):
                continue
            raw_status = str(state.get("status", "unknown"))
            effective_status = self._effective_status(
                raw_status, state.get("cao_session"), active_sessions
            )
            if status and effective_status != status:
                continue
            stages = state.get("stages", {})
            done = sum(1 for item in stages.values() if item.get("status") == "done")
            workflow = read_json(state_path.parent / "workflow.json", {})
            target = state.get("target_fingerprint", {}).get("target") or workflow.get("target")
            context = self._target_context(target)
            runs.append(
                {
                    "run_id": state["run_id"],
                    "workflow": state.get("workflow", state_path.parent.name),
                    "status": effective_status,
                    "raw_status": raw_status,
                    "created_at": state.get("created_at"),
                    "finished_at": state.get("finished_at"),
                    "duration_seconds": duration_seconds(
                        state.get("created_at"), state.get("finished_at")
                    ),
                    "stages_done": done,
                    "stages_total": len(stages),
                    "cao_session": state.get("cao_session"),
                    "finding_records": state.get("finding_records"),
                    "campaign": state.get("campaign") or workflow.get("campaign"),
                    "target": target,
                    "target_head": state.get("target_fingerprint", {}).get("head"),
                    **context,
                }
            )
        runs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return runs

    @staticmethod
    def _campaign_instance_id(campaign_id: str, root_run_id: str, target: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", campaign_id).strip("-") or "campaign"
        digest = hashlib.sha256(
            f"{campaign_id}\0{root_run_id}\0{target}".encode("utf-8")
        ).hexdigest()[:12]
        return f"{slug[:48]}-{digest}"

    @staticmethod
    def _campaign_status(statuses: list[str]) -> str:
        for status in (
            "running",
            "launching",
            "orphaned",
            "failed-integrity",
            "failed",
            "invalid",
            "stopped",
            "prepared",
        ):
            if status in statuses:
                return status
        return "completed" if statuses and all(value == "completed" for value in statuses) else "unknown"

    def list_campaigns(
        self,
        status: str | None = None,
        active_sessions: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate CAO-visible child flows into stable campaign instances."""
        groups: dict[str, dict[str, Any]] = {}
        for run in self.list_runs(active_sessions=active_sessions):
            campaign = run.get("campaign")
            if not isinstance(campaign, dict) or not campaign.get("id"):
                continue
            phase = str(campaign.get("phase") or "flow")
            root_run_id = str(
                campaign.get("intelligence_run_id")
                or (run["run_id"] if phase == "recon" else campaign.get("parent_run_id"))
                or run["run_id"]
            )
            instance_id = self._campaign_instance_id(
                str(campaign["id"]), root_run_id, str(run.get("target") or "")
            )
            item = groups.setdefault(
                instance_id,
                {
                    "instance_id": instance_id,
                    "campaign_id": str(campaign["id"]),
                    "root_run_id": root_run_id,
                    "schema": campaign.get("schema"),
                    "target": run.get("target"),
                    "target_head": run.get("target_head"),
                    "project": run["project"],
                    "domain": run["domain"],
                    "service": run["service"],
                    "flows": [],
                },
            )
            item["flows"].append({**run, "phase": phase})

        phase_order = {"recon": 0, "planning": 1, "mining": 2}
        campaigns: list[dict[str, Any]] = []
        for item in groups.values():
            item["flows"].sort(
                key=lambda flow: (
                    phase_order.get(flow["phase"], 50),
                    flow.get("created_at") or "",
                )
            )
            statuses = [flow["status"] for flow in item["flows"]]
            item["status"] = self._campaign_status(statuses)
            phases = {flow["phase"] for flow in item["flows"]}
            if item["status"] == "completed" and not {
                "recon",
                "planning",
                "mining",
            }.issubset(phases):
                item["status"] = "incomplete"
            if status and item["status"] != status:
                continue
            created = [flow["created_at"] for flow in item["flows"] if flow.get("created_at")]
            finished = [flow["finished_at"] for flow in item["flows"] if flow.get("finished_at")]
            item["created_at"] = min(created) if created else None
            item["finished_at"] = max(finished) if finished else None
            item["duration_seconds"] = duration_seconds(
                item["created_at"], item["finished_at"]
            )
            item["flow_count"] = len(item["flows"])
            item["stages_done"] = sum(flow["stages_done"] for flow in item["flows"])
            item["stages_total"] = sum(flow["stages_total"] for flow in item["flows"])
            item["phases"] = [flow["phase"] for flow in item["flows"]]
            campaigns.append(item)
        campaigns.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return campaigns

    def get_campaign(
        self, instance_id: str, active_sessions: set[str] | None = None
    ) -> dict[str, Any]:
        summary = next(
            (
                item
                for item in self.list_campaigns(active_sessions=active_sessions)
                if item["instance_id"] == instance_id
            ),
            None,
        )
        if summary is None:
            raise CampaignNotFound(instance_id)
        flows = [
            self.get_run(item["run_id"], active_sessions=active_sessions)
            | {"phase": item["phase"]}
            for item in summary["flows"]
        ]
        by_id = {flow["run_id"]: flow for flow in flows}
        edges: list[dict[str, Any]] = []
        for flow in flows:
            campaign = flow.get("campaign") or {}
            parent_id = (
                campaign.get("parent_run_id")
                or campaign.get("planning_run_id")
                or campaign.get("intelligence_run_id")
            )
            if parent_id in by_id and parent_id != flow["run_id"]:
                edges.append(
                    {
                        "id": f"{parent_id}--{flow['run_id']}",
                        "source": parent_id,
                        "target": flow["run_id"],
                        "type": "handoff",
                        "transfers": [],
                    }
                )
        graph = {
            "nodes": [
                {
                    "id": flow["run_id"],
                    "label": flow["workflow"],
                    "status": flow["status"],
                    "type": "flow",
                    "phase": flow["phase"],
                }
                for flow in flows
            ],
            "edges": edges,
        }
        deliverables = [
            {
                **artifact,
                "run_id": flow["run_id"],
                "workflow": flow["workflow"],
                "phase": flow["phase"],
            }
            for flow in flows
            for artifact in flow.get("deliverables", [])
        ]
        reports = [
            report
            for flow in summary["flows"]
            for report in [self._report_summary(flow)]
            if report
        ]
        final_summary = next(
            (flow.get("summary") for flow in reversed(flows) if flow.get("summary")),
            None,
        )
        return {
            **summary,
            "flows": flows,
            "graph": graph,
            "deliverables": deliverables,
            "reports": reports,
            "summary": final_summary,
            "agent_count": sum(len(flow.get("agents", [])) for flow in flows),
            "integrity": all(flow.get("integrity", {}).get("ok") is not False for flow in flows),
        }

    def list_projects(
        self,
        status: str | None = None,
        active_sessions: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return adaptive directory trees whose Git repositories are service leaves."""
        projects = {
            project["id"]: self._discover_project(project)
            for project in self._configured_projects
        }
        for run in self.list_runs(status=status, active_sessions=active_sessions):
            project = run["project"]
            if project["id"] not in projects:
                projects[project["id"]] = self._empty_project(project)
            item = projects[project["id"]]
            service = self._ensure_tree_service(item["tree"], run["service"])
            service["flows"].append(run)
            item["flows"].append(run)
            report = self._report_summary(run)
            if report:
                service["reports"].append(report)
            finished_at = run.get("finished_at")
            if finished_at and (
                not item["latest_finished_at"] or finished_at > item["latest_finished_at"]
            ):
                item["latest_finished_at"] = finished_at
            if finished_at and (
                not service["latest_finished_at"]
                or finished_at > service["latest_finished_at"]
            ):
                service["latest_finished_at"] = finished_at
        for project in projects.values():
            self._rollup_tree(project["tree"])
            project["directory_count"] = project["tree"]["directory_count"]
            project["domain_count"] = project["directory_count"]
            project["service_count"] = project["tree"]["service_count"]
            project["flow_count"] = project["tree"]["flow_count"]
            project["report_count"] = project["tree"]["report_count"]
            project["flows"].sort(
                key=lambda value: value.get("created_at") or "", reverse=True
            )
        values = list(projects.values())
        values.sort(
            key=lambda item: (item.get("latest_finished_at") or "", item["name"].lower()),
            reverse=True,
        )
        return values

    def get_project(
        self,
        project_id: str,
        status: str | None = None,
        active_sessions: set[str] | None = None,
    ) -> dict[str, Any]:
        for project in self.list_projects(status=status, active_sessions=active_sessions):
            if project["id"] == project_id:
                return project
        raise ProjectNotFound(project_id)

    def _load_projects(self) -> list[dict[str, str]]:
        if not self.projects_file or not self.projects_file.is_file():
            return []
        value = read_json(self.projects_file)
        if not isinstance(value, dict) or not isinstance(value.get("projects"), list):
            raise ValueError(f"projects file must contain a projects array: {self.projects_file}")
        projects: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in value["projects"]:
            if not isinstance(raw, dict) or not raw.get("root"):
                raise ValueError("each project must contain a root directory")
            root = Path(str(raw["root"])).expanduser().resolve(strict=False)
            project_id = str(raw.get("id") or self._path_id(root.name or "project", root))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project_id) or project_id in seen:
                raise ValueError(f"invalid or duplicate project id: {project_id}")
            seen.add(project_id)
            projects.append(
                {
                    "id": project_id,
                    "name": str(raw.get("name") or root.name or project_id),
                    "root_path": str(root),
                    "repo_path": str(root),
                    "configured": True,
                }
            )
        return projects

    def open_project(
        self, root: str, name: str | None = None, project_id: str | None = None
    ) -> dict[str, Any]:
        """Register an application root and immediately expose its discovered tree."""
        if not self.projects_file:
            raise ValueError("Hutch projects file is not configured")
        if not isinstance(root, str) or not root.strip():
            raise ValueError("project path cannot be empty")
        root_path = Path(root).expanduser().resolve(strict=False)
        if not root_path.is_dir():
            raise ValueError(f"project root is not a directory: {root_path}")
        effective_id = project_id or self._path_id(root_path.name or "project", root_path)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", effective_id):
            raise ValueError(f"invalid project id: {effective_id!r}")
        effective_name = str(name or root_path.name or effective_id).strip()
        if not effective_name:
            raise ValueError("project name cannot be empty")

        with self._projects_lock:
            document = read_json(self.projects_file, {"projects": []})
            if not isinstance(document, dict) or not isinstance(
                document.get("projects"), list
            ):
                raise ValueError(
                    f"projects file must contain a projects array: {self.projects_file}"
                )
            projects = document["projects"]
            replacement = {
                "id": effective_id,
                "name": effective_name,
                "root": str(root_path),
            }
            matched = False
            for index, item in enumerate(projects):
                if not isinstance(item, dict):
                    continue
                item_root = Path(str(item.get("root", ""))).expanduser().resolve(
                    strict=False
                )
                item_id = str(item.get("id", ""))
                if item_id == effective_id and item_root != root_path:
                    raise ValueError(
                        f"project id {effective_id!r} is already used by {item_root}"
                    )
                if item_root == root_path:
                    projects[index] = replacement
                    matched = True
                    break
            if not matched:
                projects.append(replacement)
            document.setdefault("schema", "hutch.projects.v1")
            self.projects_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.projects_file.with_suffix(self.projects_file.suffix + ".tmp")
            temporary.write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.projects_file)
            self._configured_projects = self._load_projects()
        return self.get_project(effective_id)

    @staticmethod
    def _path_id(name: str, path: Path) -> str:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "project"
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]
        return f"{slug}-{digest}"

    @staticmethod
    def _git_repo_for_target(target: str | None) -> Path:
        if target:
            target_path = Path(target).expanduser().resolve(strict=False)
            start = target_path if target_path.is_dir() else target_path.parent
            return next(
                (
                    candidate
                    for candidate in (start, *start.parents)
                    if (candidate / ".git").exists()
                ),
                target_path,
            )
        return Path("unknown-target")

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _target_context(self, target: str | None) -> dict[str, dict[str, Any]]:
        repo = self._git_repo_for_target(target)
        configured = sorted(
            (
                project
                for project in self._configured_projects
                if self._is_relative_to(repo, Path(project["root_path"]))
            ),
            key=lambda project: len(Path(project["root_path"]).parts),
            reverse=True,
        )
        if configured:
            project = dict(configured[0])
            root = Path(project["root_path"])
            relative = repo.relative_to(root)
            lineage = list(relative.parts[:-1])
        else:
            project = {
                "id": self._path_id(repo.name or "project", repo),
                "name": repo.name or str(repo),
                "root_path": str(repo),
                "repo_path": str(repo),
                "configured": False,
            }
            root = repo
            lineage = []
        domain_path = root.joinpath(*lineage) if lineage else root
        domain_name = " / ".join(lineage) if lineage else "root"
        domain = {
            "id": self._path_id(domain_name, domain_path),
            "name": domain_name,
            "path": str(domain_path),
            "relative_path": str(domain_path.relative_to(root)) if domain_path != root else ".",
            "lineage": lineage,
        }
        service = {
            "id": self._path_id(repo.name or "service", repo),
            "name": repo.name or str(repo),
            "repo_path": str(repo),
            "relative_path": str(repo.relative_to(root)) if self._is_relative_to(repo, root) else str(repo),
            "tree_path": lineage,
        }
        return {"project": project, "domain": domain, "service": service}

    @staticmethod
    def _empty_project(project: dict[str, Any]) -> dict[str, Any]:
        return {
            **project,
            "tree": {
                "id": f"{project['id']}-root",
                "type": "root",
                "name": project["name"],
                "path": project["root_path"],
                "relative_path": ".",
                "children": [],
            },
            "directory_count": 0,
            "domain_count": 0,
            "service_count": 0,
            "flow_count": 0,
            "report_count": 0,
            "latest_finished_at": None,
            "flows": [],
        }

    def _discover_project(self, project: dict[str, Any]) -> dict[str, Any]:
        item = self._empty_project(project)
        root = Path(project["root_path"])
        if not root.is_dir():
            item["available"] = False
            return item
        item["available"] = True
        if (root / ".git").exists():
            item["tree"]["children"].append(self._service_node(root, root, []))
        else:
            item["tree"]["children"] = self._discover_tree_children(root, root)
        return item

    def _discover_tree_children(self, directory: Path, root: Path) -> list[dict[str, Any]]:
        ignored = {"node_modules", "vendor", "dist", "build", ".gradle", ".idea"}
        try:
            paths = sorted(directory.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            return []
        nodes: list[dict[str, Any]] = []
        for path in paths:
            if (
                not path.is_dir()
                or path.is_symlink()
                or path.name.startswith(".")
                or path.name in ignored
            ):
                continue
            resolved = path.resolve()
            relative = resolved.relative_to(root)
            if (resolved / ".git").exists():
                nodes.append(
                    self._service_node(resolved, root, list(relative.parts[:-1]))
                )
                continue
            children = self._discover_tree_children(resolved, root)
            if children:
                nodes.append(
                    {
                        "id": self._path_id(resolved.name, resolved),
                        "type": "directory",
                        "name": resolved.name,
                        "path": str(resolved),
                        "relative_path": str(relative),
                        "children": children,
                    }
                )
        return nodes

    def _service_node(
        self, repo: Path, root: Path, tree_path: list[str]
    ) -> dict[str, Any]:
        return {
            "id": self._path_id(repo.name or "service", repo),
            "type": "service",
            "name": repo.name or str(repo),
            "repo_path": str(repo),
            "relative_path": str(repo.relative_to(root)) if repo != root else ".",
            "tree_path": tree_path,
            "flow_count": 0,
            "report_count": 0,
            "latest_finished_at": None,
            "flows": [],
            "reports": [],
        }

    def _ensure_tree_service(
        self, tree: dict[str, Any], value: dict[str, Any]
    ) -> dict[str, Any]:
        def find(node: dict[str, Any]) -> dict[str, Any] | None:
            if node.get("type") == "service" and node.get("id") == value["id"]:
                return node
            for child in node.get("children", []):
                result = find(child)
                if result:
                    return result
            return None

        existing = find(tree)
        if existing:
            return existing
        parent = tree
        root = Path(tree["path"])
        current = root
        for segment in value.get("tree_path", []):
            current /= segment
            node_id = self._path_id(segment, current)
            directory = next(
                (
                    child
                    for child in parent["children"]
                    if child.get("type") == "directory" and child["id"] == node_id
                ),
                None,
            )
            if not directory:
                directory = {
                    "id": node_id,
                    "type": "directory",
                    "name": segment,
                    "path": str(current),
                    "relative_path": str(current.relative_to(root)),
                    "children": [],
                }
                parent["children"].append(directory)
            parent = directory
        service = {
            **value,
            "type": "service",
            "flow_count": 0,
            "report_count": 0,
            "latest_finished_at": None,
            "flows": [],
            "reports": [],
        }
        parent["children"].append(service)
        return service

    def _rollup_tree(self, node: dict[str, Any]) -> None:
        if node.get("type") == "service":
            node["flows"].sort(
                key=lambda value: value.get("created_at") or "", reverse=True
            )
            node["reports"].sort(
                key=lambda value: value.get("finished_at") or "", reverse=True
            )
            node["directory_count"] = 0
            node["service_count"] = 1
            node["flow_count"] = len(node["flows"])
            node["report_count"] = len(node["reports"])
            return
        node["children"].sort(
            key=lambda value: (value.get("type") == "service", value["name"].lower())
        )
        for child in node["children"]:
            self._rollup_tree(child)
        node["directory_count"] = sum(
            child["directory_count"] + (1 if child.get("type") == "directory" else 0)
            for child in node["children"]
        )
        node["service_count"] = sum(child["service_count"] for child in node["children"])
        node["flow_count"] = sum(child["flow_count"] for child in node["children"])
        node["report_count"] = sum(child["report_count"] for child in node["children"])

    def _report_summary(self, run: dict[str, Any]) -> dict[str, Any] | None:
        run_dir = self.runs_dir / run["run_id"]
        workflow = read_json(run_dir / "workflow.json", {})
        stages = workflow.get("stages", []) if isinstance(workflow, dict) else []
        candidates: list[tuple[str, str | None]] = []
        if stages:
            final = stages[-1]
            if final.get("artifact"):
                candidates.append((final["artifact"], final.get("id")))
        artifacts = run_dir / "artifacts"
        if artifacts.is_dir():
            candidates.extend(
                (str(path.relative_to(run_dir)), None)
                for path in sorted(artifacts.glob("*report*.md"))
            )
        for relative, stage in candidates:
            path = (run_dir / relative).resolve()
            if path.is_file() and self._is_relative_to(path, run_dir.resolve()):
                return {
                    "run_id": run["run_id"],
                    "workflow": run["workflow"],
                    "status": run["status"],
                    "finished_at": run.get("finished_at"),
                    "path": str(path.relative_to(run_dir)),
                    "stage": stage,
                    "bytes": path.stat().st_size,
                }
        return None

    def get_run(
        self, run_id: str, active_sessions: set[str] | None = None
    ) -> dict[str, Any]:
        candidates = {
            item["run_id"] for item in self.list_runs(active_sessions=active_sessions)
        }
        if run_id not in candidates:
            raise RunNotFound(run_id)
        run_dir = (self.runs_dir / run_id).resolve()
        if run_dir.parent != self.runs_dir:
            raise RunNotFound(run_id)

        state = read_json(run_dir / "state.json", {})
        workflow = read_json(run_dir / "workflow.json", {})
        manifest = read_json(run_dir / "manifest.json", {})
        events = read_json_lines(run_dir / "events.jsonl")
        stage_defs = {
            stage["id"]: stage
            for stage in workflow.get("stages", [])
            if isinstance(stage, dict) and stage.get("id")
        }
        agents = [self._agent(run_dir, state, workflow, stage_id, stage, events) for stage_id, stage in stage_defs.items()]
        supervisor = self._supervisor(state, workflow, agents)
        if supervisor:
            agents.insert(0, supervisor)

        deliverables = self._flow_deliverables(run_dir, workflow)
        graph = self._graph(agents)
        final_stage = workflow.get("stages", [])[-1]["id"] if workflow.get("stages") else None
        summary = next(
            (
                agent.get("result_summary")
                for agent in agents
                if agent.get("stage") == final_stage and agent.get("result_summary")
            ),
            None,
        )
        target = state.get("target_fingerprint", {}).get("target") or workflow.get("target")
        context = self._target_context(target)
        raw_status = str(state.get("status", "unknown"))
        effective_status = self._effective_status(
            raw_status, state.get("cao_session"), active_sessions
        )
        return {
            "run_id": state.get("run_id", run_id),
            "workflow": state.get("workflow", workflow.get("name", run_id)),
            "workflow_version": workflow.get("version"),
            "status": effective_status,
            "raw_status": raw_status,
            "created_at": state.get("created_at"),
            "finished_at": state.get("finished_at"),
            "duration_seconds": duration_seconds(state.get("created_at"), state.get("finished_at")),
            "cao_flow": state.get("cao_flow"),
            "cao_session": state.get("cao_session"),
            "campaign": state.get("campaign") or workflow.get("campaign"),
            "target": target,
            "target_head": state.get("target_fingerprint", {}).get("head"),
            **context,
            "integrity": state.get("integrity") or {
                "ok": state.get("target_fingerprint") == state.get("final_target_fingerprint")
                if state.get("final_target_fingerprint")
                else None
            },
            "summary": summary,
            "stage_count": len(stage_defs),
            "agents": agents,
            "graph": graph,
            "deliverables": deliverables,
            "event_count": len(events),
            "manifest_present": bool(manifest),
        }

    def delete_run(
        self, run_id: str, active_sessions: set[str] | None = None
    ) -> dict[str, Any]:
        """Remove a finished run from the dashboard by moving it to recoverable trash."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
            raise RunNotFound(run_id)
        run_dir = (self.runs_dir / run_id).resolve()
        if run_dir.parent != self.runs_dir or not run_dir.is_dir():
            raise RunNotFound(run_id)
        state = read_json(run_dir / "state.json", {})
        if not isinstance(state, dict) or state.get("run_id") != run_id:
            raise RunNotFound(run_id)
        raw_status = str(state.get("status", "unknown"))
        status = self._effective_status(
            raw_status, state.get("cao_session"), active_sessions
        )
        if status in {"launching", "running"}:
            raise RunDeleteConflict(f"flow {run_id} is still {status}")
        trash = self.runs_dir / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = trash / f"{run_id}-{stamp}"
        try:
            run_dir.rename(destination)
        except OSError:
            shutil.move(str(run_dir), str(destination))
        return {
            "deleted": True,
            "run_id": run_id,
            "status": status,
            "raw_status": raw_status,
            "recoverable_path": str(destination),
        }

    def stop_run(self, run_id: str) -> dict[str, Any]:
        """Persist an operator-requested stop after CAO has removed the session."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
            raise RunNotFound(run_id)
        run_dir = (self.runs_dir / run_id).resolve()
        if run_dir.parent != self.runs_dir or not run_dir.is_dir():
            raise RunNotFound(run_id)
        state_path = run_dir / "state.json"
        state = read_json(state_path, {})
        if not isinstance(state, dict) or state.get("run_id") != run_id:
            raise RunNotFound(run_id)
        status = str(state.get("status", "unknown"))
        if status not in {"prepared", "launching", "running"}:
            raise RunDeleteConflict(f"flow {run_id} cannot be stopped from {status}")
        stopped_at = datetime.now().astimezone().isoformat(timespec="seconds")
        state["status"] = "stopped"
        state["stopped_at"] = stopped_at
        state["finished_at"] = stopped_at
        for stage in state.get("stages", {}).values():
            if stage.get("status") in {"launching", "running"}:
                stage["status"] = "interrupted"
        temporary = state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary.replace(state_path)
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "ts": stopped_at,
                        "event": "run_stopped_by_operator",
                        "previous_status": status,
                        "cao_session": state.get("cao_session"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return {
            "ok": True,
            "run_id": run_id,
            "status": "stopped",
            "previous_status": status,
            "cao_session": state.get("cao_session"),
        }

    @staticmethod
    def _effective_status(
        status: str,
        cao_session: str | None,
        active_sessions: set[str] | None,
    ) -> str:
        if (
            status in {"launching", "running"}
            and active_sessions is not None
            and (not cao_session or cao_session not in active_sessions)
        ):
            return "orphaned"
        return status

    def get_terminal_snapshot(self, terminal_id: str) -> dict[str, Any] | None:
        """Return the durable CAO terminal record after its tmux window is gone."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", terminal_id):
            return None
        metadata = self._terminal_metadata(terminal_id)
        if not metadata:
            return None
        output = ""
        for suffix in (".scrollback", ".log"):
            path = self.terminal_logs / f"{terminal_id}{suffix}"
            try:
                output = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if output:
                break
        return {
            "terminal_id": terminal_id,
            "session": metadata.get("session_name"),
            "window": metadata.get("window_name"),
            "provider": metadata.get("provider"),
            "agent_profile": metadata.get("agent_profile"),
            "working_directory": metadata.get("working_directory"),
            "live": False,
            "output": output,
        }

    @staticmethod
    def _graph(agents: list[dict[str, Any]]) -> dict[str, Any]:
        nodes = [
            {
                "id": agent["stage"],
                "label": agent.get("profile") or agent["stage"],
                "status": agent.get("status"),
                "type": "supervisor" if agent["stage"] == "flow-supervisor" else "agent",
            }
            for agent in agents
        ]
        by_stage = {agent["stage"]: agent for agent in agents}
        producer_by_path: dict[str, str] = {}
        for agent in agents:
            for deliverable in agent.get("deliverables", []):
                producer_by_path[deliverable["path"]] = agent["stage"]

        edges: dict[tuple[str, str], dict[str, Any]] = {}

        def ensure_edge(source: str, target: str, edge_type: str) -> dict[str, Any]:
            key = (source, target)
            if key not in edges:
                edges[key] = {
                    "id": f"{source}--{target}",
                    "source": source,
                    "target": target,
                    "type": edge_type,
                    "transfers": [],
                }
            elif edges[key]["type"] != edge_type:
                edges[key]["type"] = "dependency+data"
            return edges[key]

        supervisor_present = "flow-supervisor" in by_stage
        for agent in agents:
            target = agent["stage"]
            if target == "flow-supervisor":
                continue
            dependencies = agent.get("depends_on", [])
            if supervisor_present and not dependencies:
                ensure_edge("flow-supervisor", target, "dispatch")
            for source in dependencies:
                if source in by_stage:
                    ensure_edge(source, target, "dependency")
            for path in agent.get("inputs", []):
                source = producer_by_path.get(path)
                if not source or source == target:
                    continue
                edge = ensure_edge(source, target, "data")
                if path not in edge["transfers"]:
                    edge["transfers"].append(path)
        return {"nodes": nodes, "edges": list(edges.values())}

    def _agent(
        self,
        run_dir: Path,
        state: dict[str, Any],
        workflow: dict[str, Any],
        stage_id: str,
        stage: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        stage_state = state.get("stages", {}).get(stage_id, {})
        task_id = stage.get("task_id") or stage_state.get("task_id")
        task = read_json(run_dir / "inbox" / f"{task_id}.task.json", {}) if task_id else {}
        result_path = run_dir / "outbox" / f"{task_id}.result.json" if task_id else None
        result = read_json(result_path, {}) if result_path else {}
        profile = (
            task.get("agent_profile")
            or stage_state.get("agent_profile")
            or stage.get("profile")
            or stage.get("agent")
        )
        assignments = self._assignments(stage_id, stage_state, events, state.get("cao_session"))
        findings = result.get("findings", []) if isinstance(result, dict) else []
        statuses = Counter(
            finding.get("status", "unknown")
            for finding in findings
            if isinstance(finding, dict)
        )
        artifact_path = stage.get("artifact") or result.get("artifacts", [None])[0]
        outputs: list[dict[str, Any]] = []
        if artifact_path:
            output = self._deliverable(run_dir, artifact_path, "artifact", stage_id)
            if output:
                outputs.append(output)
        if result_path and result_path.is_file():
            output = self._deliverable(
                run_dir, str(result_path.relative_to(run_dir)), "result", stage_id
            )
            if output:
                outputs.append(output)
        return {
            "stage": stage_id,
            "task_id": task_id,
            "profile": profile,
            "provider": stage_state.get("provider") or stage.get("provider") or workflow.get("provider"),
            "status": stage_state.get("status", result.get("status", "unknown")),
            "depends_on": stage.get("depends_on", []),
            "inputs": task.get("inputs", stage.get("inputs", [])),
            "started_at": stage_state.get("started_at"),
            "finished_at": stage_state.get("validated_at") or stage_state.get("finished_at"),
            "assignments": assignments,
            "result_summary": result.get("summary"),
            "finding_count": len(findings),
            "finding_statuses": dict(statuses),
            "deliverables": outputs,
        }

    def _assignments(
        self,
        stage_id: str,
        stage_state: dict[str, Any],
        events: list[dict[str, Any]],
        default_session: str | None,
    ) -> list[dict[str, Any]]:
        raw: list[dict[str, Any]] = []
        for event in events:
            if event.get("stage") != stage_id:
                continue
            if event.get("event") == "stage_assigned_by_cao" and event.get("terminal_id"):
                raw.append(
                    {
                        "terminal_id": event["terminal_id"],
                        "session": default_session,
                        "attempt": event.get("attempt"),
                        "started_at": event.get("ts"),
                    }
                )
            elif event.get("event") == "stage_started" and event.get("session"):
                raw.append(
                    {
                        "terminal_id": None,
                        "session": event["session"],
                        "attempt": event.get("attempt"),
                        "started_at": event.get("ts"),
                    }
                )
        if not raw and (stage_state.get("terminal_id") or stage_state.get("session")):
            raw.append(
                {
                    "terminal_id": stage_state.get("terminal_id"),
                    "session": stage_state.get("session") or default_session,
                    "attempt": stage_state.get("attempt"),
                    "started_at": stage_state.get("started_at"),
                }
            )
        seen: set[tuple[Any, Any]] = set()
        assignments: list[dict[str, Any]] = []
        for item in raw:
            key = (item.get("terminal_id"), item.get("session"))
            if key in seen:
                continue
            seen.add(key)
            metadata = self._terminal_metadata(item.get("terminal_id"))
            assignments.append(
                {
                    **item,
                    "session": metadata.get("session_name") or item.get("session"),
                    "window": metadata.get("window_name"),
                    "provider": metadata.get("provider"),
                    "working_directory": metadata.get("working_directory"),
                    "caller_id": metadata.get("caller_id"),
                    "snapshot_available": metadata.get("snapshot_available", False),
                    "scrollback_available": metadata.get("scrollback_available", False),
                }
            )
        return assignments

    def _terminal_metadata(self, terminal_id: str | None) -> dict[str, Any]:
        if not terminal_id:
            return {}
        snapshot_path = self.terminal_logs / f"{terminal_id}.snapshot.json"
        snapshot = read_json(snapshot_path, {})
        if isinstance(snapshot, dict) and snapshot:
            snapshot["snapshot_available"] = True
            snapshot["scrollback_available"] = (
                self.terminal_logs / f"{terminal_id}.scrollback"
            ).is_file()
            return snapshot
        if self.cao_db.is_file():
            try:
                with sqlite3.connect(self.cao_db) as connection:
                    row = connection.execute(
                        "SELECT id, tmux_session, tmux_window, provider, agent_profile "
                        "FROM terminals WHERE id = ?",
                        (terminal_id,),
                    ).fetchone()
                if row:
                    return {
                        "terminal_id": row[0],
                        "session_name": row[1],
                        "window_name": row[2],
                        "provider": row[3],
                        "agent_profile": row[4],
                        "snapshot_available": False,
                        "scrollback_available": False,
                    }
            except sqlite3.Error:
                pass
        return {}

    def _supervisor(
        self,
        state: dict[str, Any],
        workflow: dict[str, Any],
        agents: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        session = state.get("cao_session")
        if not session:
            return None
        caller_ids = {
            assignment.get("caller_id")
            for agent in agents
            for assignment in agent.get("assignments", [])
            if assignment.get("caller_id")
        }
        assignments = []
        for terminal_id in sorted(caller_ids):
            metadata = self._terminal_metadata(terminal_id)
            assignments.append(
                {
                    "terminal_id": terminal_id,
                    "session": metadata.get("session_name") or session,
                    "window": metadata.get("window_name"),
                    "provider": metadata.get("provider") or workflow.get("provider"),
                    "working_directory": metadata.get("working_directory"),
                    "caller_id": None,
                    "snapshot_available": metadata.get("snapshot_available", False),
                    "scrollback_available": metadata.get("scrollback_available", False),
                }
            )
        return {
            "stage": "flow-supervisor",
            "task_id": None,
            "profile": f"{state.get('workflow', workflow.get('name'))}-supervisor",
            "provider": workflow.get("provider"),
            "status": state.get("status"),
            "depends_on": [],
            "inputs": ["manifest.json", "state.json", "workflow.json"],
            "started_at": state.get("created_at"),
            "finished_at": state.get("finished_at"),
            "assignments": assignments or [{"terminal_id": None, "session": session}],
            "result_summary": "Coordinates CAO worker terminals and advances Hutch artifact gates.",
            "finding_count": 0,
            "finding_statuses": {},
            "deliverables": [],
        }

    def _flow_deliverables(
        self, run_dir: Path, workflow: dict[str, Any]
    ) -> list[dict[str, Any]]:
        deliverables: list[dict[str, Any]] = []
        stages = workflow.get("stages", [])
        final_stage = stages[-1].get("id") if stages else None
        stage_paths: set[str] = set()
        for stage in stages:
            stage_id = stage.get("id")
            task_id = stage.get("task_id")
            paths = [stage.get("artifact")]
            if task_id:
                paths.append(f"outbox/{task_id}.result.json")
            for relative in paths:
                if not relative:
                    continue
                stage_paths.add(relative)
                value = self._deliverable(
                    run_dir,
                    relative,
                    "final" if stage_id == final_stage else "intermediate",
                    stage_id,
                )
                if value:
                    deliverables.append(value)
        for relative, kind in (
            ("findings.jsonl", "aggregate"),
            ("state.json", "control"),
            ("events.jsonl", "control"),
            ("manifest.json", "control"),
            ("workflow.json", "control"),
            ("shared/source-fingerprint.json", "evidence"),
            ("shared/snapshot-manifest.json", "evidence"),
        ):
            if relative in stage_paths:
                continue
            value = self._deliverable(run_dir, relative, kind, None)
            if value:
                deliverables.append(value)
        return deliverables

    @staticmethod
    def _deliverable(
        run_dir: Path, relative: str, kind: str, stage: str | None
    ) -> dict[str, Any] | None:
        path = (run_dir / relative).resolve()
        try:
            path.relative_to(run_dir)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return {
            "path": str(path.relative_to(run_dir)),
            "kind": kind,
            "stage": stage,
            "bytes": path.stat().st_size,
            "content": content,
        }
