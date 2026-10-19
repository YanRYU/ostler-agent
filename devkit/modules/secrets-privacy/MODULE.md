---
name: secrets-privacy
version: "2.0.2"
domain: secrets-privacy
depends-on: []
payload:
  - { src: "settings-secrets-privacy.partial.json", dest: ".claude/settings.json",                  mode: merge,     mechanism: BLOCK }
  - { src: "hooks/personal-path-guard.py",          dest: ".claude/hooks/personal-path-guard.py",   mode: overwrite, mechanism: DETECT }
  - { src: "rules/privacy.md",                      dest: ".claude/rules/privacy.md",               mode: overwrite, mechanism: INSTRUCT }
---
# secrets-privacy

## Prevents

Credential exposure and personal data leaking into files other people will read.

A permission fragment that denies reading or writing environment and secrets files, a hook that flags
personal absolute paths written into shared files, and the privacy rule both point at.

As shipped the hook is DETECT: it prints a warning and lets the write through. With
`PERSONAL_PATH_GUARD_MODE=block` the same hook is BLOCK: it exits 2 and the write is refused.

## Procedure

`mode: merge` for the fragment: the installer adds these `deny` patterns to the target's existing
`settings.json` and never touches your `allow` entries. The hook is copied, not wired: add it to the
hooks block of your `settings.json` yourself with this entry (it needs `python3`, not `jq`):

```json
{"hooks": {"PreToolUse": [{"matcher": "Write|Edit|NotebookEdit",
  "hooks": [{"type": "command", "command": "python3 .claude/hooks/personal-path-guard.py"}]}]}}
```

By default it only warns; set `PERSONAL_PATH_GUARD_MODE=block` in the environment the agent launches with
to make it stop the write. Its header names the privacy rule it enforces. To remove the module, delete the
hook entry you added, then run `remove-module`.

## State

Edits one file, the `settings.json` in the target's `.claude` folder, and records in the install manifest
which patterns it contributed, so `remove-module` takes out exactly those and never your own entries. On
a target migrated from 1.x, a pattern that was already in the file before ownership tracking cannot be
told from your own, so it is kept (named in a `kept` line) and never removed. Also one hook script and one rule file; the hook keeps no state.
