---
name: java-injection-review
description: Review Java/JVM code for SQL, JPQL, LDAP, XPath, template, expression, command, and class-loading injection issues.
license: MIT
compatibility: opencode
metadata:
  role: java-source-auditor
  collection: java-subagent
---

# Java Injection Review

Use this skill when user-controlled data reaches query builders, expression engines, templates, command execution, or dynamic loading.

## Focus Areas

- SQL, JPQL, NoSQL, LDAP, XPath, and search query construction.
- SpEL, OGNL, template engines, scripting engines, reflection, and dynamic class loading.
- Process execution with string commands, shell invocation, environment control, or unsafe argument construction.
- SSRF-related URL construction when injection affects network destinations or headers.
- Sanitizer misuse, partial escaping, and framework APIs that look safe but concatenate raw fragments.

## Evidence Requirements

Trace source to sink, identify whether parameterization or allowlisting applies to the exact fragment, and state reachable impact.
