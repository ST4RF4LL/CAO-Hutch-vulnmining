# CAO-native DJL vulnerability-mining flow

Hutch is the compiler and durable control plane around CAO. It generates Agent profiles and a native CAO flow; CAO owns flow registration, the supervisor session, worker terminals, dispatch, and lifecycle. Hutch stores the DAG contract, immutable source snapshot, artifact gates, findings, events, and resumable state.

The previous `djl-security-review` implementation in `scripts/run_cao_flow.py` is retained as a feasibility experiment. It launches CAO agents but schedules the DAG in an external Python process, so it is not the production execution path and does not appear in CAO Web as a flow.

## Generated CAO topology

`workflows/djl-vulnerability-mining.yaml` compiles into one native flow and eight profiles:

- `djl-vulnerability-mining-supervisor`
- architecture analyst and threat modeler
- Java/JVM, native/JNI, and supply-chain auditors
- independent vulnerability validator
- final report writer

When the flow runs, CAO creates session `cao-flow-djl-vulnerability-mining`. The supervisor delegates every stage through CAO MCP `assign`, so the worker terminals are part of that CAO session and visible in CAO Web. Hutch polls for the result file because CAO's TUI completion detection can be provider/version dependent. The supervisor cannot perform the audits itself and cannot advance a stage until `hutch_flow_state.py` validates the result contract and artifact headings.

The static-analysis pipeline is:

1. architecture and attack-surface mapping;
2. threat modeling and prioritized audit planning;
3. Java/JVM audit;
4. JNI/native and native-loading audit;
5. dependency, build, artifact, model, plugin, and configuration audit;
6. independent candidate validation and deduplication;
7. final evidence-linked report.

Audit workers use an immutable text/source snapshot of `/Users/wh4lter/Workspace/djl_test/djl`. They cannot use the network, run builds/tests, execute DJL, or write to the target. Finalization compares the target Git fingerprint with the pre-run fingerprint.

## Generate and register

CAO must be running with `CAO_ENABLE_WORKING_DIRECTORY=true`.

```bash
python3 scripts/generate_cao_native_flow.py \
  workflows/djl-vulnerability-mining.yaml \
  --install --replace
```

This uses CAO's public `install` and `flow` CLI commands. It does not edit the CAO checkout. The generated bundle is under `generated/djl-vulnerability-mining/` and the registered flow is disabled by default to prevent an expensive scheduled audit from starting unexpectedly.

Open CAO Web at `http://127.0.0.1:9889`, choose **Flows**, and locate `djl-vulnerability-mining`. Use **Run Now** for a manual run. Enabling the flow activates its cron schedule; the placeholder annual schedule should be changed deliberately before enabling.

Equivalent CLI command:

```bash
uv --directory /Users/wh4lter/Workspace/lab/cli-agent-orchestrator \
  run cao flow run djl-vulnerability-mining
```

## Evidence and status

Each invocation creates `runs/djl-vulnerability-mining-<timestamp>/` containing:

- `manifest.json`, `workflow.json`, and `state.json`;
- the immutable `shared/target-snapshot/` and source fingerprint;
- task cards under `inbox/` and committed results under `outbox/`;
- seven Markdown artifacts and the final report;
- `events.jsonl` and aggregated `findings.jsonl`.

CAO Web shows the registered flow and live CAO sessions/terminals. It does not currently model a multi-stage DAG inside one native flow record. Detailed stage status therefore remains in Hutch's `state.json`; this preserves the no-CAO-patch boundary.

## Completed-flow dashboard

CAO's flow table stores definitions and one `last_run` timestamp, not durable run instances. Its session API enumerates live tmux sessions, and normal terminal cleanup removes worker database rows. Hutch therefore provides a read-only archive dashboard backed by the durable `runs/` evidence and optional CAO terminal snapshots:

```bash
python3 scripts/run_hutch_dashboard.py
```

Open `http://127.0.0.1:9890`. The left panel lists completed Flow instances. The right panel shows Flow metadata, every supervisor/worker Agent, recovered CAO session/terminal/window identifiers, retries, summaries, and the original text of intermediate and final deliverables. The validation version deliberately has no mutation endpoints and does not patch CAO.
