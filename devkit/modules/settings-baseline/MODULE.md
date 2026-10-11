---
name: settings-baseline
version: "1.0.0"
layer: BLOCKS
depends-on: []
payload:
  - { src: "settings-baseline.partial.json", dest: ".claude/settings.json", mode: merge }
---
# settings-baseline

The BLOCKS layer. A permission template for `settings.json`:
deny lists for destructive shell/git/filesystem commands and secret/env paths, plus
ask gates for privilege-escalating or outbound commands.

`mode: merge` — the installer merges this `permissions` block into the target's existing
`settings.json` rather than overwriting (so repo-specific `allow[]` and hooks survive).
`allow[]` and `additionalDirectories` are intentionally empty — each consumer fills them
per repo. Put safety limits in settings.json, not in individual scripts that could skip them.

**The `Bash(git push:*)` ask gate is deliberate.** It is the only push-side trunk
protection this kit provides. Relax it to `allow` only if you add your own hook that
blocks pushes to your trunk branch.
