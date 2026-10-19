---
name: git-multi-session
version: "2.0.2"
domain: git-multi-session
depends-on: []
payload:
  - { src: "rules/git-hygiene.md",                dest: ".claude/rules/git-hygiene.md",                mode: overwrite, mechanism: INSTRUCT }
  - { src: "hooks/stash-leak-check.sh",            dest: ".claude/hooks/stash-leak-check.sh",            mode: overwrite, mechanism: DETECT }
  - { src: "hooks/branch-similarity-check.sh",     dest: ".claude/hooks/branch-similarity-check.sh",     mode: overwrite, mechanism: DETECT }
---
# git-multi-session

## Prevents

Collisions between parallel sessions working in one repository, and git state left behind that nobody noticed.

One always-on rule (git hygiene: branches, stashes, who owns what) and the two hooks that enforce its
checkable parts. Both hooks only surface information; neither stops the action.

## Procedure

Install it and read the rule once. The hooks are copied, not wired: add them to the hooks block of your
`settings.json` yourself (until you do, nothing is surfaced) with this entry:

```json
{"hooks": {
  "PreToolUse": [{"matcher": "Bash",
    "hooks": [{"type": "command", "command": "bash .claude/hooks/branch-similarity-check.sh"}]}],
  "PostToolUse": [{"matcher": "Bash",
    "hooks": [{"type": "command", "command": "bash .claude/hooks/stash-leak-check.sh"}]}]}}
```

Both read the tool-call JSON, so they need `jq` on your PATH and do nothing without it. Each hook's header
names the rule it enforces; the audit module cross-checks that pairing. To remove the module, delete the two
hook entries you added, then run `remove-module`.

## State

One rule file and two hook scripts in the target's `.claude` folder. The hooks are stateless: they read
the current git state and keep nothing.
