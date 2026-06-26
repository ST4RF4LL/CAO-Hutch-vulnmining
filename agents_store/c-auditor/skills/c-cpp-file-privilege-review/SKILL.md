---
name: c-cpp-file-privilege-review
description: Review C/C++ filesystem, process execution, temporary file, symlink, and privilege-handling code for security issues.
license: MIT
compatibility: opencode
metadata:
  role: c-cpp-source-auditor
  collection: c-cpp-subagent
---

# C/C++ File and Privilege Review

Use this skill when reviewing filesystem operations, path construction, process spawning, permissions, or privileged native utilities.

## Focus Areas

- Path traversal, unsafe path joins, canonicalization gaps, and archive extraction issues.
- Symlink/hardlink races, unsafe temporary files, predictable names, and insecure permissions.
- Shell/process execution with attacker-controlled arguments, environment, current directory, or inherited descriptors.
- Privilege dropping, privilege restoration, uid/gid/capability handling, and chroot/sandbox assumptions.
- Format string bugs and unsafe logging when untrusted data reaches format parameters.

## Evidence Requirements

Trace the untrusted path, command, or privilege context into the sensitive operation and document missing checks.
