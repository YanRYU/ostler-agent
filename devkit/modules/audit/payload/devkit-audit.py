#!/usr/bin/env python3
"""devkit-audit — drift + enforcement-gap checker (stdlib only, self-contained).

Copy-on-build (see rules/self-contained-agents.md): this script runs inside a target
with the devkit factory deleted. It depends on nothing but the Python standard library.

Jobs:
  1. Drift: read <target>/.devkit-manifest.json, recompute SHA-256 of each listed
     file, classify clean / modified-downstream / forked / missing. Exit non-zero
     if any file is modified-downstream or missing.
  2. Enforcement gaps: cross-check .claude/hooks/ `Enforces:` headers against
     .claude/rules/ (informational).

Usage: python3 devkit-audit.py [TARGET_DIR]
  Default TARGET_DIR: the directory you run it from if it holds .devkit-manifest.json,
  else the parent of the directory containing this script (the repo root when the
  script is installed as .claude/devkit-audit.py), else the script's own directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Built split so this detector script never false-positives on its own source.
FORK_MARKER = "devkit:" + "forked"

LEDGER_KEY = "patched_pending_upstream"
# Lowercase only (matches hashlib's own hexdigest() convention, which is what
# patch() always writes) and checked via .fullmatch(), never .match() with a
# `^...$` anchor -- `$` matches before a trailing "\n", so "a"*64 + "\n" would
# otherwise pass as "exactly 64 hex characters" when it is 65.
# Uppercase hex is rejected because it would never match hashlib's lowercase
# hexdigest() and would always misreport STALE.
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def has_fork_marker(path: Path) -> bool:
    try:
        return FORK_MARKER in path.read_text(errors="ignore")
    except OSError:
        return False


def collect_managed(manifest: dict) -> dict[str, dict]:
    """path -> {"sha256": recorded sha, "mode": recorded mode} across every install."""
    managed: dict[str, dict] = {}
    for install in manifest.get("installs", []) or []:
        for f in install.get("files", []) or []:
            path = f.get("path")
            if isinstance(path, str):
                managed[path] = {"sha256": f.get("sha256", ""), "mode": f.get("mode", "overwrite")}
    return managed


def validate_ledger(manifest: dict, managed: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """Validate patched_pending_upstream BEFORE any file classification.

    Returns (ledger_by_path, errors). A non-empty errors list means the whole
    ledger is malformed and classification must not run (exit 2, every
    offending entry named).

    Why a `mode: merge` file cannot carry a pending-upstream entry: a merged
    file's content is not a copy of the payload, so its sha256 cannot be pinned
    to one patched version. Only `mode: overwrite` files can be ledgered.
    """
    _absent = object()
    raw = manifest.get(LEDGER_KEY, _absent)
    errors: list[str] = []
    if raw is _absent:
        raw = []  # an absent key is a valid manifest, treated as an empty ledger.
    elif not isinstance(raw, list):
        # A PRESENT key that is null (or any other non-array JSON value) is malformed,
        # not an empty ledger — `null` must not silently degrade to "no ledger" and
        # skip past validation.
        errors.append(f"{LEDGER_KEY} is not a JSON array: {raw!r}")
        return {}, errors

    seen_paths: dict[str, int] = {}
    ledger_by_path: dict[str, dict] = {}
    for idx, entry in enumerate(raw):
        label = f"entry #{idx}"
        if not isinstance(entry, dict):
            errors.append(f"{label}: not an object: {entry!r}")
            continue
        path = entry.get("path")
        label = f"entry #{idx} (path={path!r})"
        if not isinstance(path, str) or path not in managed:
            errors.append(f"{label}: path is not an exact match for a manifest-managed path")
            continue
        if path in seen_paths:
            errors.append(f"{label}: duplicate path (also entry #{seen_paths[path]})")
            continue
        seen_paths[path] = idx
        sha = entry.get("sha256")
        if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
            errors.append(f"{label}: sha256 absent, or not exactly 64 hex characters")
            continue
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason:
            errors.append(f"{label}: reason absent, empty, or not a string")
            continue
        if managed[path]["mode"] != "overwrite":
            errors.append(
                # repr() the mode: it comes from the unvalidated manifest, and an
                # embedded newline must not be able to print a fake `refresh:` line.
                f"{label}: manifest record has mode {managed[path]['mode']!r}, not 'overwrite' "
                "(a mode:merge file cannot carry a pending-upstream entry: its merged content has no single payload sha to pin)"
            )
            continue
        ledger_by_path[path] = {"path": path, "sha256": sha, "reason": reason, "date": entry.get("date")}
    return ledger_by_path, errors


def age_label(date_str: object) -> str:
    if not isinstance(date_str, str):
        return "(age unknown)"
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return "(age unknown)"
    # Ledger dates are stamped in UTC (patch()'s `datetime.now(timezone.utc)`) —
    # compare against UTC "today" too, or the age is off by one near UTC midnight.
    return f"({(datetime.now(timezone.utc).date() - d).days}d)"


def classify_verdict(fpath: Path, recorded: str, ledger_entry: dict | None) -> tuple[str, bool, str | None, str]:
    """Returns (state, fail, note, label); state is one of clean / modified-downstream / pending-upstream / pending-upstream-stale / forked / missing."""
    if not fpath.exists():
        note = "ledger entry ignored — a ledger excuses an edit, never a deletion" if ledger_entry else None
        return "missing", True, note, "missing"
    if has_fork_marker(fpath):
        note = "pending-upstream entry present on forked file (contradictory)" if ledger_entry else None
        return "forked", False, note, "forked"

    cur = sha256(fpath)
    if cur == recorded:
        note = "obsolete ledger entry — clear it" if ledger_entry else None
        return "clean", False, note, "clean"

    if ledger_entry is None:
        return "modified-downstream", True, None, "modified-downstream"
    if ledger_entry["sha256"] == cur:
        return "pending-upstream", False, None, f"pending-upstream {age_label(ledger_entry.get('date'))}"
    return "pending-upstream-stale", True, None, "pending-upstream STALE"


def _feedback_dir() -> str:
    """Absolute path of the feedback-log directory, resolved when the audit runs.

    The log is a local JSONL record of the fixes you recorded with `patch`: review
    it with any JSON tool, or attach it to an issue or pull request when
    contributing a fix back to the kit.

    Self-contained on purpose (this payload is copied into target repos and must
    not import anything outside the standard library). Resolution rules:

      dir   = $DEVKIT_FEEDBACK_DIR, else <home>/devkit-feedback
      home  = the devkit home: $DEVKIT_HOME, else $HOME/.devkit  (no $HOME: /nonexistent/.devkit)

    A value counts only when it is a non-empty absolute path with no control
    characters; one trailing slash is dropped (except for "/" itself). Anything
    else is skipped silently and the next rule applies.

    Trade-off: the path is fixed when the audit runs (on the machine where you
    run the audit), not when you paste the printed command.
    """
    def valid(v) -> bool:
        return (isinstance(v, str) and v.startswith("/")
                and not any(ord(c) < 32 or ord(c) == 127 for c in v))

    def norm(v: str) -> str:
        return v[:-1] if v != "/" and v.endswith("/") else v

    def join(root: str, child: str) -> str:
        return "/" + child if root == "/" else root + "/" + child

    home = os.environ.get("DEVKIT_HOME", "")
    home = norm(home) if valid(home) else join(os.environ.get("HOME") or "/nonexistent", ".devkit")
    fdir = os.environ.get("DEVKIT_FEEDBACK_DIR", "")
    fdir = norm(fdir) if valid(fdir) else join(home, "devkit-feedback")
    return fdir


def upstream_append_command(rel_path: str, reason: str) -> str:
    """A direct append to the feedback log. Injection boundary: the whole
    serialized JSON record is shlex-quoted; reason/path are never interpolated
    into shell syntax."""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {
        "ts": ts,
        "reason": reason,
        "path": rel_path,
    }
    quoted_json = shlex.quote(json.dumps(record))
    fdir = _feedback_dir()
    log = f"{fdir}/feedback.jsonl"
    return (f"mkdir -p {shlex.quote(fdir)} && printf '%s\\n' {quoted_json} "
            f">> {shlex.quote(log)}")


_UNPRINTABLE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def sanitize_for_row(s: str) -> str:
    """Neutralize control characters (newline included) in a value taken raw from
    the manifest's own `installs`/`files` structure (module name, managed path) —
    NOT ledger-validated, so nothing stops it from embedding a fake `upstream:` /
    `refresh:` line to spoof a copy-paste command. Printed only for display; never fed back into a command.

    Covers ASCII C0 controls + DEL, Unicode C1 controls (\\x80-\\x9f — includes
    NEL, U+0085, a line-break in some terminals/renderers), and the Unicode
    line/paragraph separators U+2028/U+2029 (a range limited to ASCII would miss them)."""
    return _UNPRINTABLE_RE.sub(lambda m: "\\u{:04x}".format(ord(m.group())), s)


def refresh_command(target: Path, rel_path: str, reason: str) -> str:
    """The pending-upstream STALE recovery — the prior reason pre-filled
    as a literal, single paste. Same injection boundary as upstream_append_command:
    every free-text field is shlex-quoted, never interpolated raw."""
    return (
        f"devkit-install.sh patch {shlex.quote(str(target))} {shlex.quote(rel_path)} "
        f"--reason {shlex.quote(reason)}"
    )


def check_drift(target: Path) -> int:
    manifest_path = target / ".devkit-manifest.json"
    if not manifest_path.exists():
        print(f"[audit] no .devkit-manifest.json in {target} — nothing devkit-managed here.")
        return 0

    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"[audit] ERROR: cannot read manifest: {e}", file=sys.stderr)
        return 1

    managed = collect_managed(manifest)
    ledger_by_path, ledger_errors = validate_ledger(manifest, managed)
    if ledger_errors:
        print(f"[audit] ERROR: malformed {LEDGER_KEY} in {manifest_path}:", file=sys.stderr)
        for e in ledger_errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    fail = False
    rows = []
    for install in manifest.get("installs", []):
        module = install.get("module", "?")
        for entry in install.get("files", []):
            rel = entry.get("path", "")
            recorded = entry.get("sha256", "")
            ledger_entry = ledger_by_path.get(rel)
            state, row_fail, note, label = classify_verdict(target / rel, recorded, ledger_entry)
            counts[state] = counts.get(state, 0) + 1
            fail = fail or row_fail
            rows.append((module, rel, state, note, label, ledger_entry))

    print(f"[audit] drift check: {target}")
    for module, rel, state, note, label, ledger_entry in rows:
        flag = "OK " if state in ("clean", "forked", "pending-upstream") else "!! "
        safe_module, safe_rel = sanitize_for_row(module), sanitize_for_row(rel)
        line = f"  {flag}{label:<24} {safe_module:<18} {safe_rel}"
        if note:
            line += f"  ({note})"
        print(line)
        if state == "pending-upstream" and ledger_entry:
            print(f"    upstream: {upstream_append_command(rel, ledger_entry['reason'])}")
        if state == "pending-upstream-stale" and ledger_entry:
            print(f"    refresh: {refresh_command(target, rel, ledger_entry['reason'])}")
    print("[audit] summary: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    return 2 if fail else 0


def _scan_enforcement_gaps(hooks_dir: Path, rules_dir: Path | None) -> None:
    enforces_re = re.compile(r"Enforces:\s*(.+)")
    rule_re = re.compile(r"rules/([A-Za-z0-9_.\-]+\.md)")  # matches any path ending in rules/<name>.md
    none_re = re.compile(r"^\s*none\b", re.IGNORECASE)
    enforced_rules = set()
    print(f"[audit] enforcement-gap scan: {hooks_dir}")
    for hook in sorted(hooks_dir.glob("*")):
        if hook.suffix not in (".sh", ".py"):
            continue
        try:
            head = "\n".join(hook.read_text(errors="ignore").splitlines()[:5])
        except OSError:
            continue
        m = enforces_re.search(head)
        if not m:
            print(f"  ?? {hook.name}: no `Enforces:` header")
            continue
        rest = m.group(1).strip()
        rule_m = rule_re.search(rest)
        if rule_m:
            rule_name = rule_m.group(1)
            enforced_rules.add(rule_name)
            if rules_dir is None or not (rules_dir / rule_name).exists():
                print(f"  !! {hook.name}: enforces rules/{rule_name} but that rule file is MISSING")
        elif none_re.match(rest):
            continue
        else:
            print(f"  ?? {hook.name}: Enforces header references no rule file ({rest[:48]})")
    if rules_dir is not None:
        for rule in sorted(rules_dir.glob("*.md")):
            if rule.name not in enforced_rules:
                print(f"  -- rule {rule.name}: no hook declares it (may be instruction-only)")


def check_enforcement_gaps(target: Path) -> None:
    """Scan each installed agent layout without mistaking Codex policy files for Markdown rules."""
    layouts = (
        (target / ".claude" / "hooks", target / ".claude" / "rules"),
        (target / ".codex" / "hooks", target / ".agents" / "ostler" / "rules"),
        (target / "hooks", target / "rules"),
    )
    for hooks_dir, rules_dir in layouts:
        if hooks_dir.is_dir():
            _scan_enforcement_gaps(hooks_dir, rules_dir if rules_dir.is_dir() else None)


def _default_target() -> Path:
    """No argument: audit the repo root, i.e. the directory holding the manifest.
    Tried in order: the current directory, the parent of this script's directory
    (the repo root for .claude/devkit-audit.py), this script's directory."""
    here = Path(__file__).resolve().parent
    for cand in (Path.cwd(), here.parent, here):
        if (cand / ".devkit-manifest.json").is_file():
            return cand
    return Path.cwd()


def main(argv: list[str]) -> int:
    target = Path(argv[1]).expanduser().resolve() if len(argv) > 1 else _default_target()
    if not target.is_dir():
        print(f"[audit] ERROR: not a directory: {target}", file=sys.stderr)
        return 1
    code = check_drift(target)
    check_enforcement_gaps(target)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
