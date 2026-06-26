---
name: java-web-security-review
description: Review Java web applications for authentication, authorization, Spring Security, SSRF, file handling, and framework configuration issues.
license: MIT
compatibility: opencode
metadata:
  role: java-source-auditor
  collection: java-subagent
---

# Java Web Security Review

Use this skill for Java/JVM HTTP services, API servers, admin panels, and framework configuration.

## Focus Areas

- Spring Security filters, interceptors, method security, route matchers, and annotation coverage.
- Object-level authorization, tenant isolation, admin-only operations, and insecure direct object access.
- SSRF, redirects, CORS/CSRF, session flags, debug endpoints, actuator exposure, and error handling.
- Uploads, downloads, archive extraction, path traversal, and temporary file handling.
- Maven/Gradle dependencies and production reachability of test or debug utilities.

## Evidence Requirements

Tie each issue to an entrypoint and exact framework configuration path. Verify that filters and annotations apply to the route under review.
