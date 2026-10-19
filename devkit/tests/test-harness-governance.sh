#!/usr/bin/env bash
# Runs devkit/tests/test_harness_governance.py: the harness-governance module (installed scanner,
# registry differ and registry bootstrap run in a target with the factory unreachable and no
# PyYAML; baseline-then-drift; the shipped schema equals the installer's vocabulary; shipped text
# passes the lexicon). Without the optional lexicon and registry schema inputs, those tests skip.
#
# Usage: bash devkit/tests/test-harness-governance.sh
# Needs bash, python3, PyYAML and jq.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v "$REPO/devkit/tests/test_harness_governance.py" >"$OUT" 2>&1
rc=$?
tail -n 5 "$OUT"

total=$(sed -n 's/^Ran \([0-9]*\) tests\{0,1\} in .*/\1/p' "$OUT")
total=${total:-0}
bad=$(grep -c -E ' \.\.\. (FAIL|ERROR)$' "$OUT")
passed=$((total - bad))
skipped=$(sed -n 's/^FAILED (.*skipped=\([0-9]*\).*/\1/p; s/^OK (skipped=\([0-9]*\))/\1/p' "$OUT" | head -1)
skipped=${skipped:-0}
passed=$((passed - skipped))
echo "$passed/$total passed, $skipped skipped"
if [ "$rc" -ne 0 ] || [ "$bad" -ne 0 ] || [ "$total" -eq 0 ]; then
  echo "FAILED"
  exit 1
fi
exit 0
