#!/bin/bash
# Enforces: none — convenience hook
# plan-exit-print-location.sh — PostToolUse hook for ExitPlanMode
# Prints the plan file location inline after exiting plan mode. Claude Code plan mode
# writes the plan as a .md file under a `.claude/plans/` directory (in the project or in
# your user-level config directory); this hook finds the last such path in the session
# transcript and echoes it so you can open the plan. Optional: it changes nothing.
# Needs jq: without it the output is "Plan file: (unable to determine — no transcript)".

INPUT="$(cat)"
TRANSCRIPT_PATH="$(echo "$INPUT" | jq -r '.transcript_path // empty')"

if [[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]]; then
  echo "Plan file: (unable to determine — no transcript)"
  exit 0
fi

# Find last plan file written/edited in this session
PLAN_PATH=$(grep -oE '"file_path"\s*:\s*"[^"]*\.claude/plans/[^"]*\.md"' \
    "$TRANSCRIPT_PATH" 2>/dev/null \
    | tail -1 | grep -oE '/[^"]*\.md')

if [[ -n "$PLAN_PATH" ]]; then
  echo "Plan file: $PLAN_PATH"
else
  echo "Plan file: (no plan file found in session)"
fi

exit 0
