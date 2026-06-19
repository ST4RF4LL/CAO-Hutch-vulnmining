# DJL architecture-to-audit flow

This experiment implements the Rabbit Hutch boundary described in `DESIGN-ori.md`:
CAO runs isolated CLI agents, while this repository owns the workflow DAG, durable
task/result files, artifact gates, target snapshot, and resume state.

## Pipeline

1. `architecture` maps modules, runtime boundaries, entry points, and important flows.
2. `threat-analysis` converts that map into ranked abuse cases and an audit plan.
3. `code-audit` executes the plan and separates evidence-backed findings from leads.

Each stage receives `inbox/T-*.task.json`, writes one Markdown artifact, and commits
completion by writing `outbox/T-*.result.json` last. The runner advances only when the
result contract and required artifact headings validate. CAO terminal state is recorded
for diagnostics but is not the completion authority.

The source checkout is never used as an agent working directory. The runner copies
bounded text/source files into `runs/<run-id>/shared/target-snapshot`, builds an Atlas
full-analysis index there, and verifies the original Git status fingerprint after the
flow.

The executable experiment selects providers per stage. Architecture uses CAO's `codex`
provider; bounded threat analysis and code audit use the imported OpenCode/DeepSeek
setup. The runner keeps provider selection in workflow configuration. Because OpenCode
1.16.2's fragmented TUI footer can prevent unmodified CAO from observing a ready state,
the adapter confirms readiness from CAO's full terminal-output API and dispatches via
CAO's terminal-input API. File artifacts remain authoritative. This workaround stays in
Rabbit Hutch and does not patch CAO.

For OpenCode 1.16.x only, the prototype then mirrors the verified idle footer into that
terminal's CAO FIFO so CAO's pending initialization request does not reap a healthy
session after 120 seconds. This is deliberately isolated compatibility code and a
private-interface dependency; replace it with a public status-override/event API or
remove it when CAO/OpenCode status detection is reliable.

## Run

CAO must already be healthy on `127.0.0.1:9889` and must have been started with
`CAO_ENABLE_WORKING_DIRECTORY=true`.

```bash
python3 scripts/run_cao_flow.py workflows/djl-security-review.yaml \
  --run-id djl-security-review-001
```

Use `--prepare-only` to stop after snapshot/index/task preparation. Resume a prepared
or interrupted run with the same arguments plus `--resume`.

Runtime material is written below `runs/` and ignored by Git. The three CAO profiles
are installed through CAO's public CLI; the CAO source checkout is not edited.
