# Ostler governance for Codex

- Before changing repository state, check the current branch and any pre-existing changes. Do not move or discard another session's work.
- Resolve every stash you create before this session ends, or identify it and hand it off to the user. Leave pre-existing stashes alone.
- Do not put personal absolute paths in shared repository files. Use relative paths or documented environment variables.
- When this repository ships agent configuration, copy its dependencies into the repository; do not require runtime reads from a maintainer's home directory.
- For plans, inspect the files they discuss and distinguish verified behavior from inference.

The detailed conventions are in `.agents/ostler/rules/`. Codex command rules and hooks live in `.codex/` when those modules are installed. They require the project's Codex config layer and hooks to be trusted before they run.
