# Hutch Flow Store

Each directory is a self-contained source for one reusable CAO workflow
template:

```text
<flow-id>/
  flow.json
```

`hutch flow from-template` and `hutch flow one_run` resolve these files while
rendering target-specific workflows. During deployment, `bin/hutch-deploy`
copies this default store to `${HUTCH_HOME}/flows_store` when the runtime store
does not already exist.

Rules:

- `flow.json` must use `hutch.cao-workflow-template.v1`.
- The template `id` must match the containing directory name.
- Keep checked-in templates portable: no machine-specific absolute paths.
- Do not store target-specific generated workflows or CAO-registered Flow
  instances here; those belong under `workflows/`, `${HUTCH_HOME}/workflows`,
  `${HUTCH_HOME}/generated`, or CAO runtime state.
- Runtime-local customizations belong in `${HUTCH_HOME}/flows_store` or the path
  configured with `--flows-store` / `HUTCH_FLOWS_STORE`.
