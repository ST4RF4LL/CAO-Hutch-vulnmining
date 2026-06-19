# DJL security review flow experiment

Date: 2026-06-19

## Outcome

The Rabbit Hutch prototype completed a durable three-stage flow against a source-only
snapshot of DJL:

1. architecture analysis;
2. threat analysis;
3. bounded code audit of the highest-priority model-bundle execution path.

All three stages passed the Markdown artifact and `hutch.result.v1` JSON gates. The
completed runtime is under `runs/djl-security-review-001/` and is intentionally ignored
by Git because its Atlas database is approximately 300 MB.

## Runtime evidence

- Snapshot: 2,016 files / 16,528,648 bytes copied from the working tree.
- Atlas full index: 1,608 code files, 25,712 symbols, 161,341 references, and 169,236
  edges. Java capability was reported as `dataflow_full` with a 75% confidence floor.
- Architecture artifact: `runs/djl-security-review-001/artifacts/architecture.md`.
- Threat artifact: `runs/djl-security-review-001/artifacts/threat-model.md`.
- Audit artifact: `runs/djl-security-review-001/artifacts/code-audit.md`.
- Durable state: `runs/djl-security-review-001/state.json` reports `completed`.

Architecture ran under CAO's Codex provider. Threat analysis and code audit ran under
the imported OpenCode/DeepSeek setup. Each retry used a unique session name so delayed
cleanup from a failed provider initialization could not kill a later attempt.

## Audit result interpretation

The bounded AP-1 review emitted three **candidate** findings and one audit lead:

- F-01: model metadata can select a translator class loaded from model-local classes or
  JARs;
- F-02: the `blockFactory` argument reaches model-local class loading;
- F-03: model-local Java source can reach `JavaCompiler.run` before class loading;
- AL-01: reflective native loading exists, but its caller and parameter provenance were
  not established in scope.

These are not confirmed vulnerabilities. Exploitability depends on whether an
application treats untrusted model bundles as data-only inputs, how the relevant
translator factory is selected, and deployment classloader/runtime policy. F-01 and
F-03 still require confirmation of the upstream factory-delegation chain. No dynamic
validation or exploit construction was performed.

AP-2 through AP-6 remain unreviewed: archive/cache safety, outbound URL/SSRF paths,
native/JNI parsing and loading, provider loading, and parser/allocation controls.

## Adapter findings

OpenCode 1.16.2 renders its idle footer as cursor-addressed fragments. Unmodified CAO
can therefore leave the terminal at `unknown` and reap the session after its 120-second
initialization timeout even though the composited terminal is ready.

The prototype handles this outside CAO:

1. confirm the idle footer through CAO's full terminal-output API;
2. mirror the verified `ctrl+p commands` marker into that terminal's CAO FIFO;
3. dispatch through CAO's terminal-input API;
4. treat the file result contract, not terminal status, as completion authority.

The FIFO pulse is an experimental private-interface dependency. It should be replaced
by a public CAO status/event override or removed once OpenCode status detection is
reliable. No CAO source patch, commit, push, or pull request was made.

## Integrity

The original DJL checkout remained unchanged throughout the flow:

- HEAD: `1d7c627697d2a2a5ae8fea8c9ecdd4b8cb187545`
- pre-existing status paths: 2,103
- status fingerprint before and after:
  `4cf1a8fee72a7c297de61fb84357e48a17d0e265fdff601934b9cc0096030619`

The CAO source checkout remained clean on `main...origin/main`. All experiment sessions
were shut down, and the CAO health endpoint remained healthy.
