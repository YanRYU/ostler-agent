#!/usr/bin/env python3
"""Warn when a file edit adds a personal absolute path to a shared file."""
# Enforces: .agents/ostler/rules/privacy.md

import json
import os
from pathlib import Path
import re
import subprocess
import sys


PERSONAL = re.compile(r"(?<![\w/])(?:/Users/[^/\s\"']+|/home/[^/\s\"']+)/(?:[^\s\"']+)")


def shared(cwd, path):
    try:
        root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()).resolve()
        candidate = (Path(cwd) / path).resolve()
        rel = candidate.relative_to(root)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return False
    if any(part in (".git", ".codex") for part in rel.parts):
        return False
    ignored = subprocess.run(["git", "check-ignore", "-q", "--", str(rel)], cwd=root, capture_output=True)
    return ignored.returncode != 0


def patch_edits(command):
    path = None
    for line in command.splitlines():
        if line.startswith(("*** Add File: ", "*** Update File: ")):
            path = line.split(": ", 1)[1]
        elif line.startswith("*** Delete File: "):
            path = None
        elif path and line.startswith("+") and not line.startswith("+++"):
            yield path, line[1:]


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    tool, inputs = event.get("tool_name"), event.get("tool_input", {})
    if not isinstance(inputs, dict):
        return
    cwd = event.get("cwd") or os.getcwd()
    if tool == "apply_patch":
        edits = patch_edits(inputs.get("command", ""))
    elif tool in ("Edit", "Write"):
        edits = [(inputs.get("file_path", ""), inputs.get("new_string", inputs.get("content", "")))]
    else:
        return
    for path, content in edits:
        if isinstance(path, str) and isinstance(content, str) and PERSONAL.search(content) and shared(cwd, path):
            notice = f"Ostler: personal absolute path in shared file {path}. Replace it with a relative path or a placeholder before committing."
            output = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": notice}}
            if os.environ.get("PERSONAL_PATH_GUARD_MODE") == "block":
                output["hookSpecificOutput"].update({"permissionDecision": "deny", "permissionDecisionReason": notice})
            print(json.dumps(output))
            return


if __name__ == "__main__":
    main()
