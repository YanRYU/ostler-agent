#!/usr/bin/env bash
# Enforces: rules/git-hygiene.md (stash discipline)
# PostToolUse hook for Bash: after `git push` or `git commit`, surface remaining stashes.
# Per rules/git-hygiene.md — the agent must resolve every stash it creates before
# session end. This hook makes invisible stash state visible at the moments the agent
# is most likely to "finish and walk away".
#
# Non-blocking: prints to stderr only. The discipline lives in the rule file;
# this hook just removes the "I forgot they were there" excuse.

set -u

# Read tool call JSON from stdin
input=$(cat 2>/dev/null || echo "")
[ -z "$input" ] && exit 0

# Extract the bash command (requires jq on PATH; without it the hook exits 0 and prints nothing)
cmd=$(echo "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)
[ -z "$cmd" ] && exit 0

# Only fire on git push / git commit
echo "$cmd" | grep -qE '(^|[[:space:]]|;|&&|\|\|)git[[:space:]]+(push|commit)([[:space:]]|$)' || exit 0

# Must be in a git repo
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

stashes=$(git stash list 2>/dev/null)
[ -z "$stashes" ] && exit 0

{
  echo ""
  echo "[stash-leak-check] git stash list is non-empty after: $cmd"
  echo "$stashes" | sed 's/^/  /'
  echo ""
  echo "Per rules/git-hygiene.md: resolve every stash created this session"
  echo "before close. Disposition = pop / drop (with reason) / handoff to user."
  echo ""
} >&2

exit 0
