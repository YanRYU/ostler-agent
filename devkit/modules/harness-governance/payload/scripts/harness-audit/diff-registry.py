#!/usr/bin/env python3
"""diff-registry.py: registry-vs-reality drift check of the harness-governance module.

Usage:
  python3 .claude/scripts/harness-audit/diff-registry.py SCAN_JSON REGISTRY_DIR

SCAN_JSON is the output of scan.sh; REGISTRY_DIR is the registry (default location:
.claude/harness-registry), one JSON file per domain plus the shipped _schema.json. Create the
registry once with registry.py init. Standard library only.

Compared per entry: name, artifact_type, wired_at, status, escape_hatch, enforces. `mechanism` is
extracted-then-curated: a mismatch is reported only when the registry entry has no
mechanism_override_reason. Deny and ask patterns may be split over several domain entries and are
compared as sets, not by id. When _schema.json is present, an entry with a domain, mechanism,
artifact_type or status outside it is a finding, and so is an id that appears twice. An entry whose
domain is still TODO-curate is counted in a note after the result; it is not drift. Artifacts listed
in _ignored.json (files of a module whose domain is module-only, such as a copy-and-rename skeleton)
are never entries and are skipped in both directions.

A `settings-unreadable` finding in the scan (a settings file that is not valid JSON) is printed as a
finding of its own, so unreadable settings never read as an empty harness.

Exit codes: 0 = clean, 1 = findings, 2 = unreadable or malformed scan or registry (a labeled
`registry-error:` line, never a traceback).
"""
import json
import sys
from pathlib import Path

EXTRACTED = ["name", "artifact_type", "wired_at", "status", "escape_hatch", "enforces"]
GROUPED = {"deny-rule": "settings-deny", "ask-rule": "settings-ask"}
UNCURATED = "TODO-curate"


class RegistryError(Exception):
    pass


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise RegistryError("registry-error: %s is not readable JSON (%s)" % (path.name, e))


def load_registry(reg_dir: Path):
    if not reg_dir.is_dir():
        raise RegistryError("registry-error: %s is not a directory (create the registry with registry.py init)"
                            % reg_dir)
    entries, metas = [], {}
    for f in sorted(reg_dir.glob("*.json")):
        if f.name in ("_schema.json", "_ignored.json"):
            continue
        doc = read_json(f)
        if not isinstance(doc, dict):
            raise RegistryError("registry-error: %s is not a mapping" % f.name)
        meta = doc.get("_meta") or {}
        if isinstance(meta, dict) and meta.get("domain"):
            metas[meta["domain"]] = meta
        for i, e in enumerate(doc.get("entries") or []):
            if not isinstance(e, dict) or not e.get("id"):
                raise RegistryError("registry-error: %s entry %d missing id" % (f.name, i))
            e["_file"] = f.name
            entries.append(e)
    return entries, metas


def load_ignored(reg_dir: Path, schema):
    """(ids to skip, findings). Only an artifact of a module-only domain may be ignored; an id listed
    with any other domain is a finding and is NOT skipped."""
    p = reg_dir / "_ignored.json"
    if not p.is_file():
        return set(), []
    doc = read_json(p)
    items = doc.get("ignored") if isinstance(doc, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, dict) and i.get("id") for i in items):
        raise RegistryError("registry-error: _ignored.json needs an `ignored` list of {id, reason} entries")
    module_only = set((schema or {}).get("module_only_domains") or {}) | {"template"}
    ok, bad = set(), []
    for i in items:
        if i.get("domain") in module_only:
            ok.add(i["id"])
        else:
            bad.append("ignored-non-template: %s is listed in _ignored.json with domain %r, which is not a "
                       "module-only domain (%s); only such files may be ignored"
                       % (i["id"], i.get("domain"), ", ".join(sorted(module_only))))
    return ok, bad


def duplicate_findings(reg_entries):
    seen = {}
    for e in reg_entries:
        seen.setdefault(e["id"], []).append(e["_file"])
    return ["duplicate-id: %s appears %d times (%s)" % (i, len(fs), ", ".join(fs))
            for i, fs in sorted(seen.items()) if len(fs) > 1]


def load_schema(reg_dir: Path):
    p = reg_dir / "_schema.json"
    if not p.is_file():
        return None
    s = read_json(p)
    if not isinstance(s, dict) or not isinstance(s.get("domains"), dict):
        raise RegistryError("registry-error: _schema.json has no domains mapping")
    return s


