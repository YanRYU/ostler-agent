# Codex Installer Design

## Intent

Ostler currently installs repository governance for Claude Code. Add a Codex installation path so a repository owner can install equivalent guidance and the controls Codex actually supports, then check for drift with Ostler's existing manifest workflow. The Claude installation path must continue to work.

This design assumes the first release covers Ostler's existing layers: instructions, command policy, hooks, a skill template, and drift audit. It does not promise identical enforcement across the two agents.

## User workflow

From an Ostler checkout, run `devkit/install/codex-install.sh install /path/to/target-repo`. The wrapper selects Codex modules and uses the existing planner, lock, transaction journal, manifest, refusal behavior, and `--allow-dirty` / `--force` flags. `check`, `outdated`, `patch`, `remove-module`, and `recover` remain available through the same wrapper. The existing `devkit-install.sh` keeps its Claude defaults.

The target is a local repository. Installing the Codex hooks module requires the target to be the Git repository root. A Codex install places repository-level files in that target, not in the user's global Codex configuration. Reinstall is idempotent; unmanaged or edited files are refused unless the user explicitly passes `--force`. The target can be used without the Ostler checkout after installation.

## Installed layers

| Ostler layer | Codex output | Behavior |
|---|---|---|
| Entry and instructions | `AGENTS.md` plus `.agents/ostler/rules/*.md` | Short persistent instructions point to detailed rules. Existing unmanaged `AGENTS.md` is preserved by refusal. |
| Command permissions | `.codex/rules/ostler.rules` | Codex `prefix_rule` entries cover command approval or denial outside the sandbox. They do not reproduce Claude's file read/write deny list. |
| Hooks | `.codex/hooks.json` and `.codex/hooks/*` | Port branch and stash notices; adapt the personal-path check for Codex `apply_patch` input. Do not ship the Claude plan-exit hook, whose transcript and plan-path assumptions do not hold in Codex. |
| Skill template | `.agents/ostler/templates/EXAMPLE-SKILL/SKILL.md` | A starter skill outside Codex's discovery path; copy and customize it into `.agents/skills/` to activate it. |
| Drift audit | `.codex/devkit-audit.py` | Self-contained standard-library checker for manifest-managed files and Codex hook/rule references. |

Codex requires review and trust before non-managed hooks run. Project-local `.codex` hooks and command rules also require the project config layer to be trusted. The installer prints these activation steps and never reports a copied file as an active control without runtime verification. Command prefix rules and edit hooks cover only matching tool paths; they are not a complete security boundary. [Codex hooks](https://learn.chatgpt.com/docs/hooks), [Codex rules](https://learn.chatgpt.com/docs/agent-configuration/rules), [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md), [skills](https://learn.chatgpt.com/docs/build-skills).

## Architecture

Use a separate Codex module catalogue under `devkit/codex-modules/`, with `codex-` names so both catalogues can be represented in the same target manifest without collisions. Add platform selection to the current Python installer rather than duplicating its transaction engine. A thin Codex shell wrapper passes the platform selection. Shared `check` reads the target manifest regardless of which wrapper installed it; platform-specific `outdated` checks only modules from that catalogue.

Keep the existing manifest and transaction metadata filenames so mixed Claude/Codex installations are serialized by one lock and audited together. Each module owns only its own destination paths. Audit code may be copied into both payload catalogues, with a test ensuring the two copies stay identical.

## Failure behavior

An install must refuse before writing if a destination escapes the target, a source module is dirty without `--allow-dirty`, a target file is unmanaged or modified, a hook configuration is malformed, two modules claim colliding paths, or the hooks target is not the Git repository root. The existing transaction library remains the only mutating path. Installing or removing Codex modules cannot delete Claude-managed files, and vice versa. A copied hook that has not been trusted is reported as installed but not verified active.

## Verification

1. Keep the existing Claude transaction, feedback, and hook tests green.
2. Add tests for Codex module discovery, install, repeat install, refusal without partial writes, update, remove, recover, and mixed-platform manifest behavior.
3. Exercise the generated hook scripts with Codex-shaped JSON fixtures and check the generated JSON and rule syntax.
4. Run an end-to-end installation into a disposable Git repository, then run the factory and installed audit scripts. Document that hook activation requires an interactive trust review in Codex.

## Scope boundary

This release installs into a repository. It does not edit `~/.codex`, import a Claude installation automatically, publish a plugin, or claim to intercept every tool path. Codex rule files are experimental and only govern commands outside the sandbox; hooks cover supported local tool paths and require trust.
