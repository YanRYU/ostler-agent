---
name: codex-command-rules
version: "1.0.0"
layer: BLOCKS
depends-on: []
payload:
  - { src: "ostler.rules", dest: ".codex/rules/ostler.rules", mode: overwrite }
---

# Codex command rules

Codex `prefix_rule` policy for commands it considers outside the sandbox. This does not reproduce Claude's Read/Write deny list or block operations inside the sandbox. Project-local rules require the Codex project config layer to be trusted.
