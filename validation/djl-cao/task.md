# CAO/OpenCode Java auditor feasibility task

Audit target: `target/api/src/main/java/ai/djl/engine/rpc`

This is a bounded, read-only feasibility check, not a full DJL security audit.

Requirements:

1. Confirm that the Java auditor profile and its named skills are available.
2. Inspect the scoped Java package for trust boundaries and security-sensitive data flows.
3. Produce at most three evidence-backed candidate findings. Do not invent reachability.
4. If no defensible finding exists, report that result and list the inspected sinks/guards.
5. Do not edit anything under `target/` and do not perform network actions.
6. Write the result to `reports/java-auditor-feasibility.md`.

