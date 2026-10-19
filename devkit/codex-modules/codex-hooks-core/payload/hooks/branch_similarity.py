#!/usr/bin/env python3
"""Warn before creating a branch that already exists locally or remotely."""
# Enforces: .agents/ostler/rules/git-hygiene.md

import json
import os
import re
import shlex
import subprocess
import sys


STOPWORDS = {"feat", "fix", "chore", "docs", "refactor", "test", "perf", "style",
             "build", "ci", "revert", "hotfix", "bug", "bugfix", "wip", "tmp",
             "temp", "draft", "scratch", "pr"}


def tokens(branch):
    return {word.lower() for word in re.split(r"[/_-]", branch) if word and word.lower() not in STOPWORDS}


def git(*args):
    return subprocess.run(["git", *args], cwd=os.getcwd(), capture_output=True, text=True)


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
    if not isinstance(command, str):
        return
    # Parse one simple git invocation; complex shell programs are left to review.
    if re.search(r"[;&|`\n]", command):
        return
    try:
        words = shlex.split(command)
    except ValueError:
        return
    if not words or words[0] != "git":
        return
    branch = None
    if len(words) >= 4 and ((words[1] == "switch" and words[2] in ("-c", "-C", "--create", "--force-create"))
                            or (words[1] == "checkout" and words[2] in ("-b", "-B"))):
        branch = words[3]
    elif len(words) >= 3 and words[1] == "branch" and not words[2].startswith("-"):
        branch = words[2]
    if not branch or branch.startswith("-") or git("check-ref-format", "--branch", branch).returncode:
        return
    notices = []
    if git("show-ref", "--verify", "--quiet", "refs/heads/" + branch).returncode == 0:
        notices.append(f"Branch {branch} already exists locally. Reuse or inspect it before creating another.")
    elif git("show-ref", "--verify", "--quiet", "refs/remotes/origin/" + branch).returncode == 0:
        notices.append(f"Branch {branch} already exists on origin. Inspect it before creating another.")
    refs = git("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin")
    if refs.returncode == 0:
        names = {name.removeprefix("origin/") for name in refs.stdout.splitlines()}
        proposed = tokens(branch)
        if len(proposed) >= 2:
            similar = sorted(name for name in names if name != branch and name != "HEAD"
                             and len(proposed & tokens(name)) >= 2)
            if similar:
                notices.append("Branches sharing at least two domain tokens: " + ", ".join(similar[:8]) + ". Inspect for overlapping work.")
    if not notices:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(notices)}}))


if __name__ == "__main__":
    main()
