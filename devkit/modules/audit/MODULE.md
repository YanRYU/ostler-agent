---
name: audit
version: "1.2.7"
domain: all
depends-on: []
payload:
  - { src: "devkit-audit.py", dest: ".claude/devkit-audit.py", mode: overwrite, mechanism: VERIFY }
---
# audit

## Prevents

Silent drift: installed files that were edited, deleted or left unwired without anyone noticing.

A checker that recomputes state from the manifest (no hand-maintained list). Stdlib-only Python
(self-contained, so it runs in a target with the kit's source folder deleted). Three jobs:

1. **Drift** — reads `<target>/.devkit-manifest.json`, validates any
   `patched_pending_upstream` ledger **before** classifying a single file (a malformed
   ledger exits 2 naming every offending entry), then recomputes SHA-256 per file and
   classifies each: `clean` / `modified-downstream` / `pending-upstream` (+ age, or
   `STALE` with a refresh command) / `forked` (in-file `devkit:forked` marker) /
   `missing`. Exits non-zero on `modified-downstream`, `missing`, or a stale
   pending-upstream entry. For each `pending-upstream` file it prints a copy-paste
   command that appends the fix to a local upstream-feedback log (never auto-run; see the Patch ledger section of the devkit README).
   A `mode: merge` file (the `settings.json` several modules share) is `clean` while it is valid JSON and every permission
   pattern the modules recorded is still in it; hooks, `allow` entries and patterns of your own that you add are not drift,
   with one exception: `permissions.defaultMode` set to `bypassPermissions` (every tool call runs without asking) is
   reported `modified-downstream` with a reason line. Not inspected: a hook that returns its own permission decision
   (for example an auto-allow `PreToolUse` hook), and what any hook script does.
2. **Enforcement gaps** — scans the target's hooks directory headers for `Enforces: rules/<x>` and
   its rules directory, reporting hooks without a rule file and rules without an enforcing
   hook (informational; does not fail the exit code).
3. **Board** — `python3 devkit-audit.py board [target_dir]` prints one card per installed module,
   built from the manifest (domain, per-file mechanism, the module's Prevents / Procedure / State
   text) and the hook wiring found in the `settings.json` in the target's `.claude` folder. Five faces per card:
   purpose, surfaces, principles, procedure, state.

## Procedure

Usage: `python3 devkit-audit.py [target_dir]`. With no argument it audits the repo root: the
current directory if it holds `.devkit-manifest.json`, else the parent of the directory the
script sits in (the repo root when the script sits in the target's `.claude` folder), else the script's own directory.
The kit installer's `check` subcommand (run from the kit's source folder) runs the **source folder's own copy** of this script against
the target, not the copy installed into the target (so the copy installed in a target is
only exercised when you run `python3 .claude/devkit-audit.py .` inside that target).

## State

Reads `<target>/.devkit-manifest.json` and the files it lists; writes nothing. The only state it
mentions is the local feedback log described below.

Record schema (one JSON object per line): `{"ts", "reason", "path"}`:
`ts` is a UTC timestamp, `reason` is the text you gave `--reason`, `path` is the target-relative
file the fix is about.

Feedback log: a local JSONL record of the fixes you recorded with `patch`; review it with any
JSON tool, or attach it to an issue when contributing a fix back to the kit (the kit takes issues, not pull requests).
The printed upstream command appends one record per fix to a single file, `feedback.jsonl`,
in this directory: `$DEVKIT_FEEDBACK_DIR` if it is an absolute path; otherwise `<home>/devkit-feedback`,
where `<home>` is the devkit home: `$DEVKIT_HOME` if absolute, else `$HOME/.devkit`.
Non-absolute or control-character values are ignored. Resolved when the audit runs, not when
you paste the command.

A deliberate downstream edit you want to keep (and perhaps contribute back) is recorded via
`devkit-install.sh patch <target> <path> --reason "<why>"` (run from the kit's source folder; see
`devkit/install/_devkit.py`), which stamps a `patched_pending_upstream` entry this
script then reports as `pending-upstream` instead of failing the drift check.
