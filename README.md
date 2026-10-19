# Ostler

Ostler is a governance kit for coding agents working on real repositories. It copies a
layered set of guardrails (permission rules, hooks that surface or stop risky actions,
behavioral rules, a drift checker, a self-audit) into a repo's Claude Code config directory,
and records a SHA-256 manifest so you can later prove nothing has drifted.

Everything is copied, never linked: a target repo works with this clone deleted. The unit
you install, and the unit you write yourself, is a **module** (a directory with a
`MODULE.md` and the files it ships). The installer is `devkit/install/devkit-install.sh`.
This is kit 2.0: modules are grouped by what they protect (see "Modules and domains").

## Prerequisites

- `bash`, `git`, and `python3` (tested on 3.13), on macOS or Linux. The installer uses
  `fcntl` file locking, so native Windows is not supported (use WSL).
- **`jq`**, only if you wire the hooks into your `settings.json` (install it with your
  package manager, for example `brew install jq` or `apt install jq`). Without it the
  `stash-leak-check.sh` and `branch-similarity-check.sh` hooks silently do nothing (they
  exit 0 and print nothing), `plan-exit-print-location.sh` prints
  `Plan file: (unable to determine — no transcript)`, and the decision gate in warn mode
  prints nothing, while in block mode it
  refuses every plan with `[outcome-block-check] jq not found; block mode cannot check plans`.
  The self-audit scanner (`scan.sh`) also needs `jq` and stops with `scan.sh: jq is required`.
  The installer and the drift check do not need it.
- **PyYAML**, which is not part of a stock `python3`. Without it the installer stops
  immediately (exit 1) with a Python traceback whose last lines are:

  ```
      import yaml  # PyYAML — factory-side only
  ModuleNotFoundError: No module named 'yaml'
  ```

  Fix: `python3 -m pip install pyyaml` (or `pip install --user pyyaml`, or your OS
  package, e.g. `apt install python3-yaml`). Only the installer needs it; the audit
  script and the self-audit scripts it installs into your target are stdlib-only.
- **A git identity** (`user.name` and `user.email`), for the `git commit` steps in "Create your
  own module" (step 3, and the "edit it, commit" step at the end) and in the exit-7 fix. Check with
  `git config user.email` (it should print an address). If none is configured, the commit stops
  with `Author identity unknown` and `fatal: unable to auto-detect email address`. Fix:
  `git config --global user.name "Your Name"` and
  `git config --global user.email "you@example.com"`, then re-run the commit.
- **Claude Code**, installed and signed in (run `claude` once in a terminal and finish the sign-in), but
  only for the steps that use the agent itself: wiring the hooks, trying the decision gate on a live plan,
  and watching a hook fire. Installing, the drift check, the self-audit and the tests do not need it.
- A target directory that is **outside** this clone. Installing into the clone itself,
  a folder inside it, or a folder that contains it is refused (exit 8).

## Quick start

Install every module into a target repo. The target may be an existing repo or a new
empty directory (it is created if missing).

`<repository-url>` is the address of the project's official repository, the one this README is
published from. Use that address as shown on its page.

```bash
git clone <repository-url> ostler
cd ostler
devkit/install/devkit-install.sh install /path/to/your-repo
```

Expected output (the last line carries your clone's commit hash; the target path is
whatever you passed). Exit code 0:

<!-- output: install-all -->
```
[devkit] install order: ['audit', 'claude-md-kernel', 'decision-gating-business', 'decision-gating-technical', 'git-multi-session', 'harness-governance', 'observe', 'secrets-privacy', 'skill-template', 'system-safety', 'work-craft']
  write  new             .claude/devkit-audit.py
  write  new             CLAUDE.md
  write  new             .claude/rules/biz-outcome-gate.md
  write  new             .claude/hooks/outcome-block-check.sh
  write  new             .claude/rules/technical-gate.md
  write  new             .claude/rules/git-hygiene.md
  write  new             .claude/hooks/stash-leak-check.sh
  write  new             .claude/hooks/branch-similarity-check.sh
  write  new             .claude/scripts/harness-audit/scan.sh
  write  new             .claude/scripts/harness-audit/diff-registry.py
  write  new             .claude/scripts/harness-audit/registry.py
  write  new             .claude/harness-registry/_schema.json
  write  new             .claude/skills/harness-audit/SKILL.md
  write  new             .claude/rules/harness-governance.md
  write  new             .claude/hooks/plan-exit-print-location.sh
  write  new             .claude/hooks/personal-path-guard.py
  write  new             .claude/rules/privacy.md
  write  new             .claude/skills/EXAMPLE-SKILL/SKILL.md
  write  new             .claude/rules/plan-writing.md
  write  new             .claude/rules/self-contained-agents.md
  write  new             .claude/rules/output-quality-standards.md
  write  new             .claude/rules/communication-style.md
  merge  permissions     .claude/settings.json
[devkit] wrote <target>/.devkit-manifest.json (source_commit=<hash>, source_dirty=false)
```

