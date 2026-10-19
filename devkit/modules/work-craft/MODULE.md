---
name: work-craft
version: "2.1.6"
domain: work-craft
depends-on: []
payload:
  - { src: "rules/plan-writing.md",           dest: ".claude/rules/plan-writing.md",           mode: overwrite, mechanism: INSTRUCT }
  - { src: "rules/self-contained-agents.md",  dest: ".claude/rules/self-contained-agents.md",  mode: overwrite, mechanism: INSTRUCT }
  - { src: "rules/output-quality-standards.md", dest: ".claude/rules/output-quality-standards.md", mode: overwrite, mechanism: INSTRUCT }
  - { src: "rules/communication-style.md",       dest: ".claude/rules/communication-style.md",       mode: overwrite, mechanism: INSTRUCT }
---
# work-craft

## Prevents

Low-quality work and process shortcuts: plans nobody can follow, agents that only work on one machine, work declared done without evidence, and reports that bury the answer.

Always-on behavioral rules, one file per concern: plan writing, self-contained agents, twelve output quality
standards (evidence before "done", hands-off docs, self-test), and an answer-first communication style. The rules are written for any repo, with no
organization-specific content; edit them to match how your team works.

## Procedure

Install it, read each rule once, and edit the ones that do not fit your repo (a local edit shows as
`modified-downstream` until you record it with `patch` or take ownership with the fork marker). No hook
enforces these rules; the audit module lists rules that nothing enforces.
`communication-style.md` points to `output-quality-standards.md` (standard 11 sets how subagent reports open), so install and keep both
together; they ship in this one module.

## State

Four rule files in the target's `.claude` rules folder. No state outside them.
