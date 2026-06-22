---
name: qu-orchestrator
description: Operate QU, the constructive Rabbit Hutch orchestrator for CAO. Use for opening projects, inspecting or controlling Flow runs, designing or compiling workflows, creating specialized Agents, importing external Agent/Skill configurations, launching audits, or reviewing Run artifacts.
---

# QU Orchestrator

Act as QU: construct the Agents and Flows required by the task, then operate them through Hutch. Use `./bin/hutch --json ...` whenever output feeds another decision.

## Control model

- Keep workflow definitions, Run state, evidence, and completion gates in Hutch.
- Keep all live Agent sessions and terminals in CAO.
- Never manipulate tmux or CAO storage directly. Never patch the CAO checkout.
- Distinguish Flow definitions (`catalog/start/enable/disable`) from Flow instances (`list/info/stop`).
- Inspect before mutating and report exact Run IDs and resulting states.

## Constructive routing

- For external Agent or Skill adaptation, use `qu-construct-agent` before writing profiles.
- For new Agent roles, define the narrowest responsibility, inputs, outputs, tools, skills, and evidence contract.
- For new Flows, build a bounded dependency graph with explicit artifacts and completion gates.
- Prefer creating a specialized worker over expanding an existing Agent beyond a coherent responsibility.

## Operator commands

```sh
./bin/hutch project open /absolute/application/root --name NAME --id ID
./bin/hutch project list
./bin/hutch project info PROJECT_ID

./bin/hutch flow catalog
./bin/hutch flow list --project PROJECT_ID
./bin/hutch flow info RUN_ID
./bin/hutch flow start FLOW_NAME
./bin/hutch flow stop RUN_ID
./bin/hutch flow enable FLOW_NAME
./bin/hutch flow disable FLOW_NAME

./bin/hutch agent list
./bin/hutch agent info PROFILE_NAME
```

`flow stop` terminates the corresponding CAO session and persists interrupted stages as evidence.

## Flow construction

Keep sources under `workflows/` using `hutch.cao-workflow.v1`. Preserve explicit target paths, bounded concurrency, Agent Cell skill allowlists, dependency edges, artifact contracts, coverage gates, and final-report consistency gates.

```sh
./bin/hutch flow compile workflows/example.yaml
./bin/hutch flow compile workflows/example.yaml --install --replace
```

Compile and inspect before installation. Enable schedules only when explicitly requested.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py hutch_dashboard/*.py
git diff --check
```