To install only some modules, name them (dependencies are ordered for you, and a missing
dependency prints a `WARN`):

```bash
devkit/install/devkit-install.sh install /path/to/your-repo git-multi-session work-craft
```

One thing to know before you install all of them into a repo you already use:
`claude-md-kernel` writes a template `CLAUDE.md`, and if your repo already has one the
install is refused (see "When things go wrong"). Leave that module out if you keep your own.

The hooks are copied but **not wired up**: nothing runs them until you add the entries in
"Wire the hooks" below. The two permission modules (`system-safety`, `secrets-privacy`)
merge deny/ask lists into your existing `settings.json` and never touch your own `allow`
entries.

## Modules and domains

A module is **one domain**: a group of controls that protect one thing. The domain is the
`domain:` line in the module's `MODULE.md` (the accepted values are listed in
`devkit/domains.json`). Inside a module, **every shipped file carries the mechanism it
works by**, the `mechanism:` key on its `payload` entry:

| Mechanism | What the file does |
|---|---|
| `BLOCK` | Stops an action (a deny pattern, or a hook that exits 2). |
| `ASK` | Makes the agent ask you first. |
| `DETECT` | Warns or reports, then lets the action through. |
| `INSTRUCT` | A rule or skill the agent reads. |
| `ROUTE` | A pointer that sends the agent to the file that holds a rule. |
| `VERIFY` | A check that compares what is installed with what should be. |

A file's mechanism is its **default** behavior. A hook that only blocks when you switch it
on (the decision gate with `OUTCOME_CHECK_MODE=block`, the path guard with
`PERSONAL_PATH_GUARD_MODE=block`) is declared `DETECT`, and its module says what block mode does.

The list of shipped modules, with version, domain, mechanisms and the one problem each
prevents, is the generated table in `devkit/README.md` (it is produced from the `MODULE.md`
files, so it cannot go stale). Each module's `MODULE.md` has three fixed sections:
`## Prevents` (the problem), `## Procedure` (what you do after installing) and `## State`
(what it leaves on disk). To see them for what you installed, run the board in your target repo:

```bash
cd /path/to/your-repo
python3 .claude/devkit-audit.py board .
```

It prints one card per installed module with five faces (purpose, surfaces, principles,
procedure, state), read from the install manifest and from the hooks you wired.

## Wire the hooks

Add this to the settings file in your target's `.claude` folder (`settings.json`). If the
file already has a `hooks` entry, merge the lists instead of replacing yours. Keep only the
entries for the modules you installed.

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "ExitPlanMode",
       "hooks": [{"type": "command", "command": "bash .claude/hooks/outcome-block-check.sh"}]},
      {"matcher": "Bash",
       "hooks": [{"type": "command", "command": "bash .claude/hooks/branch-similarity-check.sh"}]},
      {"matcher": "Write|Edit|NotebookEdit",
       "hooks": [{"type": "command", "command": "python3 .claude/hooks/personal-path-guard.py"}]}
    ],
    "PostToolUse": [
      {"matcher": "Bash",
       "hooks": [{"type": "command", "command": "bash .claude/hooks/stash-leak-check.sh"}]},
      {"matcher": "ExitPlanMode",
       "hooks": [{"type": "command", "command": "bash .claude/hooks/plan-exit-print-location.sh"}]}
    ]
  }
}
```

| Hook | Module | Default | Switch |
|---|---|---|---|
| `outcome-block-check.sh` | `decision-gating-business` | warns when a plan has no `## Outcome` block | `OUTCOME_CHECK_MODE=block` refuses the plan (exit 2); `OUTCOME_CHECK_DISABLED=1` silences it |
| `personal-path-guard.py` | `secrets-privacy` | warns when a shared file gets a personal absolute path | `PERSONAL_PATH_GUARD_MODE=block` refuses the write (exit 2) |
| `branch-similarity-check.sh` | `git-multi-session` | prints similar existing branches before you create one | `BRANCH_CHECK_DISABLED=1` silences it |
| `stash-leak-check.sh` | `git-multi-session` | prints leftover stashes after a push or commit | none (stateless) |
| `plan-exit-print-location.sh` | `observe` | prints the plan file's path after plan mode | none (leave the entry out to skip it) |

