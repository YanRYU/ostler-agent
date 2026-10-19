---
name: codex-instructions
version: "1.0.0"
domain: all
depends-on: []
payload:
  - { src: "AGENTS.md", dest: "AGENTS.md", mode: overwrite, mechanism: ROUTE }
  - { src: "rules/git-hygiene.md", dest: ".agents/ostler/rules/git-hygiene.md", mode: overwrite, mechanism: INSTRUCT }
  - { src: "rules/plan-writing.md", dest: ".agents/ostler/rules/plan-writing.md", mode: overwrite, mechanism: INSTRUCT }
  - { src: "rules/privacy.md", dest: ".agents/ostler/rules/privacy.md", mode: overwrite, mechanism: INSTRUCT }
  - { src: "rules/self-contained-agents.md", dest: ".agents/ostler/rules/self-contained-agents.md", mode: overwrite, mechanism: INSTRUCT }
---

# Codex instructions

Repository guidance for Codex. `AGENTS.md` is the entry file Codex reads; the other files provide detail when relevant. Existing unmanaged `AGENTS.md` files are refused, not merged or overwritten.

## Prevents
Repository work that loses Git hygiene, planning, privacy, or portable agent practices.

## Procedure
Codex reads `AGENTS.md` and follows the linked detailed rules when relevant.

## State
The guidance is copied into the target; existing unmanaged `AGENTS.md` is preserved.
