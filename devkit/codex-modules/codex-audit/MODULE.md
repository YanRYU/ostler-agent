---
name: codex-audit
version: "1.0.0"
layer: VERIFIES
depends-on: []
payload:
  - { src: "devkit-audit.py", dest: ".codex/devkit-audit.py", mode: overwrite }
---

# Codex audit

Installs the same standard-library-only drift checker as the Claude module. The target can audit its installed files after the factory checkout is removed.