Set a switch in the environment Claude Code launches with, for example:

```bash
OUTCOME_CHECK_MODE=block claude
```

Wiring a hook does not make the drift check (below) fail: `settings.json` is shared with
you, so hooks and `allow` entries you add are yours. What the drift check does catch there is
a deny or ask pattern a module installed that you later removed, or a file that is no longer valid JSON.

### Try the decision gate

The gate runs when Claude Code leaves plan mode. To see it without a live session, feed it
the two fixture plans that ship with the kit. Run this from your target repo (replace
`/path/to/ostler` with your clone):

```bash
export OUTCOME_CHECK_MODE=block
jq -Rs '{tool_name:"ExitPlanMode",tool_input:{plan:.}}' /path/to/ostler/devkit/tests/fixtures/plan-no-outcome.md \
  | bash .claude/hooks/outcome-block-check.sh; echo "exit $?"
```

Expected output (the plan has no `## Outcome` block, so it is refused):

<!-- output: gate-block -->
```
[outcome-block-check] this plan is missing a decision-gate block:
  • Business (biz-outcome-gate.md): no ## Outcome block. Add — the business KPI that moves
    (not an internal proxy) / expected lift / measured-on (date + how you will check it, e.g. a query, dashboard or test) / consumer (who relies on it) / decision (what it changes). If this is a
    one-line fix or pure research, the gate doesn't apply — carry on.
  Blocking (OUTCOME_CHECK_MODE=block). Add the missing block, or set OUTCOME_CHECK_MODE=warn / OUTCOME_CHECK_DISABLED=1.
exit 2
```

```bash
jq -Rs '{tool_name:"ExitPlanMode",tool_input:{plan:.}}' /path/to/ostler/devkit/tests/fixtures/plan-with-outcome.md \
  | bash .claude/hooks/outcome-block-check.sh; echo "exit $?"
```

Expected output (the plan carries the block, a rollback and a verification step, so it passes):

<!-- output: gate-pass -->
```
exit 0
```

