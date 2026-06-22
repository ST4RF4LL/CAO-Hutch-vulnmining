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

## Customization

```sh
./bin/hutch agent construct /path/to/external-config --output cao-profiles --include-skills
./bin/hutch agent import-opencode /path/to/opencode-config --output cao-profiles
./bin/hutch flow compile workflows/example.yaml
./bin/hutch flow compile workflows/example.yaml --install --replace
```

Compilation does not install unless `--install` is present. Installed schedules remain disabled unless `--enable` is also present.

## Client configurations

- Codex: `.codex/config.toml`, root `AGENTS.md`, `qu-orchestrator`, and `qu-construct-agent` skills.
- OpenCode: `.opencode/opencode.json`, `.opencode/agents/qu.md`, and the `qu-construct-agent` skill.

Both configurations enforce the same boundary: no direct tmux control, no CAO source patches, and no manual Run-state edits.
