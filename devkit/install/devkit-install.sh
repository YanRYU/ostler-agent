#!/usr/bin/env bash
# devkit-install.sh — the installer.
# Reads each module's MODULE.md and copies its payload into a target, stamping a
# .devkit-manifest.json (per-file SHA-256 + source_commit).
#
# Usage:
#   devkit-install.sh install <target> [module ...] [--force] [--allow-dirty]
#                                                  all-or-nothing: a refusal writes nothing;
#                                                  files dropped from a selected module upstream
#                                                  are pruned; a dirty factory needs --allow-dirty
#   devkit-install.sh update  <target> [module ...] [--force] [--allow-dirty]
#                                                  install over the modules the manifest records; migrates
#                                                  1.x records to 2.0 in the same transaction
#   devkit-install.sh check   <target>
#   devkit-install.sh --check <target>            # alias for: check <target>
#   devkit-install.sh patch   <target> <path> --reason "<why you made this edit>"
#                                                  record a downstream edit as pending-upstream
#   devkit-install.sh outdated <target> [module ...]
#                                                  one line per installed module whose source changed
#   devkit-install.sh remove-module <target> <name> [--force]
#                                                  remove a module's clean files and its record
#   devkit-install.sh recover <target>             finish/roll back an interrupted install
#
# Implementation lives in _devkit.py (this wrapper keeps the named, documented entry).
set -euo pipefail

# Do not leave install/lib/__pycache__ behind in the checkout.
export PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Normalize the `--check <target>` alias to `check <target>`
if [ "${1:-}" = "--check" ]; then
    shift
    exec python3 "$HERE/_devkit.py" check "$@"
fi

exec python3 "$HERE/_devkit.py" "$@"
