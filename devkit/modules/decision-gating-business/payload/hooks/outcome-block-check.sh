#!/usr/bin/env bash
# Enforces: rules/biz-outcome-gate.md (business go/no-go) + rules/technical-gate.md (technical go/no-go)
# PreToolUse hook for ExitPlanMode: before a plan is shown to you, check it carries the two
# decision-gate blocks and say so if either is absent.
#   business:  an ## Outcome block (KPI / lift / measured-on / consumer / decision)
#   technical: a rollback path AND an end-to-end verification step named
#
# Mode (OUTCOME_CHECK_MODE):
#   warn  (default) prints to stderr, exit 0 always. A reminder, not a gate.
#   block exits 2 with the same message and requires a line that is exactly "## Outcome" outside
#         fenced and indented code (field words, other heading levels and variants do not pass);
#         rollback + verification are still required. Plan text comes from tool_input.plan, else
#         from the last .claude/plans/*.md the transcript shows a Write or Edit of (a Read never counts). It refuses (exit 2, one line)
#         when jq is missing, the input cannot be read, or no plan can be found.
#   Warn mode reads the plan from the same two places, but never refuses: without jq, with unreadable
#   input, or with no plan found it stays silent and exits 0.
# The mode value is case-insensitive and trimmed.
# Unknown values behave as warn. OUTCOME_CHECK_DISABLED=1 silences both modes for the session
# (one-line fixes and pure-research plans legitimately have no Outcome block).
#
# Wire it (copied, not wired by the installer): a PreToolUse entry with matcher ExitPlanMode in
# .claude/settings.json running `bash .claude/hooks/outcome-block-check.sh`. Needs jq.

set -u

[ "${OUTCOME_CHECK_DISABLED:-0}" = "1" ] && exit 0

