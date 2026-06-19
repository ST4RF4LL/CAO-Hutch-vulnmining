# CAO + OpenCode multi-agent feasibility result

Date: 2026-06-19

## Outcome

The configuration is importable and individual OpenCode agents run correctly under CAO.
CAO supervisor-to-worker routing also creates the expected worker and delivers its task.
End-to-end blocking `handoff` completion is not yet reliable with the current OpenCode
1.16.2 + DeepSeek transport combination.

## Verified

- CAO server health endpoint responds on `http://127.0.0.1:9889/health`.
- Seven OpenCode roles were converted to CAO profiles and installed with provider
  `opencode_cli`.
- Seventeen OpenCode skills were installed in the CAO skill store.
- `java-source-auditor` completed a bounded read-only review of
  `target/api/src/main/java/ai/djl/engine/rpc` and wrote
  `reports/java-auditor-feasibility.md`.
- `security-audit-orchestrator` connected to the local `cao-mcp-server`, created a
  `security-intel-collector` worker, and delivered the bounded task.
- The worker wrote `reports/cao-handoff-feasibility-v2.md` after inspecting
  `target/api`.
- The DJL checkout's Git status fingerprint remained unchanged:
  `4cf1a8fee72a7c297de61fb84357e48a17d0e265fdff601934b9cc0096030619`
  with 2,103 pre-existing modified paths.

## Compatibility finding

CAO's OpenCode status detector expects a stable raw `ctrl+p commands` footer.
OpenCode 1.16.2 paints the footer with cursor-addressed fragments and continuously
animates TUI chrome. This can delay or miss CAO state transitions. A temporary local
diagnostic change confirmed the cause and was then fully removed. The CAO checkout
remains unmodified; no CAO commit, pull request, or push was made.

The generated supervisor profile uses the local CAO checkout for `cao-mcp-server`.
This avoids the first-run timeout caused by the upstream example's network-dependent
`uvx --from git+...` command.

## Remaining limitation

During the blocking handoff, the worker finished its file artifact but OpenCode
showed an API socket retry before emitting its duration-bearing completion marker.
CAO therefore kept both worker and supervisor in `processing`. Durable Rabbit Hutch
execution must treat artifact/result files as authoritative and record terminal status
as supporting runtime state, not as the sole completion source.

Any workaround for this compatibility gap belongs in the Rabbit Hutch adapter or its
file-based completion protocol, not in a CAO fork.

The candidate findings in the Java auditor report are unvalidated smoke-test output and
must not be treated as confirmed vulnerabilities.