def schema_findings(reg_entries, schema):
    out = []
    if schema is None:
        return out
    domains = set(schema["domains"])
    mechs = set(schema.get("mechanisms") or [])
    types = set(schema.get("artifact_types") or [])
    statuses = set(schema.get("statuses") or [])
    for e in reg_entries:
        i, f = e["id"], e["_file"]
        if e.get("domain") not in domains and e.get("domain") != UNCURATED:
            out.append("invalid-domain: %s has domain %r, not one of the schema's domains (%s) (%s)"
                       % (i, e.get("domain"), ", ".join(sorted(domains)), f))
        if mechs and e.get("mechanism") not in mechs:
            out.append("invalid-mechanism: %s has mechanism %r, not one of %s (%s)"
                       % (i, e.get("mechanism"), ", ".join(sorted(mechs)), f))
        if types and e.get("artifact_type") not in types:
            out.append("invalid-artifact-type: %s has artifact_type %r (%s)" % (i, e.get("artifact_type"), f))
        if statuses and e.get("status") not in statuses:
            out.append("invalid-status: %s has status %r (%s)" % (i, e.get("status"), f))
    return out


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    scan_path, reg_dir = Path(argv[0]), Path(argv[1])
    try:
        scan = read_json(scan_path)
        if not isinstance(scan, dict):
            raise RegistryError("registry-error: %s is not a mapping" % scan_path.name)
        scan_entries = scan.get("entries") or []
        for i, e in enumerate(scan_entries):
            if not isinstance(e, dict) or not e.get("id"):
                raise RegistryError("registry-error: %s entry %d missing id" % (scan_path.name, i))
        reg_entries, metas = load_registry(reg_dir)
        schema = load_schema(reg_dir)
        ignored, ignored_findings = load_ignored(reg_dir, schema)
    except RegistryError as e:
        print(e)
        return 2

    findings = duplicate_findings(reg_entries) + ignored_findings + schema_findings(reg_entries, schema)
    for f_ in scan.get("findings") or []:
        if isinstance(f_, dict) and f_.get("check") == "settings-unreadable":
            findings.append("settings-unreadable: %s (%s)" % (f_.get("artifact"), f_.get("detail")))
    scan_by_id = {e["id"]: e for e in scan_entries if e["id"] not in ignored}
    reg_by_id = {e["id"]: e for e in reg_entries}

    for atype in GROUPED:
        scan_pats = {p for e in scan_entries if e.get("artifact_type") == atype for p in e.get("patterns") or []}
        reg_pats = {p for e in reg_entries if e.get("artifact_type") == atype for p in e.get("patterns") or []}
        for p in sorted(scan_pats - reg_pats):
            findings.append("pattern-not-in-registry: %s pattern %r exists in settings but no registry entry covers it" % (atype, p))
        for p in sorted(reg_pats - scan_pats):
            findings.append("pattern-not-in-reality: %s pattern %r in registry but absent from settings" % (atype, p))

    def grouped(e):
        return e.get("artifact_type") in GROUPED

    for sid, se in scan_by_id.items():
        if grouped(se):
            continue
        re_ = reg_by_id.get(sid)
        if re_ is None:
            findings.append("missing-from-registry: %s (%s) exists in reality, no registry entry" % (sid, se.get("name")))
            continue
        for f_ in EXTRACTED:
            if f_ in se and se.get(f_) != re_.get(f_):
                findings.append("drift: %s field %s: registry=%r reality=%r" % (sid, f_, re_.get(f_), se.get(f_)))
        if se.get("mechanism") != re_.get("mechanism") and not re_.get("mechanism_override_reason"):
            findings.append("drift: %s field mechanism: registry=%r scanner-proposed=%r (no mechanism_override_reason)"
                            % (sid, re_.get("mechanism"), se.get("mechanism")))

    for rid, re_ in reg_by_id.items():
        if grouped(re_) or rid in scan_by_id or re_.get("status") in ("planned", "deprecated"):
            continue
        findings.append("missing-from-reality: %s (%s) in registry (%s) but absent from scan" % (rid, re_.get("name"), re_["_file"]))

    for dom, meta in metas.items():
        if meta.get("not_applicable") or dom == UNCURATED:
            continue
        if not any(e.get("domain") == dom for e in reg_entries):
            findings.append("empty-domain: %s has no entries and is not marked not_applicable" % dom)

    for line in findings:
        print(line)
    if not findings:
        print("registry-vs-reality: CLEAN (0 diffs)")
    if ignored:
        print("ignored: %d (%s)" % (len(ignored), ", ".join(sorted(ignored))))
    todo = [e for e in reg_entries if e.get("domain") == UNCURATED]
    if todo:
        print("uncurated: %d entries still have domain %s (set their domain, origin and gaps in the registry files)"
              % (len(todo), UNCURATED))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
