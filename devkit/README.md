# devkit — installer and module library

The installer and module library that copies guardrails into target repos. In this document "factory" means your clone of this repo and "downstream" or "target" means a repo you install into. Distributes capability by **copy-on-build + version
stamp**: a target works fully with this repo deleted (never symlink, never runtime-
reference).

## Layout

```
<repo root>/
├── devkit/
│   ├── modules/<name>/
│   │   ├── MODULE.md     frontmatter (name, version, layer, depends-on, payload)
│   │   └── payload/      files copied into a target
│   ├── codex-modules/<name>/  Codex module catalogue (same metadata contract)
│   ├── install/
│   │   ├── devkit-install.sh  Claude entry point
│   │   ├── codex-install.sh   Codex entry point
│   │   └── _devkit.py          its implementation
│   └── tests/
│       ├── test-devkit-feedback.sh        the shipped test suite (see Tests)
│       ├── test-personal-path-guard.sh    path-guard test (see Tests)
│       ├── test-txn.sh                    transaction-library test runner (see Tests)
│       └── test_txn.py                    transaction-library unit tests
└── install/lib/
    └── txn.py            the transaction library _devkit.py imports 
```

The step-by-step guide to writing your own `modules/<name>/` is in the root `README.md`.

## Modules — one per control layer (blocks, detects, instructs, points, verifies)

| Module | Layer | Installs |
|---|---|---|
| settings-baseline | BLOCKS | `settings.json` permission template (deny/ask), merged |
| hooks-core | DETECT | stash-leak, branch-similarity, personal-path-guard, plan-exit hooks |
| rules-core | INSTRUCT | git-hygiene, plan-writing, self-contained-agents, privacy rules |
| claude-md-kernel | POINTS | CLAUDE.md pointer-kernel template |
| skill-template | TEMPLATE | SKILL.md frontmatter contract + skeleton |
| audit | VERIFIES | `devkit-audit.py` — drift and gap checker (stdlib-only; runs without PyYAML) |

Codex modules live in `devkit/codex-modules/`: `codex-instructions` installs
`AGENTS.md` and detailed rules; `codex-command-rules` installs `.codex/rules/ostler.rules`;
`codex-hooks-core` installs `.codex/hooks.json` and branch, stash, and personal-path
notices; `codex-skill-template` installs a starter `SKILL.md`; and `codex-audit`
installs `.codex/devkit-audit.py`. The starter skill stays outside Codex's skill
discovery path until copied into `.agents/skills/` and customized.

## Use

```bash
# Install all modules into a target repo:
devkit/install/devkit-install.sh install /path/to/target-repo

# Install specific modules (deps auto-ordered):
devkit/install/devkit-install.sh install /path/to/target hooks-core rules-core

# Check drift (runs the FACTORY's own copy of devkit-audit.py against the target —
# not the copy installed at <target>/.claude/devkit-audit.py; see Self-containment):
devkit/install/devkit-install.sh --check /path/to/target

# Record a deliberate downstream edit you may later fold back into your factory clone or contribute upstream (patch ledger, see below):
devkit/install/devkit-install.sh patch /path/to/target <managed-path> --reason "<why>"

# Codex: use the same commands and argument order:
devkit/install/codex-install.sh install /path/to/target-repo
devkit/install/codex-install.sh check /path/to/target-repo
devkit/install/codex-install.sh outdated /path/to/target-repo
devkit/install/codex-install.sh remove-module /path/to/target-repo codex-hooks-core
devkit/install/codex-install.sh recover /path/to/target-repo
```

Install stamps `<target>/.devkit-manifest.json` with per-file SHA-256 and the factory's
`source_commit`. The installer **refuses to clobber** modified-downstream or pre-existing
unmanaged files without `--force`. A refused file keeps its prior manifest
record, so a `pending-upstream` file survives a
re-install as an exact managed-path match. A file carrying a `devkit:forked` marker is
left alone (the target has taken ownership).

The two catalogues share the target manifest, lock, and journal. Installing or
removing a Codex module preserves Claude module records, and vice versa. `outdated`
reports only modules from the selected catalogue; `check` audits the entire manifest.
Codex hooks run only after project config trust and `/hooks` review. Their commands
resolve scripts from the Git repository root; installing `codex-hooks-core` requires
the target to be that root. If Git or a hook script later becomes unavailable, the
hook command exits without blocking Codex.
Codex command rules govern commands outside the sandbox and are not a substitute for
Claude Code's permissions. They match exact command prefixes, so alternate flags,
paths, and shell wrappers may need separate review. The Codex personal-path hook inspects file edit inputs
through `apply_patch` (including the `Edit` and `Write` matcher aliases); it does not
cover shell redirection or arbitrary programs.
The manifest-wide `source_commit` and `source_dirty` fields describe the latest
installer run; per-module fingerprints track each installed module's version.

## Tests

`devkit/tests/test-devkit-feedback.sh` is the shipped test suite (35 checks of the audit's
ledger handling and the `patch` command). Run it from a clone with a clean git checkout:

```bash
bash devkit/tests/test-devkit-feedback.sh
# expected last line:  35/35 passed
```

