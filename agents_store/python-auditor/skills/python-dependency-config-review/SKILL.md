---
name: python-dependency-config-review
description: Review Python dependencies, configuration loading, secrets handling, logging, and deployment defaults for security weaknesses.
license: MIT
compatibility: opencode
metadata:
  role: python-source-auditor
  collection: python-subagent
---

# Python Dependency and Config Review

Use this skill for Python project metadata, runtime configuration, secrets, logging, and deployment defaults.

## Focus Areas

- `requirements*.txt`, `pyproject.toml`, `Pipfile.lock`, `poetry.lock`, vendored packages, and unsafe dependency families.
- Environment variable handling, default credentials, debug flags, insecure fallback config, and config precedence.
- Hardcoded secrets, private keys, API tokens, test credentials, and accidental logging of sensitive values.
- TLS verification, proxy configuration, allowed hosts, origins, and production safety flags.
- Packaging scripts, entrypoints, setup hooks, and dependency confusion risk.

## Evidence Requirements

Identify the config source, runtime path, default behavior, and how a deployment or attacker can reach the insecure state.
