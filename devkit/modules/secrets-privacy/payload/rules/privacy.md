# Privacy — No Personal Paths in Shared Artifacts

**Always-on. Applies in every repo. Enforced by hook:
`personal-path-guard.py` (PreToolUse: Write/Edit/NotebookEdit).**

---

## The rule

Never write a personal absolute path (`/Users/<name>/...`, `/home/<name>/...`) into a
**shared artifact** — any file inside a git-tracked, non-ignored repository. Personal
paths leak each user's directory layout into shared docs/scripts/configs and break
portability: a teammate must find-and-replace before they can use the artifact.

Use portable forms instead:
- `$HOME` / `${HOME}` / `~/` for home-relative paths
- `$REPO_ROOT` or a documented project env var for repo-relative paths
- relative paths from the repo root where possible

## Where personal paths are fine (not flagged)

- Files outside any git repo (machine-local).
- Git-ignored files (private, not shared).
- Known machine-local configs: `~/.zshrc`, `~/.bashrc`, `~/.profile`, `.env`,
  `settings.local.json`, and the agent's own user-level config dir.

## Mode

The hook runs in `warn` mode by default (loud stderr, allows the write). Set
`PERSONAL_PATH_GUARD_MODE=block` to make it block the tool call once the false-positive
rate is acceptable on your repos.
