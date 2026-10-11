#!/usr/bin/env bash
# Runs devkit/tests/test_txn.py: crash safety at every fault point, the
# optional derived file, remove_paths, compare-and-swap, locking, fingerprint.
#
# Usage: bash devkit/tests/test-txn.sh
# Needs bash and python3 (stdlib only); every test uses a temp-dir sandbox.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v "$REPO/devkit/tests/test_txn.py" >"$OUT" 2>&1
rc=$?
tail -n 5 "$OUT"

total=$(grep -c ' \.\.\. ' "$OUT")
bad=$(grep -c ' \.\.\. \(FAIL\|ERROR\)$' "$OUT")
passed=$((total - bad))
echo "$passed/$total passed"
if [ "$rc" -ne 0 ] || [ "$bad" -ne 0 ] || [ "$total" -eq 0 ]; then
  echo "FAILED"
  exit 1
fi
exit 0
