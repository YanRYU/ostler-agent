---
name: codex-skill-template
version: "1.0.0"
domain: template
depends-on: []
payload:
  - { src: "SKILL.md", dest: ".agents/ostler/templates/EXAMPLE-SKILL/SKILL.md", mode: overwrite, mechanism: INSTRUCT }
---

# Codex skill template

An example only. Rename the directory, frontmatter name, and description before using it as a real skill.

## Prevents
Accidental activation of an uncustomized example skill.

## Procedure
Copy the template into `.agents/skills/`, then rename and customize it.

## State
The template remains outside Codex skill discovery until copied.
