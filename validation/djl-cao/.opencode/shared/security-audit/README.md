# Shared Security Audit Assets

This directory is readable by all security audit subagents.

Only `security-skill-optimizer` should normally modify these assets during the validation feedback loop.

## Directories

- `joern-rules/`: Joern rules, rule metadata, and rule inventory.
- `vulnerability-cases/`: confirmed or likely reusable vulnerability cases.
- `false-positive-cases/`: rejected findings and false-positive patterns.
- `rule-results/`: non-source static scan summaries used to improve rules and skills.

## Feedback Flow

1. Source auditors produce candidate findings.
2. `vulnerability-validator` classifies them as `confirmed`, `likely`, `needs-info`, or `false-positive`.
3. `security-audit-orchestrator` sends useful learning signals to `security-skill-optimizer`.
4. `security-skill-optimizer` updates skills, Joern rules, vulnerability cases, and false-positive cases.