In a live session, start `claude` with `OUTCOME_CHECK_MODE=block`, enter plan mode, and let it
present a plan: a plan without an `## Outcome` line on its own is refused with the same message.
Without `OUTCOME_CHECK_MODE=block` (warn mode, the shipped default) the plan goes through, and the reminder is
not printed in the chat itself: open the transcript view (press Ctrl-O) or start `claude --verbose` to see it.
In a live session the plan text is usually not in the tool input, so in both modes the hook reads the plan file
Claude Code wrote (the last plans-folder markdown file the session transcript shows being written or edited).
If you see nothing at all, the hook is not wired (check the JSON above is in the settings file
in your target's `.claude` folder) or `jq` is missing.

## Check

```bash
devkit/install/devkit-install.sh --check /path/to/your-repo
```

Expected output on a clean target (exit code 0):

<!-- output: check-clean -->
```
[audit] drift check: <target>
  OK clean                    audit              .claude/devkit-audit.py
  OK clean                    claude-md-kernel   CLAUDE.md
  OK clean                    decision-gating-business .claude/rules/biz-outcome-gate.md
  OK clean                    decision-gating-business .claude/hooks/outcome-block-check.sh
  OK clean                    decision-gating-technical .claude/rules/technical-gate.md
  OK clean                    git-multi-session  .claude/rules/git-hygiene.md
  OK clean                    git-multi-session  .claude/hooks/stash-leak-check.sh
  OK clean                    git-multi-session  .claude/hooks/branch-similarity-check.sh
  OK clean                    harness-governance .claude/scripts/harness-audit/scan.sh
  OK clean                    harness-governance .claude/scripts/harness-audit/diff-registry.py
  OK clean                    harness-governance .claude/scripts/harness-audit/registry.py
  OK clean                    harness-governance .claude/harness-registry/_schema.json
  OK clean                    harness-governance .claude/skills/harness-audit/SKILL.md
  OK clean                    harness-governance .claude/rules/harness-governance.md
  OK clean                    observe            .claude/hooks/plan-exit-print-location.sh
  OK clean                    secrets-privacy    .claude/settings.json
  OK clean                    secrets-privacy    .claude/hooks/personal-path-guard.py
  OK clean                    secrets-privacy    .claude/rules/privacy.md
  OK clean                    skill-template     .claude/skills/EXAMPLE-SKILL/SKILL.md
  OK clean                    system-safety      .claude/settings.json
  OK clean                    work-craft         .claude/rules/plan-writing.md
  OK clean                    work-craft         .claude/rules/self-contained-agents.md
  OK clean                    work-craft         .claude/rules/output-quality-standards.md
  OK clean                    work-craft         .claude/rules/communication-style.md
[audit] summary: clean=24
[audit] enforcement-gap scan: <target>/.claude/hooks
  -- rule communication-style.md: no hook declares it (may be instruction-only)
  -- rule harness-governance.md: no hook declares it (may be instruction-only)
  -- rule output-quality-standards.md: no hook declares it (may be instruction-only)
  -- rule plan-writing.md: no hook declares it (may be instruction-only)
  -- rule self-contained-agents.md: no hook declares it (may be instruction-only)
```

The `--` lines are informational (a rule with no hook enforcing it); they do not affect
the exit code. What does: a managed file that you edited shows
`!! modified-downstream` and the check exits 2.

### What --check covers

For every file the kit installed, `--check` compares the file's SHA-256 with the manifest. The one file shared
with you, `settings.json`, is judged differently: it stays `OK clean` while it is valid JSON and every permission
pattern the modules put there is still present, so your own hooks, `allow` entries and extra patterns are not drift.
Two things in it do count: losing a pattern a module added (including moving a `deny` or `ask` pattern into
`allow`), and `permissions.defaultMode` set to `bypassPermissions`, which turns permission prompts off. Either
shows `!! modified-downstream` (the `bypassPermissions` case prints a reason in parentheses) and exit code 2.
What `--check` does **not** look at: a hook, in your `settings.json`, that returns its own permission decision
(for example a `PreToolUse` hook that approves every command), and what any hook script does when it runs. It also does not read `settings.local.json`, the personal settings
file that Claude Code layers over `settings.json`: a `bypassPermissions` mode, an `allow` entry or a hook set
there goes unreported.

The `[audit] enforcement-gap scan` section appears only when the target has a hooks folder
(a `hooks` folder inside the target's `.claude` folder, or directly under the target). If it has none (for example you installed only
`work-craft` into a repo with no hooks folder), the section is omitted and the output ends at
the summary line, still with exit code 0:

<!-- output: check-one -->
```
[audit] drift check: <target>
  OK clean                    work-craft         .claude/rules/plan-writing.md
  OK clean                    work-craft         .claude/rules/self-contained-agents.md
  OK clean                    work-craft         .claude/rules/output-quality-standards.md
  OK clean                    work-craft         .claude/rules/communication-style.md
[audit] summary: clean=4
```

A missing scan section is not an error.

After you edit a template such as `CLAUDE.md` (you are meant to fill its `{placeholders}`),
`--check` will flag it. Two ways to say "this file is mine now": put the text
`devkit:` immediately followed by `forked` in the file (for example in a comment) and
the check reports `OK forked`, and re-installs skip it; or record it as a deliberate local
change with `devkit/install/devkit-install.sh patch` (the check then reports it as `pending-upstream` instead
of failing; nothing is sent anywhere, so to contribute the fix back, open an issue on the
repository you cloned from; see `devkit/README.md`, "Patch ledger", and "Issues, not pull
requests" below).

## Self-audit: is the harness what you think it is?

The `harness-governance` module lets a repo audit its own `.claude` folder against a baseline it
owns. It needs only `bash`, `jq` and `python3` inside the target (no clone, no PyYAML). Wire the
hooks first (above), then, from the target's root, create the baseline once:

```bash
python3 .claude/scripts/harness-audit/registry.py init .
```

Expected output (the counts are for the full install above). Exit code 0:

<!-- output: registry-init -->
```
registry init: seeded 20 entries into 8 files under <target>/.claude/harness-registry (0 need curation: domain TODO-curate)
```

Each artifact's `domain` is copied from the module that installed it. An artifact no module
installed gets `domain: TODO-curate`, for you to set. Now audit:

```bash
mkdir -p .claude/harness-registry/reports
bash .claude/scripts/harness-audit/scan.sh . --out .claude/harness-registry/reports/scan.json
python3 .claude/scripts/harness-audit/diff-registry.py .claude/harness-registry/reports/scan.json .claude/harness-registry
```

Expected output right after `init` (exit code 0; the `ignored` line is the example skill, which is a
template and never a registry entry):

<!-- output: audit-clean -->
```
registry-vs-reality: CLEAN (0 diffs)
ignored: 1 (skill-EXAMPLE-SKILL)
```

Now plant a hook nobody wired, as if a teammate had added one, and audit again:

```bash
printf '#!/bin/bash\nexit 0\n' > .claude/hooks/planted.sh
bash .claude/scripts/harness-audit/scan.sh . --out .claude/harness-registry/reports/scan.json
python3 .claude/scripts/harness-audit/diff-registry.py .claude/harness-registry/reports/scan.json .claude/harness-registry
```

Expected output (exit code 1; one line per difference, here exactly one):

<!-- output: audit-planted -->
```
missing-from-registry: hook-planted (planted.sh) exists in reality, no registry entry
ignored: 1 (skill-EXAMPLE-SKILL)
```

Remove it again with `rm .claude/hooks/planted.sh`. The other lines you may see:
`missing-from-reality: <id> ...` (a managed file was deleted), `drift: <id> field wired_at ...`
(a hook was wired or unwired), `pattern-not-in-registry: ...` (a permission pattern was added),
`settings-unreadable: settings.json ...` (the settings file is not valid JSON, so nothing in it could
be read). Exit 2 means the scan or a registry file is malformed. If the change was intended,
re-baseline with `registry.py init . --force`; it rewrites the computed fields from reality and
keeps what you curated (a domain you set, an origin, a gaps note). Without `--force`, `init`
never overwrites an existing registry (exit 2). `init` also refuses (exit 2, nothing read or written) when
the default registry folder, or the `.claude` folder above it, is a symlink that leads outside the target;
pass `--registry DIR` if that location is intended.

Check that the board shows every installed module:

```bash
python3 .claude/devkit-audit.py board . | grep -c '^=== '
python3 .claude/devkit-audit.py board . | grep -cE '^(purpose|surfaces|principles|procedure|state):'
```

Expected output for the full install (11 modules, five faces each):

<!-- output: board-counts -->
```
11
55
```

The audit does not scan the audit script itself or the tooling an add-on module installs under the
`.claude` folder outside the hooks, rules, skills, agents and scripts folders; the drift check covers those.
Asking Claude Code to run the `harness-audit` skill does the same steps and writes a draft report.

## Create your own module

A module is a directory under `devkit/modules/`. There is **no registry to edit**: the
installer scans `devkit/modules/*/MODULE.md` on every run, so a new directory is
installed by name as soon as it exists.

This walk-through builds `changelog-guard`, a module that installs one rule file into
a target.

**1. Make the directory and put the files you want shipped under its `payload` folder.**

```bash
cd ostler
mkdir -p devkit/modules/changelog-guard/payload/rules
cat > devkit/modules/changelog-guard/payload/rules/changelog.md <<'EOF'
# Changelog

Every user-visible change adds one line to `CHANGELOG.md` under "Unreleased", in the same commit.
EOF
```

**2. Write `MODULE.md`.** It is a YAML frontmatter block between two `---` lines, followed by
the three fixed sections.

```bash
cat > devkit/modules/changelog-guard/MODULE.md <<'EOF'
---
name: changelog-guard
version: "0.1.0"
domain: work-craft
depends-on: []
payload:
  - { src: "rules/changelog.md", dest: ".claude/rules/changelog.md", mode: overwrite, mechanism: INSTRUCT }
---
# changelog-guard

## Prevents

Releases whose user-visible changes nobody wrote down.

## Procedure

Install it and read the rule once. Nothing to wire.

## State

One rule file under the target's rules folder.
EOF
```

Every frontmatter field, and what the installer does when it is missing or wrong:

| Field | Meaning | If missing or wrong |
|---|---|---|
| `name` | The module's name, used on the command line (`install <target> changelog-guard`) and in the manifest. Keep it equal to the directory name. | **Mandatory.** Python crashes with `KeyError: 'name'`, and because every run scans every module, this breaks installs of all modules until you fix it. |
| `domain` | The one domain the module protects; one of the values in `devkit/domains.json`. | Exit 8 before anything is written: `missing domain:` or `unknown domain`, naming the accepted values. |
| `payload` | List of files to ship (entry syntax below). | Installs without error but ships nothing (the module is just recorded in the manifest). Treat as mandatory. |
| `version` | Your module's version string (quote it). Shown by `outdated`, stored in the manifest. | Optional; recorded as `0.0.0`. |
| `depends-on` | List of module names to install first. An unknown or unselected dependency prints a `WARN` but does not stop the install. | Optional; treated as `[]`. |
| `layer` | Removed in kit 2.0 (use `domain` and a per-file `mechanism`). | Exit 8: `layer: was removed in contract v2` ("contract v2" is this module file format: `domain`, a per-file `mechanism`, and the three body sections below). |

The body needs the three sections `## Prevents`, `## Procedure` and `## State`, each non-empty (exit 8
otherwise); the board prints them.

Each `payload` entry is a one-line mapping with these keys:

| Key | Meaning | If missing or wrong |
|---|---|---|
| `src` | Path of the file to copy, **relative to the module's own payload folder** (for the example, the folder devkit/modules/changelog-guard/payload). It must be an existing regular file, not a symlink, and may not escape that folder. | Exit 8, `ERROR: ... payload src None: 'src'` or `payload source missing: <path>`; nothing is written. |
| `dest` | Where the file lands, **relative to the target repo root**. It may not be absolute, contain `..`, or pass through a symlinked directory. Two entries may not resolve to the same file (including names that differ only by case). | Exit 8, `ERROR: ... invalid relative path` or `already claimed by <module>`; nothing is written. |
| `mode` | `overwrite` (default): the file is copied whole. `merge`: allowed only when `dest` ends in `.json`; it merges a top-level `permissions` object (union of `deny` and `ask`, your existing `allow` kept) into the existing JSON file. Several modules may merge into the same `settings.json`. | Defaults to `overwrite`. Any other value (or `merge` on a non-`.json` dest) gives exit 8, `unsupported mode`. |
| `mechanism` | One of `BLOCK`, `ASK`, `DETECT`, `INSTRUCT`, `ROUTE`, `VERIFY` (table in "Modules and domains"). The file's default behavior. | Exit 8: `missing mechanism:` or `unknown mechanism`. |

**3. Commit the module in your clone.** The installer refuses to install from a
checkout that has uncommitted changes under the modules it is installing, and untracked
files count. In installer messages, "factory checkout" means the clone of this repo you run the installer from. Skipping this step gives exit 7:

```
[devkit] REFUSE: the factory checkout has uncommitted changes under the selected modules (or is not a git checkout). Commit them, or re-run with --allow-dirty (the manifest will record source_dirty=true).
```

```bash
git add devkit/modules/changelog-guard
git commit -m "add changelog-guard module"
```

**4. Install it.**

```bash
devkit/install/devkit-install.sh install /path/to/your-repo changelog-guard
```

Expected output (exit code 0):

<!-- output: install-one -->
```
[devkit] install order: ['changelog-guard']
  write  new             .claude/rules/changelog.md
[devkit] wrote <target>/.devkit-manifest.json (source_commit=<hash>, source_dirty=false)
```

Installing a single module leaves everything else in the target alone. Run the check again;
your module's file appears as a managed file and the clean count goes up by one:

```
  OK clean                    changelog-guard    .claude/rules/changelog.md
```

(Its enforcement-gap line, `-- rule changelog.md: no hook declares it (may be instruction-only)`, only says
no hook enforces your rule. For a pure instruction rule that is expected.)

To take a module out of a target again: `devkit/install/devkit-install.sh remove-module /path/to/your-repo changelog-guard`.
For changing a module later: edit it, bump its `version`, commit, and re-run the same `install` command;
managed files you have not touched are updated in place.

## Upgrading from 1.x

Only relevant if you installed a 1.x release of this kit; new installs can skip this section.

Kit 2.0 regroups the 1.x modules by domain. `settings-baseline` became permission fragments inside
`system-safety` and `secrets-privacy`; `hooks-core` and `rules-core` were split across `git-multi-session`,
`secrets-privacy`, `work-craft` and `observe`. `audit`, `claude-md-kernel` and `skill-template` keep their
names. Most payload files have the same bytes; only the module that owns them changed. Four files that a 1.x
install also had changed in 2.0, and `update` overwrites them: `CLAUDE.md` (two bullets became optional
placeholders), `git-hygiene.md` (in `.claude/rules`), `SKILL.md` (the example skill, in
`.claude/skills/EXAMPLE-SKILL`) and `devkit-audit.py` (in `.claude`; it gained the `board` command). `update`
also adds two rules a 1.x target never had, `output-quality-standards.md` and `communication-style.md` (both in
`.claude/rules`), from `work-craft`.

- **`remove-module` keeps old permissions.** After migrating from 1.x, `remove-module` never deletes a permission pattern that was already in settings.json before the migration; it prints a `kept` line naming those patterns, and you delete any you do not want by hand.
- **Old names still work for one minor version.** `install`, `update`, `outdated` and `remove-module` accept a
  1.x name, print one `DEPRECATED` line naming the 2.0 modules (and `2.1.0`, the release that removes the
  alias), and act on those modules:

<!-- output: alias-install -->
```
[devkit] DEPRECATED: module 'rules-core' was split in 2.0 into git-multi-session, secrets-privacy, work-craft; the old name stops working in 2.1.0
[devkit] install order: ['git-multi-session', 'secrets-privacy', 'work-craft']
  write  new             .claude/rules/git-hygiene.md
  write  new             .claude/hooks/stash-leak-check.sh
  write  new             .claude/hooks/branch-similarity-check.sh
  write  new             .claude/hooks/personal-path-guard.py
  write  new             .claude/rules/privacy.md
  write  new             .claude/rules/plan-writing.md
  write  new             .claude/rules/self-contained-agents.md
  write  new             .claude/rules/output-quality-standards.md
  write  new             .claude/rules/communication-style.md
  merge  permissions     .claude/settings.json
[devkit] wrote <target>/.devkit-manifest.json (source_commit=<hash>, source_dirty=false)
```

- **Migrate with `update`.** Pull the new kit into your clone, then run
  `devkit/install/devkit-install.sh update /path/to/your-repo`. It rewrites the target's 1.x module records to
  the 2.0 records in the same transaction as the file writes; if anything fails, run `recover` and the 1.x
  manifest comes back byte for byte. A target that never runs `update` keeps working: `--check` passes, and
  `outdated` tells you to run `update`. `update` on a target with nothing installed installs nothing and says so.
- **If you edited one of those four files, `update` refuses.** A `CLAUDE.md` you filled in is the usual case.
  You see this, the exit code is 3, and nothing is written (the target is exactly as it was):

  ```
    REFUSE modified-downstream CLAUDE.md (use --force)
  [devkit] 1 file(s) refused (modified/unmanaged). Nothing was written. Re-run with --force to overwrite.
  ```

  Two ways through. **Keep your file:** add a line containing `devkit:` immediately followed by `forked` to it
  (for example the comment `<!-- devkit:forked -->` in `CLAUDE.md`) and run `update` again; the other files
  update and the output shows `skip   forked          CLAUDE.md`. **Take the kit's version:** first copy
  your file somewhere safe, then run `update` with `--force`, which overwrites every file you modified (not
  only that one), and merge your text back by hand.
- **Partial migration is safe, but it is not free.** Installing the old name `rules-core` (as above) also
  installs the `secrets-privacy` permission fragment (five deny patterns for `.env` and secrets paths) and
  three hook scripts that a 1.x `rules-core`-only target never had. The hooks are only copied, never wired.
- Each manifest records the kit version (`kit_version`). `devkit/README.md`, "Upgrading from 1.x", has the
  full rules.

## When things go wrong

Every exit code is listed in `devkit/README.md` (end of the "Transactions and
locking" section). The ones you will meet first:

| You see | Exit | Meaning and fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'yaml'` | 1 | PyYAML is missing. `python3 -m pip install pyyaml`. |
| `REFUSE: the factory checkout has uncommitted changes under the selected modules` | 7 | You edited or added files under the modules being installed. Fix 1: `git add` and `git commit` them in your clone, then re-run. Fix 2: re-run with `--allow-dirty` (for example `devkit/install/devkit-install.sh install <target> changelog-guard --allow-dirty`); the install proceeds and the manifest records `source_dirty=true` so the target shows it was built from uncommitted source. Nothing is written when refused. |
| `REFUSE unmanaged-exists CLAUDE.md (use --force to clobber)` | 3 | The target already has a file at that path that devkit never installed. Nothing was written. Either leave that module out (name the others on the command line), or, if you really want it replaced, re-run with `--force`. |
| `REFUSE modified-downstream <path> (use --force)` | 3 | You edited a file devkit manages. Nothing was written. Revert your edit, or mark the file yours (the fork marker above), or `--force` to overwrite your edit with the module's version. |
| `ERROR: target <path> is inside the devkit factory checkout` | 8 | Pick a target outside this clone. |
| `ERROR: <module>: ...` or `module <name>: ...` (bad `src`, `dest`, `mode`, `domain` or `mechanism`) | 8 | Fix the `MODULE.md` entry as described in the tables above. |
| `another install is running (lock held ...)` | 4 | Another install on the same target is in progress. Wait and retry. |
| `interrupted transaction(s)` | 6 | An earlier install crashed. Run `devkit/install/devkit-install.sh recover <target>`; it finishes or rolls back and prints what it did. |
| `--check` prints `!! modified-downstream` | 2 | A managed file differs from what was installed. See the "Check" section. |
| `registry-vs-reality` lines and exit 1 | 1 | The audit found differences. See "Self-audit". |
| `scan.sh: jq is required` | 2 | Install `jq`, then run the scan again. |

A refusal never half-installs: every install is planned read-only first and applied as
one transaction, so after exit 3, 7 or 8 your target is exactly as it was.

## What lands in your target

| Path in the target | What it is | Commit it? |
|---|---|---|
| Everything under the target's `.claude` folder (rules, hooks, skills, settings, the audit and self-audit scripts) and `CLAUDE.md` | The module payloads themselves. | **Yes.** They are the point of the install; your agent reads them from the repo. |
| The registry folder `harness-registry` under `.claude` (after `registry.py init`) | The baseline your self-audit compares against: one JSON file per domain. Its `reports` subfolder holds scan output. | **Yes** for the registry files (they are the baseline); **no** for `reports`. |
| `.devkit-manifest.json` | The ledger: for each installed file its path and SHA-256, plus the module versions and the `source_commit` of this clone. `--check` and re-installs compare against it. | **Yes.** Without it a teammate's clone cannot run `--check` or tell managed files from your own. |
| `.devkit-install.lock` | An empty file the installer uses to stop two installs running at once. It stays after the first install and holds no data. | **No.** Add it to `.gitignore`. |
| `.devkit-txn` (a folder) | Exists only while an install is in flight or after a crash (see `recover`). | **No.** Normally absent. |

The installed audit script (devkit-audit.py in the target's `.claude` folder) is stdlib-only Python, so a target can run its
own drift check with this clone deleted. Run it in your target repo:

```bash
cd /path/to/your-repo
python3 .claude/devkit-audit.py .
```

The audit's enforcement-gap scan looks for the hooks and rules folders inside the target's `.claude` folder first. If your target directory is itself a Claude Code config directory (for example a user-level config folder), it falls back to hooks and rules folders directly under the target.

## What you see on re-run

Installing the same thing again is safe and writes nothing:

<!-- output: rerun -->
```
[devkit] install order: ['audit', 'claude-md-kernel', 'decision-gating-business', 'decision-gating-technical', 'git-multi-session', 'harness-governance', 'observe', 'secrets-privacy', 'skill-template', 'system-safety', 'work-craft']
[devkit] up to date; nothing to do.
```

Exit code 0, no file touched, no manifest rewrite. (If you have edited `settings.json` since, for
example by wiring hooks, the re-run touches no file either, but it re-records that shared file in the
manifest and ends with the `wrote` line instead.) If you update this clone (`git pull`)
and a module's payload changed, `devkit/install/devkit-install.sh outdated <target>`
prints one line per stale module (no output means current), and re-running `install`
updates the files you have not edited and refuses (exit 3) the ones you have.

## Issues, not pull requests

Changes are made by the maintainer, so pull requests are not merged. Ostler takes **issues**,
not pull requests: describe the problem (what you ran, what you saw, what you expected), and a
maintainer makes the change.

## Tests

The suite shipped with the kit is `devkit/tests/test-devkit-feedback.sh`. Run it from
your clone (it builds throwaway targets in a temp directory and cleans up after itself):

```bash
bash devkit/tests/test-devkit-feedback.sh
# expected last line:  35/35 passed
```

The transaction library under the installer has its own suite, `devkit/tests/test-txn.sh`
(needs only `bash` and `python3`; the last line is `<N>/<N> passed, 0 skipped`). The other suites in the
same folder (`test-contract-v2.sh`, `test-migration-v2.sh`, `test-decision-gating.sh`,
`test-harness-governance.sh`, `test-work-craft.sh`) end with `<P>/<T> passed` or
`<P>/<T> passed, <S> skipped`: `<P>` tests passed out of `<T>`, and `<S>` skipped. A skip is a test that
needs an optional prerequisite that is absent (for example the 1.x kit in git history, or an optional input file); skips are expected, so in this
clone `test-contract-v2.sh`, `test-decision-gating.sh`, `test-harness-governance.sh`,
`test-migration-v2.sh` and `test-work-craft.sh` report some skips; `test-txn.sh` and `test-devkit-feedback.sh` report none. Every run
exits 0 with `<P>` plus `<S>` equal to `<T>` and no `FAILED` line. Anything else is a failure.
`devkit/tests/test-personal-path-guard.sh` checks the `personal-path-guard.py` hook from
`secrets-privacy`:

```bash
bash devkit/tests/test-personal-path-guard.sh
# expected last line:  6/6 passed
```

For the module layout, the full command list (`patch`, `outdated`, `remove-module`,
`recover`, `update`) and the exit-code table, read `devkit/README.md`.
