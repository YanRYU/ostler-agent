# Codex Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task by task. Steps use checkbox syntax for tracking.

**Goal:** Add a repository-scoped Codex installer that preserves the existing Claude installer and uses Ostler's transaction and audit workflow.

**Architecture:** A Codex wrapper selects a separate `devkit/codex-modules` catalogue in the current Python installer. Modules copy Codex-native instructions, command rules, hooks, skill template, and audit script. Both platforms share one target manifest and transaction lock.

**Tech Stack:** Bash, Python 3, PyYAML (factory only), JSON, Codex `AGENTS.md` / hooks / rules / skills.

**Spec:** `docs/superpowers/specs/2026-10-19-codex-installer-design.md`

## Global Constraints

- Existing `devkit/install/devkit-install.sh` retains Claude defaults.
- Codex installation is repository-scoped and self-contained; no writes to `~/.codex`.
- Existing transaction library remains the only mutating path; unmanaged and modified files refuse without `--force`.
- Codex command rules apply outside the sandbox; non-managed hooks require trust review.

## Review Focus

- A target containing both platforms keeps every managed file when either platform is updated (Task 1 test).
- A modified `AGENTS.md` or `hooks.json` produces refusal without partial writes (Task 2 test).
- An untrusted Codex hook is never reported as active merely because files were copied (Task 3 output test).
- A Codex `apply_patch` input containing a personal absolute path is detected only for shared files (Task 3 hook test).
- A removed Codex module leaves Claude manifest records intact (Task 4 test).

---

### Task 1: Platform selection and shared manifest

**Files:** Modify `devkit/install/_devkit.py`; create `devkit/install/codex-install.sh`; create `devkit/tests/test_codex_installer.py`.

**Interfaces:** The wrapper calls `_devkit.py --platform codex <subcommand>`. `discover_modules()` reads the catalogue selected by `--platform`. `outdated` examines only modules in that catalogue. All other commands retain their existing signatures and transaction path.

- [ ] Write tests that invoke both wrappers, assert default module names are platform-specific, and assert a mixed manifest retains the first platform after installing the second.
- [ ] Run `PYTHONPATH=/private/tmp/ostler-pydeps python3 -m unittest devkit.tests.test_codex_installer -v`; confirm failures come from the missing Codex entry point and platform selection.
- [ ] Add CLI platform selection, namespace-aware catalogue discovery and `outdated`, and the Codex wrapper.
- [ ] Run the focused test until it passes; run the existing transaction suite.
- [ ] Commit the platform selection and its tests.

### Task 2: Codex instructions, command rules, skill template, and audit

**Files:** Create `devkit/codex-modules/codex-instructions/`, `codex-command-rules/`, `codex-skill-template/`, `codex-audit/` with `MODULE.md` and payloads; modify `devkit/modules/audit/payload/devkit-audit.py`; add cases to `devkit/tests/test_codex_installer.py`.

**Interfaces:** Each module has a unique `codex-` name and the existing `payload` metadata contract. The audit payload remains standard-library-only and can inspect both `.claude` and Codex layouts.

- [ ] Add tests for the installed paths, self-contained audit, idempotent reinstall, refusal on modified/unmanaged files, and a valid Codex rule file.
- [ ] Run focused tests and confirm each fails because the Codex module or behavior is absent.
- [ ] Add the four modules and adapt audit layout discovery; keep the two audit payload copies byte-identical.
- [ ] Run focused tests and the existing feedback suite until green.
- [ ] Commit modules and tests.

### Task 3: Codex hooks and activation guidance

**Files:** Create `devkit/codex-modules/codex-hooks-core/` with `hooks.json` and scripts; modify `devkit/install/_devkit.py` output; add hook cases to `devkit/tests/test_codex_installer.py`.

**Interfaces:** Hooks receive Codex's JSON object on stdin. `PreToolUse` can inspect `tool_name` and `tool_input`; a warning uses `hookSpecificOutput.additionalContext`. The installer prints project and hook trust instructions after installation.

- [ ] Add fixture-driven tests for branch/stash notices, shared-file personal-path detection in `apply_patch`, private-file exclusion, JSON configuration, and activation output.
- [ ] Run focused tests and confirm the missing hook files/behavior cause the failures.
- [ ] Implement Codex-specific hook scripts and configuration; validate hook JSON before any install transaction.
- [ ] Run focused tests until green and verify scripts with representative stdin fixtures.
- [ ] Commit hooks and tests.

### Task 4: End-to-end behavior and documentation

**Files:** Modify `README.md`, `devkit/README.md`; extend `devkit/tests/test_codex_installer.py` if needed.

**Interfaces:** `codex-install.sh` exposes `install`, `check`, `outdated`, `patch`, `remove-module`, and `recover` with the same argument order as `devkit-install.sh`.

- [ ] Add a disposable Git-repo end-to-end test for install, factory check, installed audit, update, remove, and preservation of Claude records.
- [ ] Run it red where behavior is incomplete, implement the missing behavior, then run it green.
- [ ] Document commands, output paths, trust review, and the non-equivalence of Codex and Claude permissions.
- [ ] Run all repository suites, `git diff --check`, and a manual disposable-target smoke test.
- [ ] Commit the final tests and documentation; report branch and verification for review, without merging.
