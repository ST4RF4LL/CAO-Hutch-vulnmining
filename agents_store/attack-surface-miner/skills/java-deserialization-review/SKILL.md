---
name: java-deserialization-review
description: Review Java/JVM deserialization and object-mapping code for gadget chains, polymorphic typing, and unsafe type resolution.
license: MIT
compatibility: opencode
metadata:
  role: java-source-auditor
  collection: java-subagent
---

# Java Deserialization Review (D4)

## Grep Patterns

### Java Native Serialization
```
rg 'ObjectInputStream' --type java
rg 'readObject\(' --type java
rg 'XMLDecoder' --type java
rg 'readObject|readUnshared' --type java
```

### JSON Deserialization with Type Inference
```
rg 'JSON\.parse\(' --type java              # Fastjson
rg 'JSON\.parseObject\(' --type java        # Fastjson
rg '@type' --type java                       # Fastjson type specifier
rg 'enableDefaultTyping' --type java        # Jackson polymorphic
rg 'ObjectMapper.*enableDefaultTyping' --type java
rg 'activateDefaultTyping' --type java
```

### YAML Deserialization
```
rg 'new Yaml\(' --type java                 # Must be SafeConstructor
rg 'Yaml\(\)' --type java
rg 'SafeConstructor' --type java
rg 'org\.yaml\.snakeyaml' --type java
```

### Other Deserialization Vectors
```
rg 'XStream' --type java
rg 'Kryo' --type java
rg 'HessianInput' --type java
rg 'HessianOutput' --type java
rg 'C3P0' --type java
```

### Gadget Chain Dependencies
```
rg 'commons-collections' pom.xml build.gradle
rg 'commons-beanutils' pom.xml build.gradle
rg 'c3p0' pom.xml build.gradle
rg 'snakeyaml' pom.xml build.gradle
rg 'fastjson' pom.xml build.gradle
rg 'jackson-databind' pom.xml build.gradle
```

## Judgment Rules

| Pattern | Severity | Condition |
|---------|----------|-----------|
| `ObjectInputStream.readObject()` + untrusted source | **Critical (RCE)** | Classpath has gadget library |
| Fastjson < 1.2.83 + `JSON.parse()` | **Critical** | autoType bypasses known |
| Jackson `enableDefaultTyping()` | **Critical** | Polymorphic deserialization + gadget |
| SnakeYAML `new Yaml()` default constructor | **Critical (CVE-2022-1471)** | Untrusted YAML input |
| XStream `fromXML()` without security setup | **Critical** | No type allowlist configured |
| Hessian deserialization from external source | **High** | No type filtering |
| Fastjson `@type` in user-controlled JSON | **High** | Version-dependent bypass |

## Gadget Chain Libraries (Known Dangerous)
- `commons-collections` < 3.2.2 (CC1-CC8 chains: InvokerTransformer, ChainedTransformer, LazyMap)
- `commons-beanutils` (BeanComparator chain)
- `c3p0` (JNDI injection via C3P0Impl/ReferenceableUtils)
- `Spring` (TemplatesImpl + ServiceLoader)
- `ROME` (ToStringBean chain)
- `Fastjson` (JNDI/JDBC/Classloader injection via @type)
- `Jackson` (polymorphic type via @class/@c/@type)

## Easy-to-Miss Scenarios
- Redis/MQ messages: serialized objects stored in queue/cache get deserialized by consumer
- `Cookie` → Base64 decode → `ObjectInputStream.readObject()`
- Fastjson autoType: `JSON.parse(text, Feature.SupportAutoType)` bypasses; version < 1.2.83 has known gadget chains
- SnakeYAML: `new Yaml()` constructor (no SafeConstructor) in configuration loading
- Hessian: RPC framework using Hessian serialization → attacker can send crafted Hessian payload
- JDBC connection string injection → `jdbc:h2:mem:;INIT=RUNSCRIPT` → RCE

## False-Positive Notes
- `ObjectInputStream` used with `ValidatingObjectInputStream` (Apache Commons IO) = safe (allowlist)
- `ObjectInputStream` reading from controlled internal storage (not user-facing) = lower risk (still flag)
- SnakeYAML `new Yaml(new SafeConstructor())` = safe
- Jackson without `enableDefaultTyping` or `@JsonTypeInfo` on user-controllable classes = safe
- `ObjectMapper` default config (no default typing) for DTO classes without polymorphic fields = safe

## Severity Factors
- **+Critical**: Gadget library on classpath + input from HTTP/RPC/JMS + no type filtering
- **+High**: Input from authenticated user + gadget library present + limited type filter bypassable
- **+Medium**: Input from admin-only endpoint + gadget library present
- **-Low**: No gadget libraries detected on classpath (verify thoroughly)
