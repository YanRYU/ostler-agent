---
name: codex-audit
version: "1.0.0"
domain: all
depends-on: []
payload:
  - { src: "devkit-audit.py", dest: ".codex/devkit-audit.py", mode: overwrite, mechanism: VERIFY }
---

# Codex audit

Installs the same standard-library-only drift checker as the Claude module. The target can audit its installed files after the factory checkout is removed.

## Prevents
Undetected drift in files managed by the shared manifest.

## Procedure
Run the installed audit script or the installer `check` command after changes.

## State
The audit script is copied into the target and checks the shared manifest without factory dependencies.
