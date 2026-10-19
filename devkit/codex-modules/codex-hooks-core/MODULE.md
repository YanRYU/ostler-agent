---
name: codex-hooks-core
version: "1.0.0"
layer: DETECT
depends-on: [codex-instructions]
payload:
  - { src: "hooks.json", dest: ".codex/hooks.json", mode: overwrite }
  - { src: "hooks/branch_similarity.py", dest: ".codex/hooks/branch_similarity.py", mode: overwrite }
  - { src: "hooks/stash_leak.py", dest: ".codex/hooks/stash_leak.py", mode: overwrite }
  - { src: "hooks/personal_path_guard.py", dest: ".codex/hooks/personal_path_guard.py", mode: overwrite }
---

# Codex hooks

Repository-local notices for existing branches, leftover stashes, and personal absolute paths in file edits. Codex must trust the project config and the user must review these hooks with `/hooks` before they run.
