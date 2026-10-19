#!/usr/bin/env python3
"""registry.py: seed the harness registry of a repo (harness-governance module).

Usage:
  python3 .claude/scripts/harness-audit/registry.py init [TARGET] [--registry DIR] [--scan FILE] [--force]

TARGET defaults to the current directory; the registry directory to <TARGET>/.claude/harness-registry.
`init` scans the target (scan.sh, or --scan FILE with a saved scan) and writes one JSON file per
domain. The extracted fields are copied from the scan. The curated `domain` of an artifact comes
from the module that installed it: the install manifest (<TARGET>/.devkit-manifest.json) records
each module's domain, its files and the permission patterns it merged into settings.json. An
artifact no module owns gets domain TODO-curate (collected in _uncurated.json) for the repo owner
to curate. A hook whose scanned mechanism differs from the mechanism its module declares keeps the
declared one, with a mechanism_override_reason.

Files of a module whose domain is module-only (a copy-and-rename skeleton, domain `template`) are
never registry entries: they are listed in _ignored.json with the reason, and the differ skips them.

Without --force an existing registry is never touched (exit 2). With --force the registry is
re-baselined: extracted fields are rewritten from reality, and what a person curated is kept for every
entry that still exists: a domain a person set (init records the domain it took from the module in
`domain_from_module`; any other non-TODO domain is a hand edit, and a domain init wrote follows the
module when the module's domain changes), an origin that init did not write itself, a
gaps note that init did not write itself, a mechanism_override_reason that init did not write itself
(with the mechanism it justifies), and a domain's _meta. What init wrote itself (an origin of the form
"installed by module ...", the "copied but not wired" gaps note, a "declared ... by module ..." override
reason) is recomputed from the manifest and the scan, so it never goes stale. Only files in the registry
directory are written. Standard library only.

A settings file that is not valid JSON (the scan's `settings-unreadable` finding) makes init refuse: a baseline
seeded from it would hold no permission entries, and the later audit would pass for the wrong reason.

Exit codes: 0 = written, 2 = refused or failed (the reason is printed).
"""
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

UNCURATED = "TODO-curate"
UNCURATED_FILE = "_uncurated.json"
IGNORED_FILE = "_ignored.json"
KINDS = {"deny": "deny-rule", "ask": "ask-rule"}
MODULE_ONLY_DEFAULT = {"template"}
# Wording init writes itself; a person's text never matches, so these are the fields --force recomputes.
GEN_ORIGIN = re.compile(r"^(installed by module \S+ \S+|merged by modules? .+)$")
GEN_GAP = re.compile(r"^copied by module \S+ but not wired")
GEN_REASON = re.compile(r"^declared \S+ by module \S+; the scanner proposes")


SAFE_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def safe_domain(d):
    """A domain names a registry file, so only a plain lowercase name (or TODO-curate) is kept."""
    return d if isinstance(d, str) and (d == UNCURATED or SAFE_DOMAIN.match(d)) else UNCURATED


def die(msg):
    print("registry-error: " + msg, file=sys.stderr)
    return 2


def read_json(path: Path):
    return json.loads(path.read_text())


def owner_index(target: Path):
    """{path: (module, version, domain, mechanism)} and {(kind, pattern): (module, domain)} from the manifest."""
    files, pats = {}, {}
    mf = target / ".devkit-manifest.json"
    if not mf.is_file():
        return files, pats
    man = read_json(mf)
    for inst in man.get("installs") or []:
        mod, ver, dom = inst.get("module"), inst.get("version"), safe_domain(inst.get("domain"))
        for f in inst.get("files") or []:
            files.setdefault(f["path"], (mod, ver, dom, f.get("mechanism")))
        for kinds in (inst.get("merged") or {}).values():
            for kind, plist in kinds.items():
                for p in plist:
                    pats.setdefault((kind, p), (mod, dom))
    return files, pats


