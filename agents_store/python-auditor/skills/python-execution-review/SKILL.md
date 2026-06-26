---
name: python-execution-review
description: Review Python code for command execution, dynamic code execution, plugin loading, import abuse, and template expression risks.
license: MIT
compatibility: opencode
metadata:
  role: python-source-auditor
  collection: python-subagent
---

# Python Execution Review

Use this skill when user-controlled data can influence code execution, command execution, imports, or templates.

## Focus Areas

- `eval`, `exec`, `compile`, dynamic imports, plugin loaders, and expression/template evaluation.
- `subprocess`, `os.system`, `os.popen`, shell invocation, argument construction, environment control, and working directory control.
- Jinja2, Django templates, format strings, f-strings with untrusted expressions, and sandbox assumptions.
- File names, package names, module paths, and config values used to load code.

## Evidence Requirements

Trace source to execution sink, distinguish shell injection from argument injection, and document controls such as allowlists or fixed command templates.
