---
name: decision-gating-business
version: "1.0.17"
domain: decision-gating-business
depends-on: []
payload:
  - { src: "rules/biz-outcome-gate.md",       dest: ".claude/rules/biz-outcome-gate.md",       mode: overwrite, mechanism: INSTRUCT }
  - { src: "hooks/outcome-block-check.sh",     dest: ".claude/hooks/outcome-block-check.sh",     mode: overwrite, mechanism: DETECT }
---
# decision-gating-business

## Prevents

Building things nobody asked for: plans that reach you with no business outcome (the business KPI that moves, not an internal proxy; roughly how much; how and when you will check it; and the consumer who relies on it and the decision that changes).

A rule (the outcome gate: four questions, a short planning horizon (2 weeks by default, adjustable), a pressure-test that reads the decision through a few review lenses (four suggested, adapt them to your context)) and a hook that
checks a plan for its `## Outcome` block, a rollback and an end-to-end verification step when the plan is
presented. As shipped the hook is DETECT: it prints a reminder and lets the plan through. With
`OUTCOME_CHECK_MODE=block` the same hook is BLOCK: it exits 2, and only a line that is exactly
`## Outcome` counts (not `# Outcome`, not inside a code fence, not indented).

## Procedure

The hook is copied, not wired: add this entry to the `hooks` block of the settings file in your target's .claude folder
(the installer never edits your hooks):

```json
{"hooks": {"PreToolUse": [{"matcher": "ExitPlanMode",
  "hooks": [{"type": "command", "command": "bash .claude/hooks/outcome-block-check.sh"}]}]}}
```

It needs `jq`. Warn mode is the default and prints the reminder only. To enforce, set
`OUTCOME_CHECK_MODE=block` (any letter case) in the environment the agent launches with. Expected result
on a plan with no outcome: in warn mode, a stderr block that starts `[outcome-block-check]` and names
`## Outcome`, and the plan goes through; in block mode the same message and the plan is refused (exit 2).
Fix: add a `## Outcome` section on a line of its own, outside any code block (KPI, lift, measured on,
consumer, decision) plus a rollback and a verification step, then present the plan again. If `jq` is
missing: warn mode stays silent and checks nothing; block mode refuses every plan with one line,
`[outcome-block-check] jq not found; block mode cannot check plans`; install `jq` and present the plan
again. For a one-line fix or a pure research plan, set `OUTCOME_CHECK_DISABLED=1` for the session.
Unknown mode values behave as warn.

Where the hook reads the plan from (both modes): the plan text in the tool input if there is any; otherwise the
last plan file (a markdown file in a plans folder under .claude) that the session's transcript records
being written or edited (a read of a plan does not count), read if it still exists. Warn mode reads the same two sources in the same order, but it never
refuses: with no jq, unreadable input, or no plan found it prints nothing and lets the plan through. In
block mode the hook never passes silently on input it cannot use. It refuses with one line:
`[outcome-block-check] cannot read hook input` when the input is empty or not valid JSON, and
`[outcome-block-check] no plan found in tool input or plan file` when the plan tool ran but neither
source gave a plan. Any other tool passes.
A heading inside a code fence does not count; a fence closes only on a line of the same character, at
least as long, with nothing after it.

 To remove the module, delete the hook entry you added, then run
`remove-module`.

## State

Two files in the target's `.claude` folder (a rule and a hook script) and their manifest records. The
hook reads the plan from its input (or the plan file the transcript names) and keeps no state. Its twin, `decision-gating-technical`, ships only
the technical rule; this module's hook checks both rules, and the audit script expects that twin rule: with
only this module installed it reports the technical rule file as MISSING. Install the twin to clear it.
