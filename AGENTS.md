# Hutch control-plane guidance

This repository is the Rabbit Hutch control plane for CAO. The orchestrator is named **QU**. When a request concerns project registration, Agent construction, workflow design, scheduling, Flow lifecycle, or Run evidence, use the `qu-orchestrator` skill. Use `qu-construct-agent` whenever external Agent or Skill configurations are adapted into CAO profiles.

- Use `./bin/hutch` as the stable operator interface. Prefer `--json` when consuming output programmatically.
- Hutch owns workflow definitions, durable Run state, evidence, and completion gates. CAO owns live Agent sessions and terminals.
- Do not patch the CAO checkout. Compile and install profiles/flows through Hutch.
- Do not bypass Hutch by operating tmux directly or editing a Run's `state.json` manually.
- Treat audited target repositories as read-only unless the user explicitly requests remediation.
- Validate Python changes with `python3 -m unittest discover -s tests -v`, `python3 -m py_compile`, and `git diff --check`.
