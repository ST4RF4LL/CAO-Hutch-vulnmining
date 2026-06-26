---
name: c-cpp-memory-safety-review
description: Review C/C++ code for memory safety including buffer overflow, use-after-free, double free, integer overflow, format string, and stack safety.
license: MIT
compatibility: opencode
metadata:
  role: c-cpp-source-auditor
  collection: c-cpp-subagent
---

# C/C++ Memory Safety Review (D4)

## Grep Patterns

### Buffer Overflow — Unsafe Functions
```
rg 'strcpy\(' --type c --type cpp
rg 'strcat\(' --type c --type cpp
rg 'sprintf\(' --type c --type cpp
rg 'gets\(' --type c --type cpp
rg 'scanf\("%s' --type c --type cpp
```

### Buffer Overflow — Safer but Error-Prone
```
rg 'strncpy\(' --type c --type cpp       # Must manually NUL-terminate
rg 'snprintf\(' --type c --type cpp      # Check return > buffer size
```

### Use-After-Free
```
rg 'free\(' --type c --type cpp -A5 | rg -v '= NULL|=\s*nullptr'  # free() without NULL assignment
rg 'delete\s+\w+' --type cpp -A3 | rg -v '=\s*nullptr'            # delete without nullptr
```

### Double-Free
```
rg 'free\(' --type c --type cpp           # Find all free sites; check for multiple paths to same pointer
```

### Integer Overflow
```
rg 'malloc\(.*\*' --type c --type cpp     # malloc(n * sizeof(T)) — check n for overflow
rg 'calloc\(' --type c --type cpp         # calloc multiplies internally but still check args
rg 'alloca\(' --type c --type cpp         # User-controlled size → stack overflow
```

### Format String
```
rg 'printf\(' --type c --type cpp | rg -v 'printf\("%s'             # Direct user string as format
rg 'fprintf\(.*,.*[^"]\)' --type c --type cpp                       # fprintf with variable format
```

### Stack Safety
```
rg 'alloca\(' --type c --type cpp          # User-controlled size
rg 'char\s+\w+\[.*\+\s*\w+\]' --type cpp  # VLA with variable size
```

## Judgment Rules

| Pattern | Severity | Condition |
|---------|----------|-----------|
| `strcpy`/`gets`/`sprintf` | **High** | Any use in production code |
| `strncpy` without manual NUL | **Medium** | Subsequent `strlen`/`strcpy` on result |
| `free(ptr)` no `ptr=NULL` | **Critical** | Reachable code path after free uses ptr |
| `free(ptr)` in two code paths | **Critical** | No interlock preventing double call |
| `malloc(n * m)` no overflow check | **High** | n or m from user input |
| `alloca(user_size)` | **High** | Stack overflow → code execution possible |
| `printf(user_input)` no format arg | **Critical** | `%n` enables arbitrary memory write |
| Recursion no depth limit | **Medium** | Input controls recursion depth |
| `realloc` return not checked | **High** | NULL return → original ptr leaked; use-after-free of original |

## Easy-to-Miss Scenarios
- `snprintf` return > buffer size → truncation → logic errors downstream
- `strncpy(dst, src, sizeof(dst))` doesn't NUL-terminate if src ≥ dst size
- `realloc` returns NULL → original block NOT freed → must save old pointer
- C++ `std::vector::operator[]` no bounds check (use `.at()`)
- Exception safety: `new` in constructor throws → partial object leak
- `int len = strlen(input); char buf[len];` — `len` is `size_t`, truncation to `int`

## False-Positive Notes
- `strncpy` with explicit `dst[sizeof(dst)-1] = '\0'` immediately after = safe
- `free(ptr); ptr = container->next;` — re-assignment from struct = safe
- `malloc(n * sizeof(int))` where n validated with `if (n > MAX) return;` = safe
- `printf("%s", user_input)` = safe; only `printf(user_input)` is dangerous
