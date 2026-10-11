---
name: claude-md-kernel
version: "1.0.0"
layer: POINTS
depends-on: []
payload:
  - { src: "CLAUDE.md", dest: "CLAUDE.md", mode: overwrite }
---
# claude-md-kernel

The POINTS layer: a pointer-style CLAUDE.md template (identity plus
short pointers to where each rule lives, never restates content held by the file that enforces it). Four
sections: About this agent, Hard rules (a bulleted list; each bullet names what enforces it), Where the rules live, Which skill to load.

`mode: overwrite` — but a target that already has a hand-tuned CLAUDE.md will show as
`modified-downstream` on `--check` and the installer will refuse to clobber it without
`--force`. Fill every `{placeholder}` before use.
