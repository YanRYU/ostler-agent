#!/usr/bin/env python3
# Enforces: rules/privacy.md (no personal paths in shared artifacts)
"""
personal-path-guard.py — PreToolUse hook (Write / Edit / NotebookEdit)

Detects when the agent is about to write a personal absolute path
(/Users/<name>/..., /home/<name>/...) into a SHARED artifact (any file
inside a git-tracked repository). Personal paths leak each user's
directory layout into shared docs / scripts / configs and break
portability.

Behavior modes:
  - MODE=warn (default): print loud warning to stderr, exit 0 (allow)
  - MODE=block: print warning + exit non-zero (PreToolUse blocks the tool)

Set the mode by exporting PERSONAL_PATH_GUARD_MODE before the agent launches,
or by editing the MODE constant below. Start in 'warn' to measure the
false-positive rate before switching to 'block'.

Skipped (never flagged):
  - Target file is OUTSIDE any git repo (machine-local)
  - Target file is under .gitignore (private, not shared)
  - Target file is inside the USER-level config dir (~/.claude); a repo-level
    .claude/ directory is shared and IS checked
  - Target file is a known machine-local config (~/.zshrc, .env, etc.)
  - Content uses portable forms ($HOME, ${HOME}, ~/, $VAR, ${VAR})
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

MODE = os.environ.get("PERSONAL_PATH_GUARD_MODE", "warn").lower()

# Match /Users/<name>/ or /home/<name>/ where <name> is a username-shaped token.
# Excluded by design: /Users (root literal, no name segment) — too rare to matter
PERSONAL_PATH_RE = re.compile(
    r"(?:^|[\s\"'`(<])(/Users/[a-zA-Z0-9_.\-]+/|/home/[a-zA-Z0-9_.\-]+/)"
)

# Files where personal paths are fine (machine-local configs)
PRIVATE_FILE_PATTERNS = [
    re.compile(r"^.+/\.(zsh|bash)rc$"),
    re.compile(r"^.+/\.profile$"),
    re.compile(r"^.+/\.env(\.|$)"),
    re.compile(r"^.+/settings\.local\.json$"),
]


def is_user_config_file(file_path: str) -> bool:
    """True if the file is inside the user-level config dir (~/.claude).

    Only that one directory is exempt; any other .claude/ (e.g. a committed
    repo-level <repo>/.claude/rules/) is a shared artifact.
    """
    try:
        user_dir = (Path.home() / ".claude").resolve()
        target = Path(file_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return target == user_dir or user_dir in target.parents


def is_private_file(file_path: str) -> bool:
    if is_user_config_file(file_path):
        return True
    return any(pat.match(file_path) for pat in PRIVATE_FILE_PATTERNS)


def in_git_repo(file_path: str) -> bool:
    """True if the target file is under a git working tree."""
    parent = str(Path(file_path).expanduser().resolve().parent)
    try:
        result = subprocess.run(
            ["git", "-C", parent, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=2,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def is_gitignored(file_path: str) -> bool:
    """True if git would ignore this file (so it's not 'shared')."""
    p = Path(file_path).expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(p.parent), "check-ignore", "-q", str(p)],
            capture_output=True, timeout=2,
        )
        return result.returncode == 0  # 0 = ignored
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def extract_content(tool_name: str, tool_input: dict) -> str | None:
    if tool_name == "Write":
        return tool_input.get("content")
    if tool_name == "Edit":
        return tool_input.get("new_string")
    if tool_name == "NotebookEdit":
        return tool_input.get("new_source")
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # malformed input → never block

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    if tool_name not in {"Write", "Edit", "NotebookEdit"}:
        return 0

    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        return 0

    content = extract_content(tool_name, tool_input)
    if not content:
        return 0

    # Only enforce when writing into a SHARED artifact (git-tracked, not ignored,
    # not a known machine-local config).
    if is_private_file(file_path):
        return 0
    if not in_git_repo(file_path):
        return 0
    if is_gitignored(file_path):
        return 0

    matches = PERSONAL_PATH_RE.findall(content)
    if not matches:
        return 0

    # De-dupe and clip
    unique = sorted(set(matches))
    shown = unique[:5]

    msg = [
        "",
        "⚠️  personal-path-guard: shared artifact contains a personal absolute path.",
        f"   target file : {file_path}",
        f"   tool        : {tool_name}",
        f"   matches     : {shown}" + (f"  (+{len(unique)-5} more)" if len(unique) > 5 else ""),
        "",
        "   Why this matters: hard-coded /Users/<name>/ paths break portability —",
        "   any teammate reading this artifact has to find-and-replace before they",
        "   can use it. Use $HOME / ~/ / env-var conventions instead.",
        "",
        "   Suggested fixes:",
        "     /Users/<name>/some/file        ->  $HOME/some/file   or   ~/some/file",
        "     /Users/<name>/.../<repo>/file  ->  a path relative to the repo root,",
        "         or ${REPO_ROOT}/file if you define such a variable",
        "",
        f"   Mode: {MODE}.  " + (
            "Blocking the tool call." if MODE == "block"
            else "Allowing for now (warn mode). Set PERSONAL_PATH_GUARD_MODE=block to enforce."
        ),
        "",
    ]
    print("\n".join(msg), file=sys.stderr)

    return 2 if MODE == "block" else 0


if __name__ == "__main__":
    sys.exit(main())
