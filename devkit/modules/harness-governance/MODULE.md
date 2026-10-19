---
name: harness-governance
version: "1.0.16"
domain: all
depends-on: []
payload:
  - { src: "scripts/harness-audit/scan.sh",         dest: ".claude/scripts/harness-audit/scan.sh",         mode: overwrite, mechanism: VERIFY }
  - { src: "scripts/harness-audit/diff-registry.py", dest: ".claude/scripts/harness-audit/diff-registry.py", mode: overwrite, mechanism: VERIFY }
  - { src: "scripts/harness-audit/registry.py",      dest: ".claude/scripts/harness-audit/registry.py",      mode: overwrite, mechanism: VERIFY }
  - { src: "harness-registry/_schema.json",          dest: ".claude/harness-registry/_schema.json",          mode: overwrite, mechanism: VERIFY }
  - { src: "skills/harness-audit/SKILL.md",          dest: ".claude/skills/harness-audit/SKILL.md",          mode: overwrite, mechanism: INSTRUCT }
  - { src: "rules/harness-governance.md",            dest: ".claude/rules/harness-governance.md",            mode: overwrite, mechanism: INSTRUCT }
---
# harness-governance

## Prevents

An unwatched harness: hooks that are copied but never wired, rules that quietly disappear, and permission lists that change without anyone deciding to.

A repo audits its own `.claude` folder against a baseline it owns. Three scripts, a skill and a rule:
`scan.sh` reads the real hooks, rules, skills, agents, settings permissions and hook wiring and prints
them as JSON; `registry.py init` seeds the registry from that scan; `diff-registry.py` compares a fresh
scan with the registry and reports every difference. All three work with nothing but `bash`, `jq` and
`python3` (standard library only), inside the target, with the kit's source folder deleted. The
`harness-audit` skill drives them and writes a draft gap report; the rule says when to re-run them.
Not covered: whether a skill is ever used (that needs session transcripts). Also not scanned: the audit
script itself (.claude/devkit-audit.py) and the tooling an add-on module installs under .claude outside
the hooks, rules, skills, agents and scripts folders (for example a folder an add-on module creates for its own tooling); they are checked by the
drift check, not by this audit. A settings file that is not valid JSON is reported as a `settings-unreadable`
finding instead of being read as empty, and `registry.py init` refuses (exit 2, one line, nothing written)
while one is present.

## Procedure

1. Install the module, then create the baseline once, from the target's root:

   ```bash
   python3 .claude/scripts/harness-audit/registry.py init .
   ```

   Expected: `registry init: seeded N entries into M files under .../.claude/harness-registry (K need curation: domain TODO-curate)`.
   Each artifact's `domain` is copied from the module that installed it (the install manifest records
   it); an artifact no module installed gets `domain: TODO-curate` in `_uncurated.json`, for you to
   set. A hook whose scanned mechanism differs from the one its module declares keeps the declared one,
   with a `mechanism_override_reason`. Files of a module whose domain is module-only (the copy-and-rename
   skill skeleton) are never registry entries; they are listed in `_ignored.json` and the audit skips them.
2. Audit any time (or ask Claude to run the `harness-audit` skill):

   ```bash
   mkdir -p .claude/harness-registry/reports
   bash .claude/scripts/harness-audit/scan.sh . --out .claude/harness-registry/reports/scan.json
   python3 .claude/scripts/harness-audit/diff-registry.py .claude/harness-registry/reports/scan.json .claude/harness-registry
   ```

   Expected right after step 1: `registry-vs-reality: CLEAN (0 diffs)` and exit 0. You will see exit 1
   and one line per difference after a change: `missing-from-registry: hook-x ...` (a hook that was
   added), `missing-from-reality: rule-y ...` (a managed file that was deleted), `drift: <id> field
   wired_at ...` (a hook that was wired or unwired), `pattern-not-in-registry: ...` (a permission
   pattern that was added). Exit 2 means the scan or a registry file is malformed. Decide whether the
   change was intended; if it was, re-baseline with `registry.py init . --force`, which rewrites the
   extracted fields from reality and keeps what you curated: a domain you set (a domain `init` took from a module follows that
   module if its domain later changes), an origin, a gaps note or an override reason you wrote. A hand-written
   origin and gaps note stay with the permission patterns they were written for, each in its own `-curated` entry. Text `init` generated itself (the installed-by origin, the "copied but
   not wired" note, a declared-mechanism reason) is recomputed. A domain that is missing or not a plain lowercase name is treated as TODO-curate. Without `--force`, `init` never
   overwrites an existing registry (exit 2). `init` also refuses (exit 2, nothing read or written) when the default registry folder, or the `.claude` folder above it, is a symlink leading outside the target; pass `--registry DIR` if that is intended (a symlink loop in that path is refused the same way). The audit also reports an id that appears twice, prints an
   `ignored:` line for the skipped skeleton files, and reports any `_ignored.json` id that is not a module-only file.
3. The scan lists the hooks you copied but did not wire as `unwired-file` findings. Wiring a hook is
   done in the settings file as each module's own Procedure shows; it then shows as drift until you
   re-baseline, which is intended.
4. If `jq` is missing, `scan.sh` exits 2 with `scan.sh: jq is required`; install `jq` and run again.
   To remove the module, run `remove-module`; the registry folder's own files are yours and stay.

## State

Installed: three scripts in .claude/scripts/harness-audit/, the vocabulary file
.claude/harness-registry/_schema.json (generated by the kit from the same schema the installer
validates `domain:` against, so the two cannot differ), the `harness-audit` skill and one rule.
Written by you or by `registry.py init`: the registry in .claude/harness-registry/, one JSON file per
domain plus `_uncurated.json` and `_ignored.json`, each domain file shaped `{"_meta": {...}, "entries": [...]}`; the scan and draft
reports in .claude/harness-registry/reports/. The registry is JSON (standard library only); curated
fields (`domain`, `origin`, `gaps`, `mechanism_override_reason`) are yours to edit, extracted fields are
computed and never hand-edited. The scanner and differ write nothing but the file you name with `--out`.
