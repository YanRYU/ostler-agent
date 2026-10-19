---
name: codex-instructions
version: "1.0.0"
layer: INSTRUCT
depends-on: []
payload:
  - { src: "AGENTS.md", dest: "AGENTS.md", mode: overwrite }
  - { src: "rules/git-hygiene.md", dest: ".agents/ostler/rules/git-hygiene.md", mode: overwrite }
  - { src: "rules/plan-writing.md", dest: ".agents/ostler/rules/plan-writing.md", mode: overwrite }
  - { src: "rules/privacy.md", dest: ".agents/ostler/rules/privacy.md", mode: overwrite }
  - { src: "rules/self-contained-agents.md", dest: ".agents/ostler/rules/self-contained-agents.md", mode: overwrite }
---

# Codex instructions

Repository guidance for Codex. `AGENTS.md` is the entry file Codex reads; the other files provide detail when relevant. Existing unmanaged `AGENTS.md` files are refused, not merged or overwritten.
