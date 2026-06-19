# Joern Rules

Store Joern and CPG-based static-analysis rules here.

Recommended layout:

- `index.json`: inventory of rules.
- `<language>/<rule-id>.sc`: Joern query or script.
- `<language>/<rule-id>.md`: rule intent, source/sink model, expected hits, known false positives, and test notes.

Rules should support defensive static analysis only.
