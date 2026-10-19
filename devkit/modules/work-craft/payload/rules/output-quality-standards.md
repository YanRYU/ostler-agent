# Output Quality Standards

Always-on gates for every artifact this agent produces for the person it is working for. Fix before delivery.

1. **Commit before claim.** Never say "done" without evidence: commit hash/branch (code), file path (docs), result or dry-run output (queries, scripts, or other runnable work, where your tools support a dry run). "Designed" is not "complete". When correcting numbers in an existing doc, fix the headline tables, not just an appendix.
   **Smoke is not e2e** (runs vs achieves the goal); if e2e cannot run in-session, name it and flag pending.
2. **Hands-off for recipient-facing docs.** Name the recipient; the doc must let them finish Day 1 and recover from a failure alone, with setup/auth, step-by-step procedures with expected output, "you will see X, do Y" recovery, zero undefined jargon. Never "ask the author". Before delivery, scan for red flags: architecture without procedures; undefined jargon; no "when it breaks" steps; internal systems without access steps (account, project, CLI); commands or queries without where to run them and what the output means; assumed tool install/auth. Any present: fix or flag to the person you are working for, never deliver silently. Test: Day 1, an unattended failure, a first month without the author.
3. **Proposals answer four questions:** problem, who is affected, options with tradeoffs, recommendation with risks. Never "let's discuss".
4. **Lead with what is done** (artifacts, evidence), then next steps with dates.
5. **Self-identify problems beyond the spec:** inconsistencies, missing fields, scope to exclude, unflagged gaps.
6. **Encode lessons into systems** (checklist, rule, context, test).
7. **Show progress, not delay explanations:** "Blocked by X, resolved by Y, result". If unresolved: tried, next, next checkpoint.
8. **Architecture self-protects:** caps, validation, permissions live in the architecture layer, not in consumers or docs.
9. **LLM-produced artifacts land as draft.** Promotion to active is a human gate (for example a drafts folder, then the live folder); never auto-merge LLM-suggested rule edits.
10. **Test testable caveats in-session.** If a caveat can be tested cheaply with data already in scope (set your own time budget, for example 30 minutes), test it before shipping rather than deferring it to a later version. Defer only if it needs external data or an experiment you cannot run. Mark an unverified factual claim from someone else as unverified when you record it (for example with a tag such as UNVERIFIED ASSUMPTION), and test it before it goes into a plan or handover document; otherwise ship it tagged with the check that would settle it.
11. **Subagent reports open with a facts-and-sources section** (for example a `## Core Facts` table: Claim | Source File | Line/Section) followed by a summary; adapt the headings to your own report format. The caller reads at least the top 3 cited sources before acting.
12. **Self-test before done.** Run what you can (syntax, dry run, compile) and report ran/passed/failed; give copy-paste commands with expected output for what you cannot; changes to state get a rollback step or a check (command, test, or query) that verifies the result. Config and governance files: back up first, then scan for stale references.

Standards 1-4 are verifiable per artifact; 5-12 are behavioral.

Pairs with `.claude/rules/communication-style.md` (how the report itself reads).
