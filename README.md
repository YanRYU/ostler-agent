# Ostler

Ostler is a governance kit for coding agents working on real repositories. It copies a
layered set of guardrails (permission rules, hooks that surface risky repo state,
behavioral rules, a drift checker) into a repo's Claude Code or Codex config, and
records a SHA-256 manifest so you can later prove nothing has drifted.

Everything is copied, never linked: a target repo works with this clone deleted. The
unit you install, and the unit you write yourself, is a **module** (a directory with a
`MODULE.md` and the files it ships). The Claude installer is
`devkit/install/devkit-install.sh`; the Codex installer is `devkit/install/codex-install.sh`.

## Prerequisites

- `bash`, `git`, and `python3` (tested on 3.13), on macOS or Linux. The installer uses
  `fcntl` file locking, so native Windows is not supported (use WSL).
- **`jq`**, only if you wire the `hooks-core` hooks into your `settings.json` (install it
  with your package manager, for example `brew install jq` or `apt install jq`). Without
  it the `stash-leak-check.sh` and `branch-similarity-check.sh` hooks silently do nothing
  (they exit 0 and print nothing), and `plan-exit-print-location.sh` prints
  `Plan file: (unable to determine — no transcript)`. The installer and the audit do not need it.
- **PyYAML**, which is not part of a stock `python3`. Without it the installer stops
  immediately (exit 1) with a Python traceback whose last lines are:

  ```
      import yaml  # PyYAML — factory-side only
  ModuleNotFoundError: No module named 'yaml'
  ```

  Fix: `python3 -m pip install pyyaml` (or `pip install --user pyyaml`, or your OS
  package, e.g. `apt install python3-yaml`). Only the installer needs it; the audit
  script it installs into your target is stdlib-only.
- A target directory that is **outside** this clone. Installing into the clone itself,
  a folder inside it, or a folder that contains it is refused (exit 8).

## Quick start

Install every module into a target repo. The target may be an existing repo or a new
empty directory (it is created if missing).

```bash
git clone <this-repo-url> ostler
cd ostler
devkit/install/devkit-install.sh install /path/to/your-repo
```

Expected output (the last line carries your clone's commit hash; the target path is
whatever you passed):

```
[devkit] install order: ['audit', 'claude-md-kernel', 'rules-core', 'hooks-core', 'settings-baseline', 'skill-template']
  write  new             .claude/devkit-audit.py
  write  new             CLAUDE.md
  write  new             .claude/rules/git-hygiene.md
  write  new             .claude/rules/plan-writing.md
  write  new             .claude/rules/self-contained-agents.md
  write  new             .claude/rules/privacy.md
  write  new             .claude/hooks/stash-leak-check.sh
  write  new             .claude/hooks/branch-similarity-check.sh
  write  new             .claude/hooks/personal-path-guard.py
  write  new             .claude/hooks/plan-exit-print-location.sh
  merge  permissions     .claude/settings.json
  write  new             .claude/skills/EXAMPLE-SKILL/SKILL.md
[devkit] wrote <target>/.devkit-manifest.json (source_commit=<hash>, source_dirty=false)
```

Exit code 0. To install only some modules, name them (dependencies are ordered for you,
and a missing dependency prints a `WARN`):

```bash
devkit/install/devkit-install.sh install /path/to/your-repo rules-core hooks-core
```

The modules shipped here are listed in the table in `devkit/README.md`. One thing to
know before you install all of them into a repo you already use: `claude-md-kernel`
writes a template `CLAUDE.md`, and if your repo already has one the install is refused
(see "When things go wrong"). Leave that module out if you keep your own.

The hooks are copied but not wired up: add them to your settings hooks block yourself.
`settings-baseline` merges deny/ask permission lists into your existing settings file
and never touches your own `allow` entries.

## Codex quick start

Run this from the same Ostler clone, targeting the root of a Git repository. The
hooks module refuses a plain directory or a subdirectory of a repository:

```bash
devkit/install/codex-install.sh install /path/to/your-repo
devkit/install/codex-install.sh check /path/to/your-repo
```

This installs `AGENTS.md`, detailed guidance under `.agents/ostler/rules/`, command
rules at `.codex/rules/ostler.rules`, `.codex/hooks.json` and its Python hooks, a
starter skill under `.agents/ostler/templates/EXAMPLE-SKILL/`, and a standalone audit at
`.codex/devkit-audit.py`. An existing unmanaged `AGENTS.md` is preserved: installation
refuses until you choose specific modules or explicitly use `--force`.
Copy and customize the starter skill into `.agents/skills/` when you want Codex to discover it.

