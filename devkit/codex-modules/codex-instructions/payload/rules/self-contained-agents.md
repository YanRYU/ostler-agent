# Self-contained agents

A repository that ships agent configuration must work after the source checkout or maintainer's local setup is removed. Copy skills, hook scripts, and other needed resources into the target repository. Avoid symlinks and runtime references to a maintainer's home directory.

Keep each constraint in the layer best able to express it: `AGENTS.md` for always-read guidance, `.agents/skills/` for reusable workflows, `.codex/hooks/` for supported lifecycle checks, `.codex/rules/` for out-of-sandbox command policy, and the manifest audit for drift. Do not describe an instruction as enforced unless a tested control actually enforces it.
