---
name: secure-code-review-common
description: Universal secure code review methodology covering dual-track audit model, coverage matrix, and evidence standards.
license: MIT
compatibility: opencode
metadata:
  role: shared
  collection: common-subagent
---

# Secure Code Review Common

Use this skill as the foundational methodology for all source security audits. Language-specific skills extend this base.

## Dual-Track Audit Model

Three execution strategies map to different vulnerability types:

| Track | Dimensions | Logic | Key Metric |
|-------|-----------|-------|------------|
| **Sink-driven** | D1, D4, D5, D6 | Grep dangerous functions → trace data flow → verify no sanitization | Sink fan-out rate ≥ 30% |
| **Control-driven** | D3, D9 | Enumerate operations → verify security controls exist → missing = vulnerability | Endpoint audit rate |
| **Config-driven** | D2, D7, D8, D10 | Search configurations → compare against security baselines | Config item coverage |

**Critical distinction**: Sink-driven finds "dangerous code that exists." Control-driven finds "security controls that are absent" — Grep cannot find code that doesn't exist.

## D1-D10 Coverage Matrix

Use this checklist to track completeness:

| D# | Dimension | Core Question | Audit Track |
|----|-----------|---------------|-------------|
| D1 | Injection | Can user input reach SQL/Cmd/LDAP/SSTI/SpEL execution sinks? | Sink-driven |
| D2 | Authentication | Token generation/verification complete? Secrets safe? Session fixation? | Config-driven |
| D3 | Authorization | Does each sensitive operation verify user/resource ownership? CRUD permission consistent? | Control-driven |
| D4 | Deserialization | Untrusted data deserialized? Gadget chains reachable? | Sink-driven |
| D5 | File Operations | Path traversal? Unrestricted upload? Zip Slip? Symlink attacks? | Sink-driven |
| D6 | SSRF | User-controlled URLs? Protocol restriction? Cloud metadata accessible? | Sink-driven |
| D7 | Cryptography | Hardcoded keys/IV? ECB mode? Weak KDF? Cert validation bypass? | Config-driven |
| D8 | Configuration | Debug endpoints exposed? CORS misconfig? Secrets in config files? | Config-driven |
| D9 | Business Logic | Race conditions? Mass Assignment? IDOR? State machine bypass? Payment tampering? | Control-driven |
| D10 | Supply Chain | Dependency CVEs? Vendored code risks? Version in security range? | Config-driven |

## Evidence Standards

Every finding must include:

1. **Affected code** — `file:line` with actual code snippet (not fabricated)
2. **Data flow** — source → transformation → sink (sink-driven) OR missing control description (control-driven)
3. **Severity rationale** — `Reachability × InputControl × ExploitComplexity × Impact`
4. **Concrete fix** — specific code change, not generic advice

## False-Positive Prevention Rules

- Verify file/location exists before reporting (use Read tool output)
- Confirm the project actually uses the language/framework detected
- Do not report missing crypto in projects without crypto functionality
- Do not report SSRF in projects without any HTTP client code
- User-controlled config ≠ vulnerability; user-controlled input reaching exec = vulnerability
- `#{}` in MyBatis = safe; `${}` = dangerous; `PreparedStatement` = safe; `Statement` + concat = dangerous

## Output Conventions

All findings must use this header:

```
=== HEADER START ===
COVERAGE: D1=✅(fan=N/M), D2=✅(N), ...
UNCHECKED: D#:[category]: brief description
STATS: tools=N/50 | files_read=N | grep_patterns=N | endpoints_audited=N/total
=== HEADER END ===
```
