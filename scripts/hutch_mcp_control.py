#!/usr/bin/env python3
"""Typed, context-bounded control surface shared by the Hutch MCP tools."""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from hutch_cli import HutchCliError, HutchClient
from adaptive_audit import atomic_json, load_json
from generate_cao_native_flow import install_bundle, load_and_validate, write_output
from hutch_campaign import DEFAULT_SKILL_ROOTS, mining_workflow, planning_workflow, recon_workflow
from hutch_paths import default_cao_repo


ROOT = Path(__file__).resolve().parents[1]
try:
    DEFAULT_CAO_REPO = default_cao_repo()
except RuntimeError:
    DEFAULT_CAO_REPO = ROOT.parent / "cli-agent-orchestrator"
DEFAULT_MCP_SKILL_ROOTS = (
    [Path(os.environ["HUTCH_SKILL_ROOT"]).expanduser().resolve()]
    if os.environ.get("HUTCH_SKILL_ROOT")
    else DEFAULT_SKILL_ROOTS
)


class HutchMcpControl:
    def __init__(self, base_url: str) -> None:
        self.client = HutchClient(base_url)

    @staticmethod
    def _success(key: str, value: Any) -> dict[str, Any]:
        return {"success": True, key: value}

    @staticmethod
    def _failure(error: Exception) -> dict[str, Any]:
        return {"success": False, "message": str(error)}

    def _call(self, key: str, action: Callable[[], Any]) -> dict[str, Any]:
        try:
            return self._success(key, action())
        except (HutchCliError, OSError, ValueError, RuntimeError) as error:
            return self._failure(error)

    @staticmethod
    def _without_artifact_content(value: Any) -> Any:
        result = copy.deepcopy(value)

        def strip(item: Any) -> None:
            if isinstance(item, dict):
                if "path" in item and "content" in item:
                    item.pop("content", None)
                for child in item.values():
                    strip(child)
            elif isinstance(item, list):
                for child in item:
                    strip(child)

        strip(result)
        return result

    def health(self) -> dict[str, Any]:
        return self._call("health", lambda: self.client.get("/api/health"))

    def list_projects(self) -> dict[str, Any]:
        return self._call("projects", lambda: self.client.get("/api/projects"))

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._call(
            "project", lambda: self.client.get(f"/api/projects/{quote(project_id, safe='')}")
        )

    def open_project(
        self, path: str, name: str | None = None, project_id: str | None = None
    ) -> dict[str, Any]:
        resolved = str(Path(path).expanduser().resolve(strict=False))
        return self._call(
            "project",
            lambda: self.client.post(
                "/api/projects/open",
                {"path": resolved, "name": name or None, "id": project_id or None},
            ),
        )

    def list_campaigns(self, status: str | None = None) -> dict[str, Any]:
        def action() -> list[dict[str, Any]]:
            values = self.client.get("/api/campaigns")
            if status:
                values = [item for item in values if item.get("status") == status]
            return values

        return self._call("campaigns", action)

    def get_campaign(
        self, instance_id: str, include_artifact_content: bool = False
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            value = self.client.get(f"/api/campaigns/{quote(instance_id, safe='')}")
            return value if include_artifact_content else self._without_artifact_content(value)

        return self._call("campaign", action)

    def list_flow_runs(
        self, project_id: str | None = None, status: str | None = None
    ) -> dict[str, Any]:
        def action() -> list[dict[str, Any]]:
            values = self.client.get("/api/runs")
            if project_id:
                values = [
                    item
                    for item in values
                    if (item.get("project") or {}).get("id") == project_id
                ]
            if status:
                values = [item for item in values if item.get("status") == status]
            return values

        return self._call("runs", action)

    def get_flow_run(
        self, run_id: str, include_artifact_content: bool = False
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            value = self.client.get(f"/api/runs/{quote(run_id, safe='')}")
            return value if include_artifact_content else self._without_artifact_content(value)

        return self._call("run", action)

    def get_flow_artifact(self, run_id: str, artifact_path: str) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            run = self.client.get(f"/api/runs/{quote(run_id, safe='')}")
            artifact = next(
                (
                    item
                    for item in run.get("deliverables", [])
                    if item.get("path") == artifact_path
                ),
                None,
            )
            if artifact is None:
                raise ValueError(
                    f"artifact is not a deliverable of run {run_id}: {artifact_path}"
                )
            return artifact

        return self._call("artifact", action)

    def get_cao_catalog(self) -> dict[str, Any]:
        return self._call("catalog", lambda: self.client.get("/api/cao/catalog"))

    def start_flow(self, flow_name: str) -> dict[str, Any]:
        return self._call(
            "result",
            lambda: self.client.post(
                f"/api/flows/{quote(flow_name, safe='')}/start"
            ),
        )

    def set_flow_schedule(self, flow_name: str, enabled: bool) -> dict[str, Any]:
        action = "enable" if enabled else "disable"
        return self._call(
            "result",
            lambda: self.client.post(
                f"/api/flows/{quote(flow_name, safe='')}/{action}"
            ),
        )

    def stop_flow_run(self, run_id: str) -> dict[str, Any]:
        return self._call(
            "result",
            lambda: self.client.post(f"/api/runs/{quote(run_id, safe='')}/stop"),
        )

    def _install_campaign_workflow(
        self,
        workflow: dict[str, Any],
        *,
        replace_existing: bool,
        start: bool,
    ) -> dict[str, Any]:
        workflow_path = ROOT / "workflows" / f"{workflow['name']}.generated.json"
        output = ROOT / "generated" / workflow["name"]
        atomic_json(workflow_path, workflow)
        validated = load_and_validate(workflow_path)
        manifest = write_output(
            workflow_path, validated, output, DEFAULT_CAO_REPO
        )
        install_bundle(
            manifest,
            DEFAULT_CAO_REPO,
            replace=replace_existing,
            disable=True,
        )
        start_result = self.start_flow(workflow["name"]) if start else None
        if start_result and not start_result.get("success"):
            raise RuntimeError(start_result.get("message", "Flow start failed"))
        return {
            "workflow": workflow["name"],
            "phase": workflow["campaign"]["phase"],
            "campaign_id": workflow["campaign"]["id"],
            "workflow_path": str(workflow_path),
            "bundle": manifest,
            "installed": True,
            "started": start,
            "start_result": start_result,
        }

    def create_audit_campaign(
        self,
        target: str,
        campaign_id: str,
        workflow_name: str | None = None,
        start: bool = True,
        replace_existing: bool = True,
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            workflow = recon_workflow(
                Path(target),
                campaign_id,
                name=workflow_name,
                cao_repo=DEFAULT_CAO_REPO,
                skill_roots=DEFAULT_MCP_SKILL_ROOTS,
            )
            return self._install_campaign_workflow(
                workflow, replace_existing=replace_existing, start=start
            )

        return self._call("campaign", action)

    def advance_audit_campaign(
        self,
        upstream_run_id: str,
        workflow_name: str | None = None,
        start: bool = True,
        replace_existing: bool = True,
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", upstream_run_id):
                raise ValueError(f"invalid upstream run id: {upstream_run_id!r}")
            run_dir = (ROOT / "runs" / upstream_run_id).resolve()
            if run_dir.parent != (ROOT / "runs").resolve() or not run_dir.is_dir():
                raise ValueError(f"upstream run not found: {upstream_run_id}")
            state = load_json(run_dir / "state.json")
            campaign = state.get("campaign") or {}
            phase = campaign.get("phase")
            if phase == "recon":
                workflow = planning_workflow(
                    run_dir,
                    name=workflow_name,
                    cao_repo=DEFAULT_CAO_REPO,
                    skill_roots=DEFAULT_MCP_SKILL_ROOTS,
                )
            elif phase == "planning":
                workflow = mining_workflow(
                    run_dir,
                    name=workflow_name,
                    cao_repo=DEFAULT_CAO_REPO,
                    skill_roots=DEFAULT_MCP_SKILL_ROOTS,
                )
            else:
                raise ValueError(
                    f"run phase must be recon or planning, got {phase!r}"
                )
            return self._install_campaign_workflow(
                workflow, replace_existing=replace_existing, start=start
            )

        return self._call("campaign", action)
