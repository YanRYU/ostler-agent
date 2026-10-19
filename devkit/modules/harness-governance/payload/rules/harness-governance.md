# Harness Governance

After any change to hooks, rules, settings permissions or wiring, skills or agents, re-run the harness audit. Never hand-edit computed registry fields.

Taxonomy: `.claude/harness-registry/_schema.json` (the domains and the six mechanisms).
- Map every control artifact to one cell: a mechanism (BLOCK / ASK / DETECT / INSTRUCT / ROUTE / VERIFY) and a domain. Mechanism is behavior, not file type: a hook that exits 2 is BLOCK.
- The registry's extracted fields are computed by `.claude/scripts/harness-audit/scan.sh`; curated fields (domain, origin, gaps) are yours to edit.
- Re-run the `harness-audit` skill (or `scan.sh` then `diff-registry.py`, both in `.claude/scripts/harness-audit/`) after changes. Findings land as DRAFT; a BLOCK claim cites the enforcing line; never auto-fix.
- Create the registry once with `registry.py init`; the registry lives in `.claude/harness-registry/`.
