---
name: hooks-core
version: "1.0.0"
layer: DETECT
depends-on: [rules-core]
payload:
  - { src: "hooks/stash-leak-check.sh",          dest: ".claude/hooks/stash-leak-check.sh",          mode: overwrite }
  - { src: "hooks/branch-similarity-check.sh",   dest: ".claude/hooks/branch-similarity-check.sh",   mode: overwrite }
  - { src: "hooks/personal-path-guard.py",       dest: ".claude/hooks/personal-path-guard.py",       mode: overwrite }
  - { src: "hooks/plan-exit-print-location.sh",  dest: ".claude/hooks/plan-exit-print-location.sh",  mode: overwrite }
---
# hooks-core

The DETECT layer: non-blocking hooks that surface invisible repo state at the moment
it matters. Each hook's header names the rule it enforces (audit cross-checks). These
ship the *hook scripts only* — wire them into the target's `settings.json` hooks block
manually (until you do, nothing is enforced or surfaced). Hooks that read the tool-call JSON
need `jq` on your PATH (`stash-leak-check.sh` and `branch-similarity-check.sh` do nothing
without it; `plan-exit-print-location.sh` prints `Plan file: (unable to determine — no
transcript)`). `plan-exit-print-location.sh` is a convenience, not a guard: after you exit
Claude Code plan mode it prints the path of the plan file (plans live in a
`plans` folder inside `.claude`) so you can open it; skip wiring it if you do not want that.
`depends-on: rules-core` because
the hook headers point at the git-hygiene and privacy rules from rules-core.
