# Supported formats and mapping

## Source discovery

| Format | Agent files | Skill files |
|---|---|---|
| OpenCode | `.opencode/agents/*.md` | `.opencode/skills/**/SKILL.md` |
| Codex | `.codex/agents/*.toml` | `.agents/skills/**/SKILL.md`, `.codex/skills/**/SKILL.md` |
| Claude | `.claude/agents/*.md` | `.claude/skills/**/SKILL.md` |
| Generic | `agents/*.md` | `skills/**/SKILL.md` |

Markdown metadata is read from YAML frontmatter. Codex Agent TOML requires `name`, `description`, and `developer_instructions`.

## Authority mapping

The importer intentionally ignores external permission grants by default.

| CAO capability | Default | Explicit option |
|---|---:|---|
| `fs_read`, `fs_list` | enabled | — |
| `fs_write` | disabled | `--allow-write` |
| `execute_bash` | disabled | `--allow-shell` |
| supervisor role and CAO MCP | disabled | `--allow-supervisor` |
| external MCP, hooks, credentials | never imported | manual design review only |

External primary/supervisor Agents are demoted and recorded as warnings unless supervisor authority is explicitly enabled.

## Outputs

- `<profile>.md`: CAO-compatible profile with QU adapter contract and original instructions.
- `_skills/<profile>/`: preserved Skill resources when importing skills.
- `import-manifest.json`: source hashes, licenses, generated roles, tools, resources, and warnings.

The manifest is the import fact source. Generated profile prose is an execution artifact.