def entry_paths(e):
    n = e.get("name", "")
    t = e.get("artifact_type")
    if t == "hook":
        return [".claude/hooks/" + n]
    if t in ("rule-file", "agent"):
        return [".claude/" + n]
    if t in ("skill", "script"):
        return [".claude/" + n.rstrip("/")]
    if t == "pointer":
        return ["CLAUDE.md", ".claude/CLAUDE.md"]
    return []


def find_owner(e, files):
    for p in entry_paths(e):
        if p in files:
            return files[p], True
        for fp, rec in files.items():  # a skill or script directory: any file under it
            if fp.startswith(p + "/"):
                return rec, False
    return None, False


def hand(origin):
    """An origin a person wrote: not empty, not TODO-curate, not in the form init generates."""
    return origin if origin and origin != UNCURATED and not GEN_ORIGIN.match(origin) else None


def curated_domain(e):
    """The domain of a prior entry when a person set it: init records the domain it computed from the
    module in `domain_from_module`, so a different (non-TODO) value is a hand edit. An entry init never
    wrote a module domain for (no such field) has a curated domain whenever it is not TODO-curate."""
    d = safe_domain(e.get("domain"))
    return d if d != UNCURATED and d != e.get("domain_from_module") else None


def slug(s):
    return s.lower().replace("_", "-")


def source_key(pe, kind):
    """A stable name for the prior entry a hand-curated pattern came from: the `curated_from` init recorded on
    its own earlier output, else the entry's id without the settings-<kind>- prefix. Stable across runs, so a
    second --force run rebuilds the same id."""
    k = pe.get("curated_from") or str(pe.get("id") or "entry")
    pre = "settings-%s-" % kind
    return slug(k[len(pre):] if k.startswith(pre) else k)


