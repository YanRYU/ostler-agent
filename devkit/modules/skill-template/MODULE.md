---
name: skill-template
version: "1.0.2"
domain: template
depends-on: []
payload:
  - { src: "SKILL.md", dest: ".claude/skills/EXAMPLE-SKILL/SKILL.md", mode: overwrite, mechanism: INSTRUCT }
---
# skill-template

## Prevents

Skills written without a consistent shape, so they never trigger or do too much.

The SKILL.md frontmatter contract + a one-screen body skeleton. Convention used by this template: keep a skill
to method and knowledge, and put execution in scripts or tools it calls (adapt if your repo differs). Fewer `allowed-tools` =
more focused skill. 
## Procedure

Copy the payload to a new skill dir and rename it.

## State

One example skill file, in a folder named EXAMPLE-SKILL under the target's `.claude` skills folder. Delete it once you have your own skill.