After installing, trust the repository's `.codex` config layer and review the hooks
with `/hooks` in Codex. Copied hooks have not been verified active. The command rules
only govern commands outside Codex's sandbox and match command prefixes rather than
every shell spelling; they do not reproduce Claude Code's file access deny list.
The hooks inspect supported tool events, so they also cannot
cover every way a file might be written. See the [Codex hook documentation](https://learn.chatgpt.com/docs/hooks)
and [command rules documentation](https://learn.chatgpt.com/docs/agent-configuration/rules).

The two installers use separate module catalogues and one manifest and transaction
lock. You can install both in a target repository; `check` audits all managed files.
Use `codex-install.sh outdated`, `patch`, `remove-module`, or `recover` with the same
argument order as the Claude installer.

## Check

```bash
devkit/install/devkit-install.sh --check /path/to/your-repo
```

Expected output on a clean target (exit code 0):

```
[audit] drift check: <target>
  OK clean                    audit              .claude/devkit-audit.py
  OK clean                    claude-md-kernel   CLAUDE.md
  OK clean                    rules-core         .claude/rules/git-hygiene.md
  OK clean                    rules-core         .claude/rules/plan-writing.md
  OK clean                    rules-core         .claude/rules/self-contained-agents.md
  OK clean                    rules-core         .claude/rules/privacy.md
  OK clean                    hooks-core         .claude/hooks/stash-leak-check.sh
  OK clean                    hooks-core         .claude/hooks/branch-similarity-check.sh
  OK clean                    hooks-core         .claude/hooks/personal-path-guard.py
  OK clean                    hooks-core         .claude/hooks/plan-exit-print-location.sh
  OK clean                    settings-baseline  .claude/settings.json
  OK clean                    skill-template     .claude/skills/EXAMPLE-SKILL/SKILL.md
[audit] summary: clean=12
[audit] enforcement-gap scan: <target>/.claude/hooks
  -- rule plan-writing.md: no hook declares it (may be instruction-only)
  -- rule self-contained-agents.md: no hook declares it (may be instruction-only)
```

The `--` lines are informational (a rule with no hook enforcing it); they do not affect
the exit code. What does: a managed file that you edited shows
`!! modified-downstream` and the check exits 2.

After you edit a template such as `CLAUDE.md` (you are meant to fill its `{placeholders}`),
`--check` will flag it. Two ways to say "this file is mine now": put the text
`devkit:` immediately followed by `forked` in the file (for example in a comment) and
the check reports `OK forked`, and re-installs skip it; or record it as a deliberate local
change with `devkit-install.sh patch` (the check then reports it as `pending-upstream` instead
of failing; nothing is sent anywhere, so to contribute the fix back, open an issue or pull
request on the repository you cloned from; see `devkit/README.md`, "Patch ledger").

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

**2. Write `MODULE.md`.** It is a YAML frontmatter block between two `---` lines,
followed by free-form Markdown documentation.

```bash
cat > devkit/modules/changelog-guard/MODULE.md <<'EOF'
---
name: changelog-guard
version: "0.1.0"
layer: INSTRUCT
depends-on: []
payload:
  - { src: "rules/changelog.md", dest: ".claude/rules/changelog.md", mode: overwrite }
---
# changelog-guard

Always-on rule: every user-visible change gets a CHANGELOG.md line.
EOF
```

Every frontmatter field, and what the installer does when it is missing :

| Field | Meaning | If missing |
|---|---|---|
| `name` | The module's name, used on the command line (`install <target> changelog-guard`) and in the manifest. Keep it equal to the directory name. | **Mandatory.** Python crashes with `KeyError: 'name'`, and because every run scans every module, this breaks installs of all modules until you fix it. |
| `payload` | List of files to ship (entry syntax below). | Installs without error but ships nothing (the module is just recorded in the manifest). Treat as mandatory. |
| `version` | Your module's version string (quote it). Shown by `outdated`, stored in the manifest. | Optional; recorded as `0.0.0`. |
| `layer` | Which kind of control the module is. Free text, not validated. The shipped modules use `BLOCKS` (settings permissions that stop a command), `DETECT` (hooks that warn), `INSTRUCT` (rules the agent reads), `POINTS` (CLAUDE.md pointers), `TEMPLATE` (files you copy and fill) and `VERIFIES` (the drift checker); see rule 3 in `devkit/modules/rules-core/payload/rules/self-contained-agents.md`. Use one of those so your modules read like the rest. | Optional; not read by the installer. |
| `depends-on` | List of module names to install first. An unknown or unselected dependency prints a `WARN` but does not stop the install. | Optional; treated as `[]`. |

Each `payload` entry is a one-line mapping with these keys:

| Key | Meaning | If missing or wrong |
|---|---|---|
| `src` | Path of the file to copy, **relative to the module's own payload folder** (for the example, the folder devkit/modules/changelog-guard/payload). It must be an existing regular file, not a symlink, and may not escape that folder. | Exit 8, `ERROR: ... payload src None: 'src'` or `payload source missing: <path>`; nothing is written. |
| `dest` | Where the file lands, **relative to the target repo root**. It may not be absolute, contain `..`, or pass through a symlinked directory. Two entries may not resolve to the same file (including names that differ only by case). | Exit 8, `ERROR: ... invalid relative path` or `already claimed by <module>`; nothing is written. |
| `mode` | `overwrite` (default): the file is copied whole. `merge`: allowed only when `dest` ends in `.json`; it merges a top-level `permissions` object (union of `deny` and `ask`, your existing `allow` kept) into the existing JSON file. | Defaults to `overwrite`. Any other value (or `merge` on a non-`.json` dest) gives exit 8, `unsupported mode`. |

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

```
[devkit] install order: ['changelog-guard']
  write  new             .claude/rules/changelog.md
[devkit] wrote <target>/.devkit-manifest.json (source_commit=<hash>, source_dirty=false)
```

Installing a single module leaves everything else in the target alone. Then run the
check again; your module's file appears as a managed file and the count goes up by one:

```
  OK clean                    changelog-guard    .claude/rules/changelog.md
[audit] summary: clean=13
  -- rule changelog.md: no hook declares it (may be instruction-only)
```

(That last `--` line only says no hook enforces your rule. For a pure instruction rule
that is expected.)

To take a module out of a target again: `devkit-install.sh remove-module <target> changelog-guard`.
For changing a module later: edit it, commit, and re-run the same `install` command;
managed files you have not touched are updated in place.

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
| `ERROR: <module>: ...` (bad `src`, `dest` or `mode`) | 8 | Fix the `MODULE.md` entry as described in the tables above. |
| `another install is running (lock held ...)` | 4 | Another install on the same target is in progress. Wait and retry. |
| `interrupted transaction(s)` | 6 | An earlier install crashed. Run `devkit/install/devkit-install.sh recover <target>`; it finishes or rolls back and prints what it did. |
| `--check` prints `!! modified-downstream` | 2 | A managed file differs from what was installed. See the "Check" section. |

A refusal never half-installs: every install is planned read-only first and applied as
one transaction, so after exit 3, 7 or 8 your target is exactly as it was.

## What lands in your target

| Path in the target | What it is | Commit it? |
|---|---|---|
| Everything under the target's `.claude` folder (rules, hooks, skills, settings, the audit script) and `CLAUDE.md` | The module payloads themselves. | **Yes.** They are the point of the install; your agent reads them from the repo. |
| `.devkit-manifest.json` | The ledger: for each installed file its path and SHA-256, plus the module versions and the `source_commit` of this clone. `--check` and re-installs compare against it. | **Yes.** Without it a teammate's clone cannot run `--check` or tell managed files from your own. |
| `.devkit-install.lock` | An empty file the installer uses to stop two installs running at once. It stays after the first install and holds no data. | **No.** Add it to `.gitignore`. |
| `.devkit-txn` (a folder) | Exists only while an install is in flight or after a crash (see `recover`). | **No.** Normally absent. |

The installed audit script (devkit-audit.py in the target's `.claude` folder) is stdlib-only Python, so a target can run its
own drift check with this clone deleted:

```bash
python3 .claude/devkit-audit.py .
```

The audit's enforcement-gap scan looks for the hooks and rules folders inside the target's `.claude` folder first. If your target directory is itself a Claude Code config directory (for example a user-level config folder), it falls back to hooks and rules folders directly under the target.

## What you see on re-run

Installing the same thing again is safe and writes nothing:

```
[devkit] install order: ['audit', 'claude-md-kernel', 'rules-core', 'hooks-core', 'settings-baseline', 'skill-template']
[devkit] up to date; nothing to do.
```

Exit code 0, no file touched, no manifest rewrite. If you update this clone (`git pull`)
and a module's payload changed, `devkit/install/devkit-install.sh outdated <target>`
prints one line per stale module (no output means current), and re-running `install`
updates the files you have not edited and refuses (exit 3) the ones you have.

## Tests

The suite shipped with the kit is `devkit/tests/test-devkit-feedback.sh`. Run it from
your clone (it builds throwaway targets in a temp directory and cleans up after itself):

```bash
bash devkit/tests/test-devkit-feedback.sh
# expected last line:  35/35 passed
```

The transaction library under the installer has its own suite, `devkit/tests/test-txn.sh`
(needs only `bash` and `python3`; the last line is `<N>/<N> passed`).

`devkit/tests/test-personal-path-guard.sh` checks the `personal-path-guard.py` hook from
`hooks-core` (it needs only `bash` and `python3`; the last line is `<N>/<N> passed`):

```bash
bash devkit/tests/test-personal-path-guard.sh
```

For the module layout, the full command list (`patch`, `outdated`, `remove-module`,
`recover`) and the exit-code table, read `devkit/README.md`.