def main(argv):
    if not argv or argv[0] != "init":
        print(__doc__)
        return 2
    args = argv[1:]
    force, scan_arg, reg_arg, pos = False, None, None, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--force":
            force = True
        elif a in ("--scan", "--registry") and i + 1 < len(args):
            if a == "--scan":
                scan_arg = args[i + 1]
            else:
                reg_arg = args[i + 1]
            i += 1
        elif a.startswith("-"):
            return die("unknown or incomplete flag: " + a)
        else:
            pos.append(a)
        i += 1
    if len(pos) > 1:
        return die("at most one TARGET")
    target = Path(pos[0] if pos else ".").resolve()
    reg_dir = Path(reg_arg).resolve() if reg_arg else target / ".claude" / "harness-registry"
    if not target.is_dir():
        return die("target is not a directory: %s" % target)
    if not reg_arg:
        # the default registry must live inside the target: a symlinked .claude or harness-registry
        # would let --force delete (or init write) outside it
        link = next((p for p in (target / ".claude", reg_dir) if p.is_symlink()), None)
        try:
            real = reg_dir.resolve()
        except (RuntimeError, OSError):
            return die("the default registry path %s cannot be resolved (symlink loop?); nothing read or written. "
                       "Pass --registry DIR explicitly" % reg_dir)
        if link is not None or real != target and target not in real.parents:
            return die("the default registry %s resolves to %s, outside the target; nothing read or written. "
                       "Pass --registry DIR explicitly if that location is intended" % (reg_dir, real))

    old_files = (sorted(p for p in reg_dir.glob("*.json") if p.name not in ("_schema.json", IGNORED_FILE))
                 if reg_dir.is_dir() else [])
    old_ignored = reg_dir / IGNORED_FILE
    if (old_files or old_ignored.is_file()) and not force:
        return die("%s already holds %d registry file(s); nothing written. To re-baseline from reality "
                   "(curated fields are kept) run again with --force" % (reg_dir, len(old_files)))

    try:
        if scan_arg:
            scan = read_json(Path(scan_arg))
        else:
            r = subprocess.run(["bash", str(Path(__file__).resolve().parent / "scan.sh"), str(target)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                return die("scan failed (exit %d): %s" % (r.returncode, r.stderr.strip()))
            scan = json.loads(r.stdout)
        bad = [f for f in scan.get("findings") or []
               if isinstance(f, dict) and f.get("check") == "settings-unreadable"]
        if bad:
            return die("settings-unreadable: %s is not valid JSON, so its permissions cannot be baselined; "
                       "nothing written. Fix the file, then run init again"
                       % ", ".join(sorted({str(f.get("artifact")) for f in bad})))
        files, pats = owner_index(target)
        old_by_id, old_meta, old_pat = {}, {}, {}
        for f in old_files:
            doc = read_json(f)
            if isinstance(doc.get("_meta"), dict) and doc["_meta"].get("domain"):
                old_meta[doc["_meta"]["domain"]] = doc["_meta"]
            for e in doc.get("entries") or []:
                old_by_id[e.get("id")] = e
                if e.get("artifact_type") in KINDS.values():
                    for p in e.get("patterns") or []:
                        old_pat[(e["artifact_type"], p)] = e
    except (OSError, ValueError, KeyError) as e:
        return die("cannot read input: %s" % e)

    module_only = set(MODULE_ONLY_DEFAULT)
    if (reg_dir / "_schema.json").is_file():
        try:
            module_only |= set(read_json(reg_dir / "_schema.json").get("module_only_domains") or {})
        except (OSError, ValueError):
            pass

    seeded, ignored = [], []
    for se in scan.get("entries") or []:
        t = se.get("artifact_type")
        if t in KINDS.values():
            kind = "deny" if t == "deny-rule" else "ask"
            # (domain, group kind, source) -> [patterns, contributing modules, origin, gaps]; kind: "module" = the
            # module that owns the pattern and the chosen domain agree; "curated" = a person put the pattern in a
            # domain its module (if any) does not name; "todo" = no domain yet. source: the prior entry whose
            # hand-written origin or gaps note the patterns carry (curated groups only), so that text stays with
            # exactly the patterns it was written for; None = no hand text to protect
            groups = {}
            for p in se.get("patterns") or []:
                own = pats.get((kind, p))
                pe = old_pat.get((t, p), {})
                dom = curated_domain(pe) or (own[1] if own else UNCURATED)
                gk = "module" if own and own[1] == dom else ("curated" if dom != UNCURATED else "todo")
                text = hand(pe.get("origin")), [g for g in pe.get("gaps") or [] if not GEN_GAP.match(g)]
                src = source_key(pe, kind) if gk == "curated" and (text[0] or text[1]) else None
                g = groups.setdefault((dom, gk, src), [[], set(), text[0] if src else None, text[1] if src else []])
                g[0].append(p)
                if gk == "module":
                    g[1].add(own[0])
            for (dom, gk, src), (plist, mods, horigin, hgaps) in groups.items():
                # one entry per (kind, domain, group kind, source): ids are unique, and a hand-written origin or
                # gaps note stays with the patterns it was written for, never with a module's patterns
                oid = "settings-%s-%s%s" % (kind, slug(dom), "-curated" if gk == "curated" else "")
                if src:
                    oid += "-" + src
                prior = old_by_id.get(oid, {})
                origin = (UNCURATED if gk != "module" else
                          "merged by module%s %s" % ("s" if len(mods) > 1 else "", ", ".join(sorted(mods))))
                origin = hand(prior.get("origin")) or horigin or origin
                entry = {"id": oid, "name": "settings.json %s list (%s, %d patterns)" % (kind, dom, len(plist)),
                         "artifact_type": t, "mechanism": se["mechanism"], "wired_at": se["wired_at"],
                         "status": se["status"], "patterns": plist, "domain": dom, "origin": origin,
                         "gaps": list(hgaps if src else prior.get("gaps") or [])}
                if src:
                    entry["curated_from"] = src
                if gk == "module":
                    entry["domain_from_module"] = dom
                seeded.append(entry)
            continue
        e = dict(se)
        prior = old_by_id.get(e["id"], {})
        rec, exact = find_owner(e, files)
        if rec and rec[2] in module_only:
            ignored.append({"id": e["id"], "name": e.get("name"), "module": rec[0], "domain": rec[2],
                            "reason": "installed by module %s, whose domain %s is module-only; "
                                      "such files are never registry entries" % (rec[0], rec[2])})
            continue
        gaps = []
        if rec:
            mod, ver, dom, mech = rec
            e["domain"], e["origin"] = dom, "installed by module %s %s" % (mod, ver)
            e["domain_from_module"] = dom
            if exact and mech and mech != e["mechanism"]:
                e["mechanism"] = mech
                e["mechanism_override_reason"] = ("declared %s by module %s; the scanner proposes %s from a static scan"
                                                  % (mech, mod, se["mechanism"]))
            if e["artifact_type"] == "hook" and e["status"] == "unwired":
                gaps.append("copied by module %s but not wired: add its hook entry to the settings file "
                            "(see the module's Procedure)" % mod)
        else:
            e["domain"], e["origin"] = UNCURATED, UNCURATED
        if prior:  # --force: what a person curated wins; what init generated is recomputed above
            if curated_domain(prior):
                e["domain"] = curated_domain(prior)
            if prior.get("origin") and prior["origin"] != UNCURATED and not GEN_ORIGIN.match(prior["origin"]):
                e["origin"] = prior["origin"]
            gaps += [g for g in prior.get("gaps") or [] if not GEN_GAP.match(g)]
            reason = prior.get("mechanism_override_reason")
            if reason and not GEN_REASON.match(reason) and prior.get("mechanism"):
                e["mechanism"], e["mechanism_override_reason"] = prior["mechanism"], reason
        e["gaps"] = gaps
        seeded.append(e)

    ids = [e["id"] for e in seeded]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        return die("internal: duplicate registry ids %s; nothing written" % ", ".join(dup))

    schema = {}
    if (reg_dir / "_schema.json").is_file():
        try:
            schema = read_json(reg_dir / "_schema.json").get("domains") or {}
        except (OSError, ValueError):
            schema = {}
    by_dom = {}
    for e in seeded:
        e["domain"] = safe_domain(e.get("domain"))
        by_dom.setdefault(e["domain"], []).append(e)
    today = datetime.date.today().isoformat()
    # build every output first, so a failure here leaves the old registry untouched
    outputs = {}
    for dom, ents in sorted(by_dom.items()):
        meta = dict(old_meta.get(dom) or {})
        meta.update({"domain": dom, "letter": (schema.get(dom) or {}).get("letter", "-")})
        meta.setdefault("last_curated", today if dom != UNCURATED else UNCURATED)
        meta.setdefault("not_applicable", False)
        name = UNCURATED_FILE if dom == UNCURATED else dom + ".json"
        outputs[name] = json.dumps({"_meta": meta, "entries": ents}, indent=2, ensure_ascii=False) + "\n"
    if ignored:
        outputs[IGNORED_FILE] = json.dumps({"ignored": ignored}, indent=2, ensure_ascii=False) + "\n"
    reg_real = reg_dir.resolve()
    for name in outputs:
        if (reg_dir / name).resolve().parent != reg_real:
            return die("internal: %s would be written outside %s; nothing written" % (name, reg_dir))
    reg_dir.mkdir(parents=True, exist_ok=True)
    for f in old_files:
        f.unlink()
    if old_ignored.is_file():
        old_ignored.unlink()
    for name, text in outputs.items():
        (reg_dir / name).write_text(text)
    todo = len(by_dom.get(UNCURATED, []))
    print("registry init: seeded %d entries into %d files under %s (%d need curation: domain %s)"
          % (len(seeded), len(by_dom), reg_dir, todo, UNCURATED))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
