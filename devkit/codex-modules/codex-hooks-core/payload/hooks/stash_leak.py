#!/usr/bin/env python3
"""Surface remaining stashes after a commit or push."""
# Enforces: .agents/ostler/rules/git-hygiene.md

import json
import os
import re
import subprocess
import sys


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    if not isinstance(event, dict) or event.get("tool_name") != "Bash":
        return
    inputs = event.get("tool_input")
    if not isinstance(inputs, dict):
        return
    command = inputs.get("command", "")
    if not isinstance(command, str) or not re.search(r"(?:^|[;&|\n])\s*git\s+(?:commit|push)\b", command):
        return
    result = subprocess.run(["git", "stash", "list"], cwd=os.getcwd(), capture_output=True, text=True)
    if result.returncode or not result.stdout.strip():
        return
    print(json.dumps({"systemMessage": "Ostler: remaining Git stashes after this command:\n" + result.stdout.strip()}))


if __name__ == "__main__":
    main()
