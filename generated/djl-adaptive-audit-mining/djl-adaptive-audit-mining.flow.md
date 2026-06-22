---
name: djl-adaptive-audit-mining
schedule: "0 0 1 1 *"
agent_profile: djl-adaptive-audit-mining-supervisor
provider: opencode_cli
script: ./prepare-run.sh
---
# CAO-native Hutch run

CAO owns this flow and the current session. Hutch prepared run `[[run_id]]` at `[[run_dir]]`.

Read these files first:

- manifest: `[[manifest]]`
- durable state: `[[state_file]]`
- immutable source snapshot: `[[target_snapshot]]`

Execute these exact dependency batches in order. Concurrency limit: 8.

Batch 1 (launch at most 8 workers):
- `audit-001`: profile `djl-adaptive-audit-mining-miner-001`, workspace `[[run_dir]]/agents/miner-001/workspace`, task `[[run_dir]]/inbox/A-0001.task.json`, dependencies: none, preflight: none.
- `audit-002`: profile `djl-adaptive-audit-mining-miner-002`, workspace `[[run_dir]]/agents/miner-002/workspace`, task `[[run_dir]]/inbox/A-0002.task.json`, dependencies: none, preflight: none.
- `audit-003`: profile `djl-adaptive-audit-mining-miner-003`, workspace `[[run_dir]]/agents/miner-003/workspace`, task `[[run_dir]]/inbox/A-0003.task.json`, dependencies: none, preflight: none.
- `audit-004`: profile `djl-adaptive-audit-mining-miner-004`, workspace `[[run_dir]]/agents/miner-004/workspace`, task `[[run_dir]]/inbox/A-0004.task.json`, dependencies: none, preflight: none.
- `audit-005`: profile `djl-adaptive-audit-mining-miner-005`, workspace `[[run_dir]]/agents/miner-005/workspace`, task `[[run_dir]]/inbox/A-0005.task.json`, dependencies: none, preflight: none.
- `audit-006`: profile `djl-adaptive-audit-mining-miner-006`, workspace `[[run_dir]]/agents/miner-006/workspace`, task `[[run_dir]]/inbox/A-0006.task.json`, dependencies: none, preflight: none.
- `audit-007`: profile `djl-adaptive-audit-mining-miner-007`, workspace `[[run_dir]]/agents/miner-007/workspace`, task `[[run_dir]]/inbox/A-0007.task.json`, dependencies: none, preflight: none.
- `audit-008`: profile `djl-adaptive-audit-mining-miner-008`, workspace `[[run_dir]]/agents/miner-008/workspace`, task `[[run_dir]]/inbox/A-0008.task.json`, dependencies: none, preflight: none.
Batch 2 (launch at most 8 workers):
- `audit-009`: profile `djl-adaptive-audit-mining-miner-009`, workspace `[[run_dir]]/agents/miner-009/workspace`, task `[[run_dir]]/inbox/A-0009.task.json`, dependencies: none, preflight: none.
- `audit-010`: profile `djl-adaptive-audit-mining-miner-010`, workspace `[[run_dir]]/agents/miner-010/workspace`, task `[[run_dir]]/inbox/A-0010.task.json`, dependencies: none, preflight: none.
- `audit-011`: profile `djl-adaptive-audit-mining-miner-011`, workspace `[[run_dir]]/agents/miner-011/workspace`, task `[[run_dir]]/inbox/A-0011.task.json`, dependencies: none, preflight: none.
- `audit-012`: profile `djl-adaptive-audit-mining-miner-012`, workspace `[[run_dir]]/agents/miner-012/workspace`, task `[[run_dir]]/inbox/A-0012.task.json`, dependencies: none, preflight: none.
- `audit-013`: profile `djl-adaptive-audit-mining-miner-013`, workspace `[[run_dir]]/agents/miner-013/workspace`, task `[[run_dir]]/inbox/A-0013.task.json`, dependencies: none, preflight: none.
- `audit-014`: profile `djl-adaptive-audit-mining-miner-014`, workspace `[[run_dir]]/agents/miner-014/workspace`, task `[[run_dir]]/inbox/A-0014.task.json`, dependencies: none, preflight: none.
- `audit-015`: profile `djl-adaptive-audit-mining-miner-015`, workspace `[[run_dir]]/agents/miner-015/workspace`, task `[[run_dir]]/inbox/A-0015.task.json`, dependencies: none, preflight: none.
- `audit-016`: profile `djl-adaptive-audit-mining-miner-016`, workspace `[[run_dir]]/agents/miner-016/workspace`, task `[[run_dir]]/inbox/A-0016.task.json`, dependencies: none, preflight: none.
Batch 3 (launch at most 8 workers):
- `coverage-gate`: profile `djl-adaptive-audit-mining-coverage-reviewer`, workspace `[[run_dir]]/agents/coverage-reviewer/workspace`, task `[[run_dir]]/inbox/G-0001.task.json`, dependencies: audit-001, audit-002, audit-003, audit-004, audit-005, audit-006, audit-007, audit-008, audit-009, audit-010, audit-011, audit-012, audit-013, audit-014, audit-015, audit-016, preflight: `python3 /Users/wh4lter/Workspace/Qu-Studio/scripts/hutch_flow_state.py coverage "[[run_dir]]" coverage-gate`.
Batch 4 (launch at most 8 workers):
- `finding-validation`: profile `djl-adaptive-audit-mining-finding-validator`, workspace `[[run_dir]]/agents/finding-validator/workspace`, task `[[run_dir]]/inbox/V-0001.task.json`, dependencies: coverage-gate, preflight: none.
Batch 5 (launch at most 8 workers):
- `final-report`: profile `djl-adaptive-audit-mining-report-writer`, workspace `[[run_dir]]/agents/report-writer/workspace`, task `[[run_dir]]/inbox/R-0001.task.json`, dependencies: finding-validation, preflight: none.

For every batch:

1. Read every task JSON in the batch and confirm its dependencies are `done` in `[[state_file]]`. Run a preflight only when that stage's explicit `preflight` value is not `none`; never infer or invent a preflight. Stop if an explicit preflight fails.
2. For every stage in the batch, run `python3 /Users/wh4lter/Workspace/Qu-Studio/scripts/cao_assign_cell.py <absolute-task-path>`. This Hutch launcher validates the Agent Cell contract, creates the worker through the CAO API in the current CAO session, forces the exact Cell `working_directory`, and submits the task. Record each returned `terminal_id`; do not await yet. Do not substitute CAO MCP `assign`.
3. Immediately after each assignment run `python3 /Users/wh4lter/Workspace/Qu-Studio/scripts/hutch_flow_state.py start "[[run_dir]]" <stage-id> <terminal-id>`.
4. After all workers in the batch are running, await each with `python3 /Users/wh4lter/Workspace/Qu-Studio/scripts/hutch_flow_state.py await "[[run_dir]]" <stage-id> --timeout 1800`. This file gate, not CAO's TUI status, is completion authority.
5. After successful validation, call CAO MCP `delete_terminal` for that worker terminal.
6. If await/validation fails, delete the failed terminal, remove only that stage's invalid result file if one exists, and run the same assignment once more after reporting the validator error. Maximum attempts: 2. Stop if the final attempt fails.

After all stages validate, run:

`python3 /Users/wh4lter/Workspace/Qu-Studio/scripts/hutch_flow_state.py finalize "[[run_dir]]"`

Your final response must state the run directory, final state, final report path `[[run_dir]]/artifacts/final-report.md`, and that CAO Web owns the visible flow/session records. Do not substitute your own analysis for any missing worker output.
