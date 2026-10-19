# Technical Gate

Go/no-go for how something ships; run after the business outcome gate. Applies to plans of 3+ steps, build/ship/deploy/migration requests, any change with blast radius beyond the file in hand. Not for one-line fixes, one-off lookups or investigations, prose edits, status checks, or already-gated plans. Failing plan: fix before implementing. A build that fails either gate does not proceed.

**Precondition (only when someone else's analysis doc is the premise):** a subagent (or a second reader) re-reads every cited source, reproduces every load-bearing number, and confirms the unit of analysis (customer, product, region, time period). List discrepancies. "Their doc already did the analysis" is not acceptable.

## Four questions
1. Blast radius: named files/systems/consumers affected.
2. Rollback: a concrete revert step, or "additive, nothing to roll back".
3. Verification: the command/query proving the goal (end to end, not smoke), run or named pending. Test testable caveats in-session.
4. Order: dependency/sequencing of steps.

Triggers include a request to challenge or stress-test how something will be executed, 3+ step plans, skipped or deferred testing, migrations, schema/contract changes, anything your repo marks as protected (for example trunk branches or production config).

This rule is an instruction only. The plan-exit hook that checks a plan for a rollback and a verification step ships in the decision-gating-business module.
