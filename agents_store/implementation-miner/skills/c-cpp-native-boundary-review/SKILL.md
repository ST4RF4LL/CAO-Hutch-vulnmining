---
name: c-cpp-native-boundary-review
description: Review native C/C++ interfaces, IPC, syscalls, plugins, and FFI boundaries for unsafe trust and privilege transitions.
license: MIT
compatibility: opencode
metadata:
  role: c-cpp-source-auditor
  collection: c-cpp-subagent
---

# C/C++ Native Boundary Review

Use this skill for native code that crosses process, language, kernel, plugin, or privilege boundaries.

## Focus Areas

- FFI, JNI, Python extensions, plugin loaders, and callbacks that trust foreign inputs or object lifetimes.
- ioctl, syscall, IPC, shared memory, socket, and message parser interfaces.
- Privileged helpers, sandbox boundaries, setuid/setcap behavior, and capability checks.
- Race-prone boundary logic such as TOCTOU between validation and use.
- Unsafe assumptions about caller identity, buffer ownership, struct layout, or ABI compatibility.

## Evidence Requirements

Show which boundary is crossed, what the caller controls, which checks exist, and why the sink is security-sensitive.
