# DJL CAO feasibility workspace

This workspace validates the OpenCode-to-CAO profile conversion against DJL.

- `target` points to the external DJL checkout and must remain read-only.
- `task.md` is the bounded validation task.
- Runtime output belongs in `reports/` and `tmp/`; both are ignored by Git.
- OpenCode agent profiles are generated under `cao-profiles/` at the repository root.

