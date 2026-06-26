---
name: security-recon
description: Five-layer attack surface reconnaissance for source security audits.
license: MIT
compatibility: opencode
metadata:
  role: security-intel-collector
  collection: security-intel-subagent
---

# Security Reconnaissance

Use this skill for Phase 1 reconnaissance. Build the attack surface map and endpoint-permission matrix.

## Five-Layer Attack Surface Derivation

### T1: Architecture Pattern
```
Grep for: microservice/, serverless/, Dockerfile, docker-compose, k8s, terraform
Question: Where are trust boundaries? Inter-service auth? Gateway/API patterns?
```

### T2: Business Domain
```
Grep for: payment, order, user, admin, transaction, balance, transfer
Question: Financial? Healthcare? SaaS multi-tenant? Domain-specific logic flaws?
```

### T3: Framework & Language
```
Detect languages: rg '\.java$|\.py$|\.go$|\.php$|\.js$|\.ts$|\.rb$|\.cs$|\.rs$'
Detect frameworks: rg -l 'SpringBoot|Django|Flask|FastAPI|Express|Gin|Rails|ASP\.NET'
```

### T4: Deployment Environment
```
Grep for: Dockerfile, docker-compose.yml, k8s/, helm/, terraform/, .github/workflows/
Check for: EXPOSE ports, ENV secrets, mounted volumes, network_mode: host
```

### T5: Function Discovery
```
Grep entry points:
  rg '@(GetMapping|PostMapping|RequestMapping)'     # Spring
  rg 'def \w+\(request'                              # Django/Flask
  rg 'app\.(get|post|put|delete|patch)\('            # Flask/FastAPI
  rg 'router\.(get|post)\('                          # Express
  rg '@app\.route\('                                 # Flask
  rg '@Scheduled|@EventListener|@KafkaListener'      # Background jobs
  rg 'def main|if __name__'                          # CLI entry
```

## Endpoint-Permission Matrix Construction

Build this table for D3/D9 control-driven audit:

```markdown
| Endpoint | Method | Auth? | Permission Check | Resource Ownership | Notes |
|----------|--------|-------|-------------------|--------------------|-------|
```

Key checks per endpoint:
1. Is authentication required? (annotation/filter/middleware)
2. Is there a permission/role check? (`@PreAuthorize`, `@RequiresPermissions`, `@login_required`)
3. Is resource ownership verified? (`findById(id, userId)` vs `findById(id)`)
4. Are all CRUD operations on the same resource uniformly protected?

## Dependency Risk Scan
```
Check manifests: pom.xml, build.gradle, requirements.txt, Pipfile, pyproject.toml, CMakeLists.txt, go.mod, Cargo.toml
Known dangerous dependencies:
  - Java: fastjson, log4j-core, shiro-core, snakeyaml, commons-collections, jackson-databind, h2
  - Python: PyYAML<6.0, Jinja2<2.11.3, Django<4.2, paramiko<2.10.1, celery (pickle), numpy<1.22
  - C/C++: OpenSSL<1.1.1, zlib versions, libcurl versions
```

## Configuration Scan
```
rg 'DEBUG\s*=\s*True'                              # Django debug
rg 'app\.debug\s*=\s*True|FLASK_DEBUG'             # Flask debug
rg 'ALLOWED_HOSTS\s*=\s*\[.*\*'                    # Wildcard hosts
rg 'CORS_ORIGIN_ALLOW_ALL'                          # Open CORS
rg 'spring\.security|actuator'                      # Spring security config
rg 'management\.endpoints\.web\.exposure'           # Actuator exposure
rg 'SECRET_KEY\s*=|secretKey\s*=|apiKey\s*='       # Secrets in code/config
rg 'password\s*=|passwd\s*='                       # Plain passwords
```

## Output Checklist

Deliver all of:
- [ ] Five-layer attack surface map (T1-T5)
- [ ] Language audit routing table with file counts and frameworks
- [ ] Endpoint-permission matrix (at least routes with auth annotations sampled)
- [ ] Dependency risk summary (known dangerous versions)
- [ ] Configuration findings (secrets, debug modes, exposure)
- [ ] High-interest files list mapped to D1-D10 dimensions
- [ ] D1-D10 dimension activation matrix (which dims are relevant)
