---
name: rules-core
version: "1.0.0"
layer: INSTRUCT
depends-on: []
payload:
  - { src: "rules/git-hygiene.md",  dest: ".claude/rules/git-hygiene.md",  mode: overwrite }
  - { src: "rules/plan-writing.md", dest: ".claude/rules/plan-writing.md", mode: overwrite }
  - { src: "rules/self-contained-agents.md",    dest: ".claude/rules/self-contained-agents.md",    mode: overwrite }
  - { src: "rules/privacy.md",      dest: ".claude/rules/privacy.md",      mode: overwrite }
---
# rules-core

The INSTRUCT layer: always-on behavioral rules, one file = one concern. Generic
skeletons only — domain/org governance rules stay in the consuming repo. Each rule is
enforced (where possible) by a hooks-core hook; the audit module flags rules without
enforcement and hooks without rules.
