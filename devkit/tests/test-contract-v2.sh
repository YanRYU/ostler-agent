#!/usr/bin/env bash
# Runs devkit/tests/test_contract_v2.py: the MODULE.md contract v2 (domain, per-file
# mechanism), settings fragments, the manifest projection, the board renderer and the
# generated README module table (it fails when the table was edited by hand).
#
# Usage: bash devkit/tests/test-contract-v2.sh
# Needs bash, python3 and PyYAML (the installer reads MODULE.md with it).
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v "$REPO/devkit/tests/test_contract_v2.py" >"$OUT" 2>&1
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
