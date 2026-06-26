---
name: python-web-framework-review
description: Review Django, Flask, FastAPI, and Starlette applications for auth, CSRF/CORS, SSRF, file handling, and framework misconfiguration.
license: MIT
compatibility: opencode
metadata:
  role: python-source-auditor
  collection: python-subagent
---

# Python Web Framework Review

Use this skill for Python web applications, APIs, admin panels, and background tasks reachable from web requests.

## Focus Areas

- Django, Flask, FastAPI, Starlette route registration, middleware order, decorators, dependencies, and auth checks.
- CSRF, CORS, session/cookie flags, debug mode, exception disclosure, admin exposure, and tenant isolation.
- SSRF through `requests`, `urllib`, async clients, redirects, proxy behavior, DNS assumptions, and metadata endpoints.
- Upload/download paths, archive extraction, path traversal, static file exposure, and temporary files.
- Template rendering, autoescaping assumptions, and user-controlled template selection.

## Evidence Requirements

Tie each issue to a route, task, or handler and verify that the relevant framework guard applies to that exact path.
