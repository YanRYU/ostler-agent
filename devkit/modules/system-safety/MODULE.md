---
name: system-safety
version: "2.0.1"
domain: system-safety
depends-on: []
payload:
  - { src: "settings-system-safety.partial.json", dest: ".claude/settings.json", mode: merge, mechanism: BLOCK }
---
# system-safety

## Prevents

Irreversible damage to the machine or the repository from one careless shell command or file write.

A permission fragment: deny patterns for destructive shell, git and filesystem commands and for writes
to system paths, plus ask gates for privilege-escalating or outbound commands.

## Procedure

`mode: merge`: the installer merges this `permissions` block into the target's existing `settings.json`
rather than overwriting it, so repo-specific `allow` entries and hooks survive. `allow` and
`additionalDirectories` start empty; fill them per repo. Put safety limits in settings, not in scripts
that could skip them.

**The `Bash(git push:*)` ask gate is deliberate.** It is the only push-side trunk protection this kit
provides. Relax it to `allow` only if you add your own hook that blocks pushes to your trunk branch.

## State

Edits one file, the `settings.json` in the target's `.claude` folder, and records in the install manifest
which patterns it contributed, so `remove-module` takes out exactly those and never your own `allow`
entries or a pattern that was already in the file before the install. On a target migrated from 1.x, a pattern that was already in the file
before ownership tracking cannot be told from your own, so it is kept (named in a `kept` line) and never removed.
