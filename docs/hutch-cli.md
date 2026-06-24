# Hutch CLI and Agent configuration

`bin/hutch` is the stable control interface shared by human operators, Codex, and OpenCode. It talks to the Hutch Dashboard API at `HUTCH_URL` (default `http://127.0.0.1:9890`); Hutch then calls CAO's constrained API.

## Quick reference

```sh
./bin/hutch project open /path/to/application --name Example --id example
./bin/hutch project list
./bin/hutch flow catalog
./bin/hutch flow start FLOW_NAME
./bin/hutch flow list --project example
./bin/hutch flow info RUN_ID
./bin/hutch flow stop RUN_ID
./bin/hutch flow enable FLOW_NAME
./bin/hutch flow disable FLOW_NAME
./bin/hutch agent list
./bin/hutch agent construct /path/to/external-config --include-skills --dry-run
```

Use global `--json` for stable machine-readable output and `--url` to select another Hutch instance.

## Quick deployment

`bin/hutch-deploy` initializes the local runtime directory, starts CAO and the
Hutch Dashboard, and can optionally register a project or install a template
Flow. It does not patch CAO and does not force Codex or OpenCode globally; the
deploy check only requires at least one of `codex` or `opencode` to exist.

```sh
./bin/hutch-deploy check
./bin/hutch-deploy init
./bin/hutch-deploy start
./bin/hutch-deploy status
./bin/hutch-deploy stop
```

One-shot local startup:

```sh
./bin/hutch-deploy all \
  --cao-repo /path/to/cli-agent-orchestrator \
  --hutch-home ~/.hutch
```

Install a generated template Flow during deployment:

```sh
./bin/hutch-deploy all \
  --cao-repo /path/to/cli-agent-orchestrator \
  --target-repo /path/to/service-repo \
  --template one-run \
  --flow-name service-one-run \
  --install-flow
```

## Customization

```sh
./bin/hutch agent construct /path/to/external-config --output cao-profiles --include-skills
./bin/hutch agent import-opencode /path/to/opencode-config --output cao-profiles
./bin/hutch flow compile workflows/example.yaml
./bin/hutch flow compile workflows/example.yaml --install --replace
./bin/hutch flow from-template /path/to/repo --template one-run \
  --name service-one-run --provider codex --compile --install --replace
```

Compilation does not install unless `--install` is present. Installed schedules remain disabled unless `--enable` is also present.
CAO-native template flows support `codex` and `opencode_cli`; choose one with
`flow from-template --provider`.

## Runtime data layout

Mutable Hutch data is stored below `~/.hutch` by default:

- `~/.hutch/runs/` — Run instances, durable state, events, task inboxes, result outboxes, artifacts, snapshots, and Agent Cell workspaces.
- `~/.hutch/runs/.trash/` — deleted Run records.
- `~/.hutch/workflows/` — generated target-specific workflow files.
- `~/.hutch/generated/` — compiled CAO bundles and generated Agent profiles.
- `~/.hutch/projects/projects.json` — Dashboard project registry.

Set `HUTCH_HOME=/path/to/runtime-root` to relocate these directories. Source workflows, templates, docs, and code remain in the Git checkout.

## Client configurations

- Codex: `.codex/config.toml`, root `AGENTS.md`, `qu-orchestrator`, and `qu-construct-agent` skills.
- OpenCode: `.opencode/opencode.json`, `.opencode/agents/qu.md`, and the `qu-construct-agent` skill.

Both configurations enforce the same boundary: no direct tmux control, no CAO source patches, and no manual Run-state edits.
