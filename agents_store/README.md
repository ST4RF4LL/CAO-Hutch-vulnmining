# Hutch Agent Store

Each directory is a self-contained, versioned source for one CAO Agent role:

```text
<role>/
  AGENTS.md
  manifest.json
  mcp.json
  skills/<skill>/SKILL.md
```

`hutch flow one_run` resolves these files while rendering the workflow. Generated
profiles therefore use the checked-in instructions, role-local Skills, and MCP
declarations instead of whichever global configuration happens to exist on the
operator's machine.

Rules:

- `manifest.json` is the role inventory and must list every copied Skill.
- `mcp.json` may contain only reviewed local stdio servers. Do not store
  credentials, headers, remote endpoints, or machine-specific absolute paths.
- `${CAO_REPO}` is resolved by Hutch when compiling a profile.
- Agent Cells copy only the Skills declared for their role.
- `provenance-lock.json` records hashes and source/license evidence for the
  redistributed role assets.
