# Hutch workflow templates

This directory stores reusable CAO-native Hutch workflow templates. A template
contains the generic Agent/Stage graph; the target Git checkout, CAO checkout,
skill roots, and generated workflow name are injected at render time.

Quick render:

```bash
./bin/hutch --json flow from-template /absolute/path/to/target-repo \
  --template one-run \
  --name target-one-run
```

The default rendered workflow path is `workflows/<name>.generated.json`, which is
ignored by Git. Pass `--compile` to immediately build the CAO bundle, and
`--install --replace` if the rendered flow should be installed into CAO.

Built-in templates:

- `information-collection`: repository architecture, module inventory, business
  flows, external interfaces, and security-relevant components.
- `threat-modeling`: information collection plus attack surface, trust
  boundaries, and threat intelligence.
- `vulnerability-mining`: a generic vulnerability-mining flow that performs
  local context reconstruction, mining, validation, and final reporting.
- `one-run`: first-contact baseline flow combining information collection,
  threat modeling, vulnerability mining, validation, and reporting in one CAO
  visible flow.
- `security-knowledge-one-run`: first-contact source audit using
  `secknowledge-skill` and `hack-skills` as bounded methodology sources.
- `security-knowledge-recon`, `security-knowledge-threat-model`, and
  `security-knowledge-vulnerability-mining`: the same security-knowledge
  methodology split across recon, planning, and mining-style flows.

Security-knowledge templates do not vendor the upstream repositories. Provide
legal local checkouts as skill roots:

```bash
export SECKNOWLEDGE_SKILL_ROOT=/path/to/secknowledge-skill
export HACK_SKILLS_ROOT=/path/to/hack-skills
./bin/hutch --json flow from-template /absolute/path/to/target-repo \
  --template security-knowledge-one-run \
  --name target-security-one-run \
  --strict-skills
```

`hack-skills` was inspected with an MIT license file. `secknowledge-skill` had
no LICENSE/NOTICE file in the inspected checkout, so Hutch records provenance
and requires the operator to supply a lawful local copy instead of silently
redistributing it.
