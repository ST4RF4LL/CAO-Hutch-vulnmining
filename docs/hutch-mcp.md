# Hutch MCP Server

`bin/hutch-mcp` exposes Hutch's structured local HTTP control plane over MCP stdio. It requires the Hutch Dashboard at `HUTCH_URL` and uses the local CAO Python environment only to provide the FastMCP runtime dependency.

## Start and inspect

```sh
curl -fsS http://127.0.0.1:9890/api/health
./bin/hutch-mcp
```

The second command is an MCP stdio server, so it waits for an MCP client rather than printing an interactive prompt. Project-local Codex and OpenCode configurations already register it as `hutch`.

Environment variables:

- `HUTCH_URL`: Hutch Dashboard API, default `http://127.0.0.1:9890`.
- `CAO_REPO`: local CAO checkout whose environment supplies FastMCP.

## Tools

| Tool | Effect |
| --- | --- |
| `hutch_health` | Check the Hutch API. |
| `list_projects` / `get_project` | Inspect application roots and service trees. |
| `open_project` | Register an application root. |
| `list_campaigns` / `get_campaign` | Inspect aggregate recon → planning → mining Campaigns. |
| `list_flow_runs` / `get_flow_run` | Inspect durable CAO child Flow state, Agents and sessions. |
| `get_flow_artifact` | Read one exact persisted report or machine artifact. |
| `get_cao_catalog` | List CAO Flows, profiles and providers through Hutch. |
| `start_flow` | Start a registered CAO Flow through Hutch. |
| `set_flow_schedule` | Enable or disable one Flow schedule. |
| `stop_flow_run` | Stop a live Flow and preserve its evidence. |
| `create_audit_campaign` | Generate, install and optionally start a generic recon Flow for a Git checkout. |
| `advance_audit_campaign` | Advance a completed recon Run to planning, or planning Run to adaptive mining. |

`get_campaign` and `get_flow_run` omit artifact bodies by default. Use `get_flow_artifact` for a precise path; set `include_artifact_content=true` only for deliberately small runs.

## Security boundary

The MCP server does not expose arbitrary shell, direct tmux operations, CAO database access, Run deletion, Run-state rewriting, or arbitrary workflow compilation. The two Campaign construction tools compile only Hutch's fixed recon/planning/mining contracts and require a completed upstream Run before advancing. CAO remains the only Agent runtime and Hutch's validated persisted state remains the completion authority.

An MCP client can drive a full audit with three bounded mutations:

1. `create_audit_campaign(target, campaign_id)`.
2. After the returned recon Run is `completed`, call `advance_audit_campaign(recon_run_id)`.
3. After planning is `completed`, call `advance_audit_campaign(planning_run_id)`.

Custom workflow construction and installation remain explicit CLI operations:

```sh
./bin/hutch flow compile workflows/example.json --install --replace
```
