---
name: decision-gating-technical
version: "1.0.4"
domain: decision-gating-technical
depends-on: []
payload:
  - { src: "rules/technical-gate.md", dest: ".claude/rules/technical-gate.md", mode: overwrite, mechanism: INSTRUCT }
---
# decision-gating-technical

## Prevents

Shipping a change with no rollback, no end-to-end proof and no named blast radius.

One rule (the technical gate: blast radius, rollback, verification, order). It is an instruction only:
no BLOCK surface in this module. The hook in `decision-gating-business` looks for a rollback and a
verification step in every plan at plan exit, but only when that module is installed and its hook is
wired.

## Procedure

Nothing to wire. The rule is read by the agent at the start of a session. To have the rule checked
mechanically, install `decision-gating-business` as well and wire its hook (its `## Procedure` has the
exact settings entry). Without that hook this rule is advice and nothing stops a plan that ignores it.

## State

One rule file in the target's `.claude` folder and its manifest record. Keeps no other state.
