#!/usr/bin/env python3
"""MCP server for Hutch's structured local control API."""

from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP

from hutch_mcp_control import HutchMcpControl


control = HutchMcpControl(os.environ.get("HUTCH_URL", "http://127.0.0.1:9890"))
mcp = FastMCP(
    "hutch-mcp-server",
    instructions="""
Use Hutch as the control plane and CAO as the only Agent runtime.

Typical audit workflow:
1. health and list_projects
2. create_audit_campaign for a Git checkout; wait until its recon Run is completed
3. advance_audit_campaign on the recon Run; wait for planning to complete; advance again
4. list_flow_runs or list_campaigns to monitor durable state
5. get_campaign for the overall graph and get_flow_run for one CAO child Flow
6. get_flow_artifact to read one exact report without loading every artifact

Never infer completion from terminal disappearance. Trust Hutch Run state, coverage gates,
validated result contracts, and persisted artifacts. This MCP does not expose arbitrary shell,
direct tmux access, CAO database access, or deletion of evidence.
""",
)


@mcp.tool()
def hutch_health() -> dict[str, Any]:
    """Check whether the local Hutch Dashboard API is reachable."""
    return control.health()


@mcp.tool()
def list_projects() -> dict[str, Any]:
    """List configured application projects, service trees, Flow runs, and reports."""
    return control.list_projects()


@mcp.tool()
def get_project(project_id: str) -> dict[str, Any]:
    """Get one Hutch project and its adaptive service tree."""
    return control.get_project(project_id)


@mcp.tool()
def open_project(
    path: str, name: str | None = None, project_id: str | None = None
) -> dict[str, Any]:
    """Register a local application root. Git repositories become service leaves."""
    return control.open_project(path, name, project_id)


@mcp.tool()
def list_campaigns(status: str | None = None) -> dict[str, Any]:
    """List aggregate audit Campaigns; optionally filter by effective status."""
    return control.list_campaigns(status)


@mcp.tool()
def get_campaign(
    instance_id: str, include_artifact_content: bool = False
) -> dict[str, Any]:
    """Get the overall recon→planning→mining graph and child Flows for one Campaign.

    Artifact bodies are omitted by default to protect model context. Use get_flow_artifact
    for a specific report or explicitly opt in only when the Campaign is small.
    """
    return control.get_campaign(instance_id, include_artifact_content)


@mcp.tool()
def list_flow_runs(
    project_id: str | None = None, status: str | None = None
) -> dict[str, Any]:
    """List durable Hutch child Flow runs, optionally filtered by project and status."""
    return control.list_flow_runs(project_id, status)


@mcp.tool()
def get_flow_run(
    run_id: str, include_artifact_content: bool = False
) -> dict[str, Any]:
    """Get one CAO child Flow with its Agent graph, sessions, and artifact metadata."""
    return control.get_flow_run(run_id, include_artifact_content)


@mcp.tool()
def get_flow_artifact(run_id: str, artifact_path: str) -> dict[str, Any]:
    """Read one exact persisted text artifact from a Hutch Flow run."""
    return control.get_flow_artifact(run_id, artifact_path)


@mcp.tool()
def get_cao_catalog() -> dict[str, Any]:
    """List CAO Flow definitions, Agent profiles, and providers visible through Hutch."""
    return control.get_cao_catalog()


@mcp.tool()
def start_flow(flow_name: str) -> dict[str, Any]:
    """Start one registered CAO Flow through Hutch's structured API."""
    return control.start_flow(flow_name)


@mcp.tool()
def set_flow_schedule(flow_name: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable the schedule for one registered CAO Flow."""
    return control.set_flow_schedule(flow_name, enabled)


@mcp.tool()
def stop_flow_run(run_id: str) -> dict[str, Any]:
    """Stop a running Hutch Flow and its CAO session while preserving evidence."""
    return control.stop_flow_run(run_id)


@mcp.tool()
def create_audit_campaign(
    target: str,
    campaign_id: str,
    workflow_name: str | None = None,
    start: bool = True,
    replace_existing: bool = True,
) -> dict[str, Any]:
    """Generate, install, and optionally start the recon Flow for a Git repository.

    The generated Flow uses the generic architecture and threat-intelligence contracts,
    provider=opencode_cli, an immutable source snapshot, isolated Agent Cells, and a
    disabled schedule. campaign_id should uniquely identify the audit activity.
    """
    return control.create_audit_campaign(
        target, campaign_id, workflow_name, start, replace_existing
    )


@mcp.tool()
def advance_audit_campaign(
    upstream_run_id: str,
    workflow_name: str | None = None,
    start: bool = True,
    replace_existing: bool = True,
) -> dict[str, Any]:
    """Generate, install, and optionally start the next Campaign Flow.

    The upstream Run must be completed. A recon Run advances to planning; a planning
    Run advances to adaptive mining. Mining cannot advance further. This tool never
    skips Hutch validation or completion gates.
    """
    return control.advance_audit_campaign(
        upstream_run_id, workflow_name, start, replace_existing
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
