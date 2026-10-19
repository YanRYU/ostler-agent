#!/usr/bin/env bash
# Tests devkit/modules/secrets-privacy/payload/hooks/personal-path-guard.py:
# only the USER-level config dir ($HOME/.claude) is exempt; a repo-level
# .claude/ directory is a shared artifact and is checked like any other file.
#
# Usage: bash devkit/tests/test-personal-path-guard.sh
# Needs bash, python3 and git; builds everything under a temp dir.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO/devkit/modules/secrets-privacy/payload/hooks/personal-path-guard.py"

pass=0; fail=0; total=0
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
WORK=$(cd "$WORK" && pwd -P)

# Personal-looking path assembled from pieces so this file holds no literal one.
PERSONAL="/Us""ers/someone/proj/"
BAD_CONTENT="see $PERSONAL file"
GOOD_CONTENT='see $HOME/proj/file'

export HOME="$WORK/home"
mkdir -p "$HOME"
REPO_DIR="$WORK/repo"
mkdir -p "$REPO_DIR"
git -C "$REPO_DIR" init -q
printf 'ignored.md\n' > "$REPO_DIR/.gitignore"
# The fake HOME is itself inside a git repo, to prove the exemption is by path.
git -C "$WORK" init -q
mkdir -p "$REPO_DIR/.claude/rules" "$REPO_DIR/.claude/notes" "$REPO_DIR/docs" "$HOME/.claude"

# run_hook <file> <content> -> exit code
run_hook() {
  FILE="$1" CONTENT="$2" python3 - <<'PY' | PERSONAL_PATH_GUARD_MODE=block python3 "$HOOK" >/dev/null 2>&1
import json, os
print(json.dumps({"tool_name": "Write",
                  "tool_input": {"file_path": os.environ["FILE"], "content": os.environ["CONTENT"]}}))
PY
  echo "${PIPESTATUS[1]}"
}

# expect <name> <flagged|clean> <file> <content>
expect() {
  local name="$1" want="$2" file="$3" content="$4" rc
  total=$((total+1))
  rc=$(PERSONAL_PATH_GUARD_MODE=block run_hook "$file" "$content")
  if { [ "$want" = flagged ] && [ "$rc" != 0 ]; } || { [ "$want" = clean ] && [ "$rc" = 0 ]; }; then
    pass=$((pass+1)); printf 'PASS  %s\n' "$name"
  else
    fail=$((fail+1)); printf 'FAIL  %s (want %s, exit %s)\n' "$name" "$want" "$rc"
  fi
}

expect "1 repo-level .claude/rules file with personal path is flagged" flagged "$REPO_DIR/.claude/rules/x.md" "$BAD_CONTENT"
expect "2 user-level \$HOME/.claude file is not flagged"              clean   "$HOME/.claude/anything.md"      "$BAD_CONTENT"
expect "3 repo docs file with personal path is flagged"               flagged "$REPO_DIR/docs/x.md"            "$BAD_CONTENT"
expect "4 gitignored file is not flagged"                             clean   "$REPO_DIR/ignored.md"           "$BAD_CONTENT"
expect "5 portable \$HOME form in repo-level .claude file not flagged" clean  "$REPO_DIR/.claude/rules/x.md"   "$GOOD_CONTENT"
expect "6 repo-level .claude/notes file with personal path flagged" flagged "$REPO_DIR/.claude/notes/x.md" "$BAD_CONTENT"

echo "$pass/$total passed"
[ "$fail" -eq 0 ]
