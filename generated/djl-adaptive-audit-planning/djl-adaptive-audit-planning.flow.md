---
name: djl-adaptive-audit-planning
schedule: "0 0 1 1 *"
agent_profile: djl-adaptive-audit-planning-supervisor
provider: opencode_cli
script: ./prepare-run.sh
---
# CAO-native Hutch run

CAO owns this flow and the current session. Hutch prepared run `[[run_id]]` at `[[run_dir]]`.

Read these files first:

- manifest: `[[manifest]]`
- durable state: `[[state_file]]`
- immutable source snapshot: `[[target_snapshot]]`

Execute these exact dependency batches in order. Concurrency limit: 1.

Batch 1 (launch at most 1 workers):
- `audit-planning`: profile `djl-adaptive-audit-planning-audit-planner`, workspace `[[run_dir]]/agents/audit-planner/workspace`, task `[[run_dir]]/inbox/P-0001.task.json`, dependencies: none, preflight: none.

For every batch:

1. Read every task JSON in the batch and confirm its dependencies are `done` in `[[state_file]]`. Run a preflight only when that stage's explicit `preflight` value is not `none`; never infer or invent a preflight. Stop if an explicit preflight fails.
2. For every stage in the batch, run `python3 /Users/wh4lter/Workspace/Qu-Studio/scripts/cao_assign_cell.py <absolute-task-path>`. This Hutch launcher validates the Agent Cell contract, creates the worker through the CAO API in the current CAO session, forces the exact Cell `working_directory`, and submits the task. Record each returned `terminal_id`; do not await yet. Do not substitute CAO MCP `assign`.
3. Immediately after each assignment run `python3 /Users/wh4lter/Workspace/Qu-Studio/scripts/hutch_flow_state.py start "[[run_dir]]" <stage-id> <terminal-id>`.
4. After all workers in the batch are running, await each with `python3 /Users/wh4lter/Workspace/Qu-Studio/scripts/hutch_flow_state.py await "[[run_dir]]" <stage-id> --timeout 1800`. This file gate, not CAO's TUI status, is completion authority.
5. After successful validation, call CAO MCP `delete_terminal` for that worker terminal.
6. If await/validation fails, delete the failed terminal, remove only that stage's invalid result file if one exists, and run the same assignment once more after reporting the validator error. Maximum attempts: 2. Stop if the final attempt fails.

After all stages validate, run:

`python3 /Users/wh4lter/Workspace/Qu-Studio/scripts/hutch_flow_state.py finalize "[[run_dir]]"`

Your final response must state the run directory, final state, final report path `[[run_dir]]/artifacts/audit-plan.md`, and that CAO Web owns the visible flow/session records. Do not substitute your own analysis for any missing worker output.
