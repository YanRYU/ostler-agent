---
name: harness-audit
description: Computed governance audit of this repo's Claude Code harness (hooks, rules, skills, agents, settings permissions and hook wiring). Runs the extraction scanner, diffs it against the repo's registry, and writes a DRAFT gap report. Never auto-fixes. Triggers: "audit the harness", "harness audit", "governance audit", "harness gap report", "registry drift check".
---

# Harness Audit

A computed audit of this repo's control surfaces against its registry. The registry says, for each
control artifact (a hook, a rule, a skill, an agent, a permission group, the CLAUDE.md pointer
table), which domain it serves and by which mechanism it works. The vocabulary is in
`.claude/harness-registry/_schema.json`. The scanner reads reality; the differ compares reality
with the registry.

Everything this skill runs or reads is under this repo's `.claude/` folder (plus `CLAUDE.md` at the
repo root). It needs `bash`, `jq` and `python3` (standard library only).

## Hard rules

- Findings land as DRAFT. NEVER auto-fix; the repo owner decides every remediation.
- A BLOCK claim ("X blocks Y") must cite the enforcing line: a deny pattern, a non-comment
  `exit 2`, or a `decision:"block"` payload. If you cannot cite it, downgrade the wording to
  DETECT or INSTRUCT.
- READ-ONLY against the repo, with two exceptions that are the owner's to review and commit: the
  registry files written by `registry.py init`, and the report written in step 6.
- Report content: every finding cites its source (file and line) so a reader can check it, plus a short summary. A table of claim, source file and line at the top is one way to lay this out (the template in step 6).

## Procedure

1. **Resolve the target.** The argument, or the current directory. Check the module is installed:
   `.claude/scripts/harness-audit/scan.sh` must exist. If it does not, stop and say so (install the
   harness-governance module first); do not improvise a scan.
2. **Scan.** Settings are read in this order: `.claude/settings.json`, then
   `settings.local.json` in the same folder. Cite which file each finding came from.

   ```bash
   mkdir -p .claude/harness-registry/reports
   bash .claude/scripts/harness-audit/scan.sh . --out .claude/harness-registry/reports/scan.json
   grep '"target"' .claude/harness-registry/reports/scan.json
   ```

   If `scan.sh` exits non-zero, stop and show its message: it removes the report file on failure, so
   there is nothing to read; never fall back to an older scan. Confirm the printed `target` is the
   repo you meant to audit: a wrong working directory
   silently audits the wrong repo.
3. **Registry diff or init.**
   - No registry yet (`.claude/harness-registry/` has no domain file): create it, and tell the
     owner the result is a draft baseline. Each artifact's domain is taken from the module that
     installed it; anything else is marked TODO-curate for the owner. Leave the files uncommitted.

     ```bash
     python3 .claude/scripts/harness-audit/registry.py init .
     ```

   - Registry exists:

     ```bash
     python3 .claude/scripts/harness-audit/diff-registry.py .claude/harness-registry/reports/scan.json .claude/harness-registry
     ```

     Exit 0 = clean (0 diffs), 1 = findings, 2 = the registry or scan is malformed (report that as
     a finding; do not repair it). A line `uncurated: N entries` is a note, not drift. Files that
     belong to a module-only domain (the copy-and-rename skill skeleton) are listed in
     `_ignored.json` and are skipped on purpose.
   - When the owner accepts the current state as the new baseline (for example after wiring a hook
     on purpose), re-run `registry.py init . --force`: extracted fields are rewritten from reality
     and what the owner curated is kept. Never hand-edit an extracted field.
4. **Layering violations (judgment).** Read `.claude/rules/` and the CLAUDE.md rows. Flag
   INSTRUCT text that claims to enforce what a hook or deny pattern already enforces (for example
   "this rule blocks X" when `.claude/hooks/` holds the hook that blocks X). Cite both the prose
   location and the enforcing artifact.
5. **Assemble the draft gap report: the six checks.**

   | # | Check | Source |
   |---|---|---|
   | 1 | empty domain (unless `not_applicable`) | diff-registry.py |
   | 2 | layering violation | step 4 (judgment) |
   | 3 | hook file wired nowhere (UNWIRED) | scan findings `unwired-file` |
   | 4 | hook without a `# Enforces:` header, or one that points at a missing file | scan findings |
   | 5 | BLOCK hook without an escape hatch (advisory) | scan findings |
   | 6 | registry-vs-reality drift, schema errors | diff-registry.py |

   Before reporting a finding, read the registry entry's `gaps:` notes: a note such as "copied but
   not wired" means the fact is KNOWN. List it under Known/Accepted, not as a new finding.
   Not covered by this module: whether a skill is ever used (that needs session transcripts); say
   so in the report rather than reporting "clean".
6. **Write the report** to `.claude/harness-registry/reports/<YYYY-MM-DD>-draft.md`:

   ```markdown
   # Harness Audit - <repo> - <date> - STATUS: DRAFT

   ## Findings
   | Claim | Source File | Line/Section |
   |-------|------------|--------------|
   | <every finding, one row, with its citation> | | |

   ## Summary
   <counts per check; new findings vs Known/Accepted; recommended next actions as OPTIONS,
   not applied changes>
   ```

7. **Deliver** the report path and the summary. Remind the owner that the report is a draft, that
   they curate every TODO-curate field and decide every remediation, and that a reader must open
   the cited sources (at least the most important ones) before acting on a finding.
