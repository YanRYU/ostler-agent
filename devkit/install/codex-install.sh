#!/usr/bin/env bash
# Repository-scoped Codex entry point for the shared devkit installer.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "--check" ]; then
    shift
    exec python3 "$HERE/_devkit.py" --platform codex check "$@"
fi

exec python3 "$HERE/_devkit.py" --platform codex "$@"
