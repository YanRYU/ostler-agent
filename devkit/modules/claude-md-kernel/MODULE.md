---
name: claude-md-kernel
version: "1.0.4"
domain: all
depends-on: []
payload:
  - { src: "CLAUDE.md", dest: "CLAUDE.md", mode: overwrite, mechanism: ROUTE }
---
# claude-md-kernel

## Prevents

Pointer rot: a CLAUDE.md that restates rules the enforcing file already holds, so the two drift apart.

A pointer-style CLAUDE.md template (identity plus short pointers to where each rule lives, never
restates content held by the file that enforces it). Four sections: About this agent, Hard rules (a
bulleted list; each bullet names what enforces it), Where the rules live, Which skill to load.

## Procedure

Install it, then fill every `{placeholder}`. `mode: overwrite` — but a target that already has a hand-tuned CLAUDE.md will show as
`modified-downstream` on `--check` and the installer will refuse to clobber it without
`--force`.

## State

One file, `CLAUDE.md` at the target root. No state outside it.
