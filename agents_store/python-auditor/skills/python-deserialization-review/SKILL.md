---
name: python-deserialization-review
description: Review Python deserialization for pickle, YAML, jsonpickle, and other unsafe object reconstruction risks.
license: MIT
compatibility: opencode
metadata:
  role: python-source-auditor
  collection: python-subagent
---

# Python Deserialization Review (D4)

## Grep Patterns

### pickle — Critical
```
rg 'pickle\.load\(' --type py
rg 'pickle\.loads\(' --type py
rg '_pickle\.load' --type py
rg 'cPickle\.load' --type py
rg 'pickle\.Unpickler' --type py
```

### YAML
```
rg 'yaml\.load\(' --type py | rg -v 'SafeLoader|yaml\.CSafeLoader'
rg 'yaml\.full_load\(' --type py
rg 'Loader=yaml\.Loader' --type py
```

### Other Deserialization
```
rg 'jsonpickle\.decode\(' --type py
rg 'marshal\.loads\(' --type py
rg 'shelve\.open\(' --type py
rg 'torch\.load\(' --type py
rg 'joblib\.load\(' --type py
rg 'dill\.load' --type py
```

### Celery / Task Queue
```
rg 'CELERY_TASK_SERIALIZER' --type py
rg 'task_serializer.*pickle' --type py
rg 'accept_content.*pickle' --type py
```

## Judgment Rules

| Pattern | Severity | Condition |
|---------|----------|-----------|
| `pickle.loads()` + untrusted source | **Critical (RCE)** | Any external input |
| `yaml.load()` without `SafeLoader` | **Critical (RCE)** | PyYAML < 6.0, untrusted YAML |
| `jsonpickle.decode()` + user input | **Critical (RCE)** | Attacker-controlled JSON |
| `torch.load()` + user-uploaded model | **Critical (RCE)** | Uses pickle internally |
| `marshal.loads()` + untrusted data | **Critical (RCE)** | Arbitrary code objects |
| Celery `task_serializer = 'pickle'` | **Critical** | Queue exposed to attacker |
| `dill.load()` / `joblib.load()` | **High** | User-provided serialized data |

## Easy-to-Miss Scenarios
- **Cache stores**: Redis/Django cache using pickle serialization → attacker controls cache key → gets value deserialized
- **ML model files**: `.pkl`/`.pt` files from user upload → `torch.load()` / `pickle.load()`
- **Message queues**: RabbitMQ/Kafka consumers deserializing pickle payloads
- **Cookie-based sessions**: Django signed cookie session (safe) vs pickle-based session backend (dangerous)
- **Default constructors**: `yaml.load(data)` without Loader parameter (Python 3.9+ FullLoader default — still dangerous)
- **Training pipelines**: User-submitted training data → model checkpoint → pickle serialization

## False-Positive Notes
- `yaml.load(data, Loader=yaml.SafeLoader)` or `yaml.safe_load(data)` = safe
- `pickle.load()` from trusted, internally-generated cache file with fixed path = lower risk (still document)
- `json.loads()` = safe (JSON only, no object reconstruction)
- `yaml.load()` in config files with `!!python/object` blocked = check if custom SafeLoader is used
- Django default session backend = signed cookie (safe, not pickle-based)

## Data Source Classification

| Source | Risk | Examples |
|--------|------|----------|
| HTTP body | **Critical** | API endpoints accepting serialized data |
| File upload | **Critical** | `.pkl`, `.pt`, `.joblib` file uploads |
| Message queue | **Critical** | Celery/RQ/Kafka consumers |
| Cache (Redis) | **High** | Cache key controllable by attacker |
| Database column | **High** | PickleType in SQLAlchemy |
| Internal file | **Medium** | Loading from fixed internal path |
| CLI argument | **Medium** | Admin-only CLI tools |

## Required Checks
1. For every `pickle.load*()` call: identify the data source and assess trust
2. For every `yaml.load()` call: verify `SafeLoader` is used
3. Check Celery config: `task_serializer` must NOT be `pickle`
4. Check ML pipeline: user-uploaded model files should be loaded in sandbox
5. Check dependency versions: PyYAML < 6.0, dill, joblib versions
