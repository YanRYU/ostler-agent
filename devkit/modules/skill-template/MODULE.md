---
name: skill-template
version: "1.0.0"
layer: TEMPLATE
depends-on: []
payload:
  - { src: "SKILL.md", dest: ".claude/skills/EXAMPLE-SKILL/SKILL.md", mode: overwrite }
---
# skill-template

The SKILL.md frontmatter contract + a one-screen body skeleton. Convention used by this template: keep a skill
to method and knowledge, and put execution in scripts or tools it calls (adapt if your repo differs). Fewer `allowed-tools` =
more focused skill. Copy the payload to a new skill dir and rename.