`devkit/tests/test-txn.sh` runs `devkit/tests/test_txn.py`, the unit tests of the
transaction library (`install/lib/txn.py`): crash recovery at every fault point, compare-and-swap,
locking and the fingerprint. It needs only
`bash` and `python3`:

```bash
bash devkit/tests/test-txn.sh
# expected last line:  <N>/<N> passed
```

`devkit/tests/test-personal-path-guard.sh` checks that the path guard exempts only the user-level
config directory in your home folder and flags a repo-level `.claude` directory:

```bash
bash devkit/tests/test-personal-path-guard.sh
# expected last line:  6/6 passed
```

The Codex suite covers installation, mixed manifests, refusal, audit, hooks, and
command rule syntax. It needs PyYAML and a `codex` executable for the rule check:

```bash
python3 -m unittest devkit.tests.test_codex_installer -v
```

The feedback suite builds throwaway targets in a temp directory and removes them on exit; it needs
`bash`, `python3` and `git`, and touches nothing outside that directory.

## Patch ledger — the minimal upstream feedback loop

`check` classifies each manifest-managed file into one of four states:
`clean` / `modified-downstream` (fails) / `pending-upstream` (a deliberate downstream
fix you intend to copy back into your devkit clone — exits 0, or exits 2 as `STALE` if the file was edited
again since) / `forked`. A downstream edit becomes `pending-upstream` by running
`patch` (above), which stamps a `patched_pending_upstream` entry — `{path, sha256,
reason, date}` — pinned to the exact patched content, not the path forever: any further
edit re-fails until the ledger entry is refreshed (the STALE failure message prints its
own pre-filled refresh command). For every `pending-upstream` file, `check` also prints
a copy-paste command that appends the fix to a JSONL log on the machine running the check
(the audit prints its path); it is never auto-run. The log is a local JSONL record of the fixes you recorded with `patch`: review it with any JSON tool, or attach it to an issue or pull request when contributing a fix back to the kit. It is one file, `feedback.jsonl`, in a `devkit-feedback` folder under the devkit home (`$DEVKIT_HOME`, default `$HOME/.devkit`), or in `$DEVKIT_FEEDBACK_DIR` when you set that to an absolute path. Each line is `{"ts", "reason", "path"}`. A `mode: merge` file (only
`settings-baseline`'s `settings.json` today) cannot carry a pending-upstream entry, because a merged
file's content is not a copy of the payload, so its sha256 cannot be pinned to one patched version (see the docstring of `validate_ledger` in `devkit/modules/audit/payload/devkit-audit.py`).

## Self-containment

Module payloads are generic templates.
The **installer** (`devkit-install.sh` / `_devkit.py`) needs PyYAML (`python3 -m pip install pyyaml`) to read
`MODULE.md`; without it it exits 1 with `ModuleNotFoundError: No module named 'yaml'`. Only the `audit` payload
is stdlib-only Python, so a target can self-check with this factory
deleted.

## Transactions and locking

`devkit-install.sh` runs on a crash-safe transaction library
(`install/lib/txn.py`). Every mutating
command plans everything read-only first, then (only if there is something to commit)
takes an exclusive lock, settles any interrupted transaction, re-plans, and applies one
journaled transaction whose manifest rename is the commit point. A refusal therefore
writes nothing at all: no files, no manifest change, no new target directory, and no lock
file. Each planned write or delete records the file state the plan saw, so if someone edits
a file between planning and applying, the install stops with exit 3 and leaves their edit
alone. `check` and `outdated` take the lock in shared mode (they wait for nothing, they
just refuse with exit 4 while an install is running), so they never report a half-applied
tree. A reader on a target that has no lock file yet does not create one; if an install
starts while the reader is running, the reader exits 4 when it finishes and prints nothing
from that read (retry it). The refusal rounds of `install`, `remove-module` and `patch` and
the `recover` "nothing to do" check behave the same way. devkit assumes it manages every path a module payload names; do not point other tools at those paths.

| Command | What it does |
|---|---|
| `outdated <target>` | one line per installed module whose factory payload changed: `devkit: <module> outdated (installed <fp>, source <fp>) -> devkit-install.sh install <target> <module>`. No output = current. Read only, exits 0 |
| `remove-module <target> <name> [--force]` | removes a module's clean files and its manifest record. Modified files are refused (exit 3) unless `--force`. Deselecting a module on `install` never prunes; use this |
| `install ... --allow-dirty` | install from a factory checkout with uncommitted changes under the selected modules (or a non-git factory). Without it: exit 7. The manifest records `source_dirty=true` |
| `recover <target>` | finish or roll back a crashed install; prints `no interrupted transaction` when there is nothing to do |

Exit codes: `0` ok · `1` usage/internal · `3` refusals, nothing written (modified-downstream
or unmanaged files; re-run with `--force`) · `4` locked by another run · `6` interrupted
transaction pending (`check` / `outdated`; run `recover`) · `7` dirty factory checkout
(commit, or `--allow-dirty`) · `8` validation / confinement error (path escapes the target,
symlinked directory, two destinations that are the same file modulo case or Unicode
normalization, or a target that is the factory checkout itself, inside it, or contains it).