# Mode: lowercased and trimmed; anything but "block" is warn.
mode=$(printf '%s' "${OUTCOME_CHECK_MODE:-warn}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
[ "$mode" = "block" ] || mode=warn

# Block mode fails closed: without jq the plan cannot be read, so refuse rather than pass silently.
if [ "$mode" = "block" ] && ! command -v jq >/dev/null 2>&1; then
    echo "[outcome-block-check] jq not found; block mode cannot check plans" >&2
    exit 2
fi

input=$(cat 2>/dev/null || echo "")
if [ -z "$input" ]; then
    [ "$mode" = "block" ] || exit 0
    echo "[outcome-block-check] cannot read hook input" >&2
    exit 2
fi

tool=$(echo "$input" | jq -r '.tool_name // ""' 2>/dev/null)
if [ "$?" -ne 0 ] && [ "$mode" = "block" ]; then
    echo "[outcome-block-check] cannot read hook input" >&2
    exit 2
fi
[ "$tool" = "ExitPlanMode" ] || exit 0

plan=$(echo "$input" | jq -r '.tool_input.plan // ""' 2>/dev/null)
if [ -z "$plan" ]; then
    # ExitPlanMode usually carries no plan text, so read the plan file the session wrote last (the
    # last .claude/plans/*.md named in the transcript, which is where Claude Code plan mode writes the plan). Both modes use this source order.
    transcript=$(echo "$input" | jq -r '.transcript_path // empty' 2>/dev/null)
    plan_path=""
    if [ -n "$transcript" ]; then
        # Only Write/Edit tool_use records count (they write the plan); a Read of an older plan must not mask it.
        plan_path=$(jq -Rr 'fromjson? | .. | objects
                | select(.type == "tool_use" and ((.name // "") | IN("Write", "Edit", "MultiEdit")))
                | (.input.file_path? // empty) | strings' "$transcript" 2>/dev/null \
            | grep -E '/\.claude/plans/.*\.md$' | tail -1)
    fi
    if [ -n "$plan_path" ] && [ -f "$plan_path" ]; then
        plan=$(cat "$plan_path" 2>/dev/null)
    fi
    if [ -z "$plan" ]; then
        [ "$mode" = "block" ] || exit 0   # warn mode never refuses: no plan found stays silent
        echo "[outcome-block-check] no plan found in tool input or plan file" >&2
        exit 2
    fi
fi

# Lowercase copy for case-insensitive marker checks.
lower=$(printf '%s' "$plan" | tr '[:upper:]' '[:lower:]')

# ─── F: business Outcome block ───────────────────────────────────────────────
# Warn mode: pass on an "outcome" heading OR ≥2 of the block's field markers (a lenient check).
# Block mode: pass only on the exact "## Outcome" heading (see below).
has_outcome=0
if [ "$mode" = "block" ]; then
    # Strict: a line that is exactly "## Outcome" (trailing whitespace allowed), outside fenced
    # code blocks (a fence closes only on the same character, at least as long, with no text after
    # it; an unclosed fence runs to the end) and not indented (a tab, or 4+ leading spaces, is code).
    printf '%s\n' "$plan" | awk '
        {
            is_f = 0
            if (match($0, /^ ? ? ?(```+|~~~+)/)) {
                m = substr($0, RSTART, RLENGTH); sub(/^ +/, "", m)
                n = length(m); c = substr(m, 1, 1)
                rest = substr($0, RSTART + RLENGTH); is_f = 1
            }
            if (fence) {   # inside a fence: only a matching bare closer (same char, >= length) ends it
                if (is_f && c == fence && n >= flen && rest ~ /^[[:space:]]*$/) fence = ""
                next
            }
            if (is_f) { fence = c; flen = n; next }
            if ($0 ~ /^## Outcome[[:space:]]*$/) found = 1
        }
        END { exit found ? 0 : 1 }' && has_outcome=1
elif printf '%s' "$lower" | grep -qE '^[[:space:]]*#+[[:space:]]*outcome'; then
    has_outcome=1
else
    fields=0
    printf '%s' "$lower" | grep -qE 'kpi|business metric'      && fields=$((fields+1))
    printf '%s' "$lower" | grep -qE 'lift|impact|move[sd]?'    && fields=$((fields+1))
    printf '%s' "$lower" | grep -qE 'measured|by when|dashboard' && fields=$((fields+1))
    printf '%s' "$lower" | grep -qE 'consumer|decision'        && fields=$((fields+1))
    [ "$fields" -ge 2 ] && has_outcome=1
fi

# ─── G: technical soundness markers ──────────────────────────────────────────
has_rollback=0
printf '%s' "$lower" | grep -qE 'rollback|revert|restore|additive|nothing to roll back' && has_rollback=1
has_verify=0
printf '%s' "$lower" | grep -qE 'verif|e2e|end-to-end|acceptance|test|dry.?run|smoke' && has_verify=1

[ "$has_outcome" = "1" ] && [ "$has_rollback" = "1" ] && [ "$has_verify" = "1" ] && exit 0

{
    echo ""
    echo "[outcome-block-check] this plan is missing a decision-gate block:"
    [ "$has_outcome" = "0" ] && cat <<'F'
  • Business (biz-outcome-gate.md): no ## Outcome block. Add — the business KPI that moves
    (not an internal proxy) / expected lift / measured-on (date + how you will check it, e.g. a query, dashboard or test) / consumer (who relies on it) / decision (what it changes). If this is a
    one-line fix or pure research, the gate doesn't apply — carry on.
F
    [ "$has_rollback" = "0" ] && echo "  • Technical (technical-gate.md): no rollback path named (or state 'additive')."
    [ "$has_verify" = "0" ]   && echo "  • Technical (technical-gate.md): no e2e verification step named (smoke ≠ e2e)."
    if [ "$mode" = "block" ]; then
        echo "  Blocking (OUTCOME_CHECK_MODE=block). Add the missing block, or set OUTCOME_CHECK_MODE=warn / OUTCOME_CHECK_DISABLED=1."
    else
        echo "  Advisory only — not blocking. Silence with OUTCOME_CHECK_DISABLED=1."
    fi
    echo ""
} >&2

[ "$mode" = "block" ] && exit 2
exit 0
