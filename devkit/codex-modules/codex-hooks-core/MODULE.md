---
name: codex-hooks-core
version: "1.0.0"
domain: all
depends-on: [codex-instructions]
payload:
  - { src: "hooks.json", dest: ".codex/hooks.json", mode: overwrite, mechanism: ROUTE }
  - { src: "hooks/branch_similarity.py", dest: ".codex/hooks/branch_similarity.py", mode: overwrite, mechanism: DETECT }
  - { src: "hooks/stash_leak.py", dest: ".codex/hooks/stash_leak.py", mode: overwrite, mechanism: DETECT }
  - { src: "hooks/personal_path_guard.py", dest: ".codex/hooks/personal_path_guard.py", mode: overwrite, mechanism: DETECT }
---

# Codex hooks

Repository-local notices for existing branches, leftover stashes, and personal absolute paths in file edits. Codex must trust the project config and the user must review these hooks with `/hooks` before they run.

## Prevents
Unnoticed branch collisions, leftover stashes, and personal paths in edits.

## Procedure
Trust the project configuration and review the hooks with `/hooks` before use.

## State
Hooks are copied but their activation depends on Codex project trust.
