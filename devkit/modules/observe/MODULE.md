---
name: observe
version: "2.0.1"
domain: observe
depends-on: []
payload:
  - { src: "hooks/plan-exit-print-location.sh", dest: ".claude/hooks/plan-exit-print-location.sh", mode: overwrite, mechanism: DETECT }
---
# observe

## Prevents

Nothing. This is the telemetry band: it enforces no rule and only makes work easier to see.

One convenience hook: after you exit Claude Code plan mode it prints the path of the plan file (plans live
in a `plans` folder inside `.claude`) so you can open it.

## Procedure

The hook is copied, not wired: add it to the hooks block of your `settings.json` yourself, or skip it if
you do not want it:

```json
{"hooks": {"PostToolUse": [{"matcher": "ExitPlanMode",
  "hooks": [{"type": "command", "command": "bash .claude/hooks/plan-exit-print-location.sh"}]}]}}
```

It reads the tool-call JSON and needs `jq` on your PATH; without `jq` it prints
`Plan file: (unable to determine — no transcript)`.

## State

One hook script in the target's `.claude` folder. Stateless.
