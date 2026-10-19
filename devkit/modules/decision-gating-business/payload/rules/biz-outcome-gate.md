# Biz Outcome Gate

Go/no-go before craft, for any plan, proposal, feature, metric or system about to be built (not one-line fixes, one-off lookups or investigations, prose edits). No business outcome, no build.

## Four questions (all must be concrete)
1. Which business KPI moves (not an internal proxy such as "quality" or "cleanliness")?
2. By how much (quantified, roughly ok)?
3. Measured when, and by which check (for example a query, dashboard, report or test)?
4. Which named consumer, what decision changes?

Consumer test: name Consumer, Decision, Decision cost, Frequency. An internal team that only passes the work along (for example, the team that built it) is plumbing, not a consumer.

Chained lift (infrastructure): name `plan -> downstream plan -> decision -> KPI -> date`; if any link is missing, defer the work until a downstream consumer actually asks for it.

**Short horizon (hard; default 2 weeks — set your own once, then hold it):** break anything not testable within your horizon into pieces that each lift something within it. Sub-plans must be MECE (mutually exclusive, collectively exhaustive: no overlap, no gap).

Plan header: `## Outcome` (KPI, lift, measured on, consumer, decision), `## Chain` (if infrastructure), `## Craft` (3+ steps only: order, commands, rollback, acceptance). A plan whose Outcome section is incomplete does not go ahead to building until the section is filled in.

No-go: Kill, Defer (write the outcome plan first), or Reframe (surface the upstream problem).

## Target selection
Name the highest-value AND the easiest candidate; if they differ, state the gap. Never silently pick the easy one.

## Lever sizing
For cost/benefit optimization where levers add into a total (even if framed around one): 0. list the other levers; 1. size every lever in the same unit in one table (mark a lever you cannot size as OPEN); 2. rank them by share of the total; 3. do not optimize a small lever (for example under 10% of the total; pick your own threshold) while a larger one is unexamined, unless you defer it with a stated reason; 4. state what was deferred.

## Pressure-test
Fire on strategy, priority and scope questions, help-me-decide, requests to expand, keep or stop a piece of work, research, repeated mistakes, requests to challenge whether or what to build, and decision support (a review feeding a material choice, a meeting held to get information or a decision). Not routine agendas, status syncs, proofreading, edits, or already-gated plans, unless a decision-support trigger is present (a single decision memo or meeting brief still gets the pass). For decision support, get a review from someone (or a fresh agent session) who was not involved in the work, if one is available.

Read the decision through these four lenses (adapt or add lenses to fit your organization), and write one short paragraph per lens. No skill or tool is needed; the lenses are questions you answer from the plan in front of you:
- **Strategy**: does this fit where the business is heading, and what does it displace? Is it the right problem, or a symptom of an upstream one? Who owns it on day one, day thirty, day ninety?
- **Finance**: what is the cash and cost impact (a number, with the assumption behind it), what is the payback, and what is the cheaper alternative that gets most of the lift?
- **Market**: who outside the team is affected (customers, partners or other outside groups that depend on the work), does it strengthen or weaken how your project or organization is seen by the people who depend on it (if that matters in your context), and what would they say if they saw it?
- **Product**: does it move the behavior or capability the product depends on, is it the 20% that delivers most of the value, and what evidence says it works (not what says it should)?

Close with one label for how the lenses see the decision (for example STRONG, CONDITIONAL, WEAK or WRONG-PROBLEM; use your own scale if you have one), then the watchpoints (each with an owner and a trigger) and what would change the view. The lenses advise; the decision-maker decides and may override.
