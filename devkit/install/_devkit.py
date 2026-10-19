#!/usr/bin/env python3
"""_devkit — implementation behind devkit-install.sh (the single installer surface).

Subcommands:
  install <target> [module ...] [--force] [--allow-dirty]
                                   copy module payloads into target, stamp manifest
  check   <target>                delegate to the target/audit drift checker
  patch   <target> <path> --reason "<why>"
                                   record a downstream edit as pending-upstream
                                   in the manifest ledger (see the Patch ledger section of devkit/README.md)
  outdated <target>               one line per installed module whose source changed
  remove-module <target> <name> [--force]
                                   remove a module's clean files and its record
  recover <target>                finish or roll back an interrupted transaction
  update  <target> [module ...] [--force] [--allow-dirty]
                                   install over an existing manifest; migrates 1.x records to 2.0
  readme [--check]                regenerate (or verify) the module table in devkit/README.md
  domains [--check]               regenerate (or verify) devkit/domains.json and the harness-governance
                                   module's _schema.json from a registry schema file (harness-registry/_schema.yaml
                                   beside devkit/, or the path in DEVKIT_REGISTRY_SCHEMA)

Factory-side tool (runs wherever the devkit checkout lives); may use PyYAML. The audit payload it
installs is stdlib-only so targets stay self-contained.

Every mutation goes through install/lib/txn.py (the transaction library): lock -> recover -> full preflight -> journaled apply -> manifest
rename as the single commit point. A refusal therefore writes NOTHING: the plan
is first built READ-ONLY under a shared lock (which never creates the lock file),
and only a plan that will actually commit creates the target directory and takes
the exclusive lock; that plan is then rebuilt under the lock, so what is applied
is what was verified. Each write/delete carries `Op.expect` (the state the
planner saw), so an edit landing between planning and applying is refused rather
than overwritten. `check` and `outdated` hold the shared lock, so they never read
a half-applied tree. devkit assumes it manages every path a module payload names;
do not point other tools at those paths.

Install safety (copy-on-build, no silent clobber):
  - new file              -> write
  - devkit-managed, clean -> overwrite (update)
  - modified-downstream   -> REFUSE without --force (report)
  - pre-existing, unmanaged -> REFUSE without --force (don't clobber hand-tuned files)
  - fork marker present   -> SKIP (target owns it)
  - dest escaping the target (.., absolute, symlinked dir) -> REFUSE (exit 8)
  - two dests that differ only by case or Unicode normalization (one file on a
    case-insensitive disk), or a dest colliding that way with a file already
    recorded in the manifest, or with installer metadata -> REFUSE (exit 8)
  - target is the factory checkout, inside it, or contains it -> REFUSE (exit 8)
  - file dropped from a selected module upstream -> pruned when clean
    (deselecting a whole module never prunes; use remove-module)
  - dirty factory checkout -> REFUSE (exit 7) unless --allow-dirty

Exit codes: 0 ok · 1 usage/internal · 3 refusals (zero writes) · 4 LOCKED ·
6 pending transaction (check/outdated) · 7 dirty source · 8 validation/confinement.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml  # PyYAML — factory-side only

sys.path.insert(0, str(Path(__file__).resolve().parent))
import modtable  # noqa: E402  stdlib-only renderer (loadable without PyYAML)

FORK_MARKER = "devkit:" + "forked"  # split so installer source isn't self-flagged
FACTORY = Path(__file__).resolve().parent.parent          # .../devkit
MODULES_DIR = FACTORY / "modules"
AUDIT_SCRIPT = MODULES_DIR / "audit" / "payload" / "devkit-audit.py"

# Transaction library (install/lib/txn.py), found relative to this file:
# <repo>/devkit/install/_devkit.py -> <repo>/install/lib
sys.path.insert(0, str(FACTORY.parent / "install" / "lib"))
try:
    import txn  # noqa: E402
except ImportError as _e:  # pragma: no cover - only in a broken checkout
    sys.stderr.write(f"[devkit] ERROR: library install/lib/txn.py not found ({_e})\n")
    raise SystemExit(1)

MANIFEST_NAME = ".devkit-manifest.json"
TXN_DIRNAME = ".devkit-txn"
LOCKNAME = ".devkit-install.lock"
INSTALLER_ID = "devkit-install/2"
KIT_VERSION = "2.0.0"

# 1.x module name -> the 2.0 modules that replaced it. An alias applies only to a name that is NOT a
# module in this factory, is expanded before dependency ordering, and prints one deprecation line per
# alias used. Removal version: the first minor release after 2.0.
ALIASES: dict[str, list[str]] = {
    "settings-baseline": ["system-safety", "secrets-privacy"],
    "hooks-core": ["git-multi-session", "secrets-privacy", "observe"],
    "rules-core": ["git-multi-session", "secrets-privacy", "work-craft"],
}
ALIAS_REMOVED_IN = "2.1.0"
RESERVED = {MANIFEST_NAME, TXN_DIRNAME, LOCKNAME}


def _norm(rel: str) -> str:
    """Comparison key for a relative path: NFC + casefold. Two dests with the same
    key are the same file on a case-insensitive or normalization-insensitive disk."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", rel).casefold())


_RESERVED_KEYS = {_norm(MANIFEST_NAME), _norm(LOCKNAME)}
_TXN_KEY = _norm(TXN_DIRNAME)


def _is_reserved(dest_rel: str) -> bool:
    key = _norm(dest_rel)
    return key in _RESERVED_KEYS or key.split("/", 1)[0] == _TXN_KEY


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _guard_target(target: Path) -> None:
    """Refuse a target that is the factory checkout, lies inside it, or contains it.

    Compared by file identity (device + inode), never by path text: on a
    case-insensitive disk a differently-cased spelling of the same directory
    passes any string comparison. Installing into the checkout the payloads come
    from would overwrite them, and a later remove-module would delete them."""
    root = FACTORY.parent
    for anc in (root, *root.parents):          # target is, or contains, the checkout
        if _same_file(anc, target):
            raise txn.ConfineError(f"target {target} is (or contains) the devkit factory checkout {root}")
    cur = target
    while True:                                # target is inside the checkout
        if _same_file(cur, root):
            raise txn.ConfineError(f"target {target} is inside the devkit factory checkout {root}")
        if cur.parent == cur:
            break
        cur = cur.parent


def sha256(path: Path) -> str:
    return txn.sha256_file(path)


# MODULE.md contract v2 -----------------------------------------------------------------------
# Frontmatter `domain:` (one module = one domain), each payload entry carries `mechanism:`,
# `layer:` is gone, and the body has the three fixed sections below (the board is rendered
# from them). The domain / mechanism vocabulary lives in devkit/domains.json, which is
# GENERATED by `_devkit.py domains`; edit through that command, not by hand. It is a JSON copy
# on purpose: the installer and any stdlib-only reader must read the vocabulary without PyYAML
# and without a schema file; when DEVKIT_REGISTRY_SCHEMA points at a schema, a devkit test asserts
# the file equals it, so there is still exactly one source of truth.
DOMAINS_FILE = FACTORY / "domains.json"
# Optional input, read only by `_devkit.py domains` (install never needs it): harness-registry/_schema.yaml
# beside devkit/ when present. Point DEVKIT_REGISTRY_SCHEMA at a schema file to regenerate from another one.
SCHEMA_FILE = Path(os.environ.get("DEVKIT_REGISTRY_SCHEMA") or FACTORY.parent / "harness-registry" / "_schema.yaml")
# The registry schema the harness-governance module ships into a target, generated from the SAME
# schema file as domains.json (one source: the two JSON copies are projections of it).
SCHEMA_PAYLOAD_FILE = (MODULES_DIR / "harness-governance" / "payload" / "harness-registry" / "_schema.json")
SECTIONS = ("Prevents", "Procedure", "State")


def load_vocab() -> tuple[list[str], list[str]]:
    data = json.loads(DOMAINS_FILE.read_text())
    return list(data["domains"]), list(data["mechanisms"])


def validate_module(meta: dict, domains: list[str], mechanisms: list[str]) -> list[str]:
    """Named v2 contract errors of one parsed module (empty list = valid)."""
    name = meta.get("name", "?")
    errs: list[str] = []
    if "layer" in meta:
        errs.append(f"module {name}: `layer:` was removed in contract v2; use `domain:` and a per-file `mechanism:`")
    dom = meta.get("domain")
    if dom is None:
        errs.append(f"module {name}: missing `domain:` (one of {domains})")
    elif dom not in domains:
        errs.append(f"module {name}: unknown domain {dom!r} (known: {domains})")
    for i, entry in enumerate(meta.get("payload") or []):
        if not isinstance(entry, dict):
            errs.append(f"module {name}: payload entry #{i} is not a mapping")
            continue
        mech = entry.get("mechanism")
        where = entry.get("dest", f"#{i}")
        if mech is None:
            errs.append(f"module {name}: payload {where}: missing `mechanism:` (one of {mechanisms})")
        elif mech not in mechanisms:
            errs.append(f"module {name}: payload {where}: unknown mechanism {mech!r} (known: {mechanisms})")
    secs = meta.get("_sections") or {}
    for h in SECTIONS:
        if not secs.get(h):
            errs.append(f"module {name}: MODULE.md body needs a non-empty `## {h}` section")
    return errs


def parse_module(module_md: Path) -> dict:
    text = module_md.read_text()
    if not text.startswith("---"):
        raise ValueError(f"{module_md}: no frontmatter")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm)
    meta["_dir"] = module_md.parent
    meta["_sections"] = modtable.extract_sections(body)
    return meta


def discover_modules() -> dict[str, dict]:
    """All modules of the factory. A module that breaks the v2 contract is still listed, with
    its named problems in `_errors`; install / readme refuse it (exit 8) before writing."""
    try:
        domains, mechanisms = load_vocab()
        vocab_err = None
    except (OSError, ValueError, KeyError) as e:
        domains, mechanisms, vocab_err = [], [], f"cannot read {DOMAINS_FILE}: {e}"
    mods = {}
    for md in sorted(MODULES_DIR.glob("*/MODULE.md")):
        meta = parse_module(md)
        meta["_errors"] = [vocab_err] if vocab_err else validate_module(meta, domains, mechanisms)
        mods[meta["name"]] = meta
    return mods


def is_alias(name: str, mods: dict[str, dict]) -> bool:
    return name in ALIASES and name not in mods


def expand_aliases(names: list[str], mods: dict[str, dict], announce: bool = True) -> list[str]:
    """Replace each 1.x module name by the 2.0 modules it maps to (order kept, duplicates dropped),
    printing exactly one deprecation line per alias used."""
    out: list[str] = []
    said: set[str] = set()
    for n in names:
        if is_alias(n, mods):
            if announce and n not in said:
                said.add(n)
                print(f"[devkit] DEPRECATED: module '{n}' was split in 2.0 into {', '.join(ALIASES[n])}; "
                      f"the old name stops working in {ALIAS_REMOVED_IN}")
            new = ALIASES[n]
        else:
            new = [n]
        out.extend(x for x in new if x not in out)
    return out


def topo_order(selected: list[str], mods: dict[str, dict]) -> list[str]:
    """Order so dependencies install before dependents. Warn on unmet deps."""
    ordered: list[str] = []
    seen: set[str] = set()
    sel = set(selected)

    def visit(name: str, stack: tuple[str, ...]) -> None:
        if name in seen:
            return
        if name in stack:
            print(f"[devkit] WARN: dependency cycle at {name} -> skipping cycle edge", file=sys.stderr)
            return
        for dep in expand_aliases(mods[name].get("depends-on") or [], mods, announce=False):
            if dep not in mods:
                print(f"[devkit] WARN: {name} depends-on '{dep}' which is not a known module", file=sys.stderr)
                continue
            if dep not in sel:
                print(f"[devkit] WARN: {name} depends-on '{dep}' which is not in this install set "
                      f"(install it too, or its rule references will dangle)", file=sys.stderr)
            visit(dep, stack + (name,))
        seen.add(name)
        ordered.append(name)

    for n in selected:
        visit(n, ())
    return [n for n in ordered if n in sel]


def load_manifest(target: Path) -> dict:
    mpath = target / MANIFEST_NAME
    if mpath.exists():
        return json.loads(mpath.read_text())
    return {"schema_version": 1, "source_commit": None, "installs": []}


def recorded_sha(manifest: dict, dest_rel: str) -> str | None:
    for inst in manifest.get("installs", []):
        for f in inst.get("files", []):
            if f.get("path") == dest_rel:
                return f.get("sha256")
    return None


def find_prior_record(manifest: dict, dest_rel: str) -> dict | None:
    for inst in manifest.get("installs", []):
        for f in inst.get("files", []):
            if f.get("path") == dest_rel:
                return dict(f)
    return None


def merge_json_permissions(existing: dict, incoming: dict) -> dict:
    """Union deny/ask lists; keep existing allow/additionalDirectories/defaultMode."""
    out = dict(existing)
    ep = dict(out.get("permissions", {}))
    ip = incoming.get("permissions", {})
    for key in ("deny", "ask"):
        merged = list(ep.get(key, []))
        for item in ip.get(key, []):
            if item not in merged:
                merged.append(item)
        ep[key] = merged
    for key in ("allow", "additionalDirectories"):
        if key not in ep:
            ep[key] = list(ip.get(key, []))
    if "defaultMode" not in ep and "defaultMode" in ip:
        ep["defaultMode"] = ip["defaultMode"]
    out["permissions"] = ep
    return out


PERM_KINDS = ("deny", "ask")


def _owned_patterns(installs: list[dict], dest_rel: str) -> dict[str, set[str]]:
    """Patterns that some installed module recorded as its contribution to a merged file."""
    owned: dict[str, set[str]] = {k: set() for k in PERM_KINDS}
    for inst in installs:
        rec = (inst.get("merged") or {}).get(dest_rel) or {}
        for k in PERM_KINDS:
            owned[k].update(p for p in rec.get(k, []) if isinstance(p, str))
    return owned


def _fragment_patterns(module_names: list[str], dest_rel: str, mods: dict[str, dict]) -> dict[str, list[str]]:
    """Union of the deny/ask patterns the named modules' merge fragments ship for `dest_rel`."""
    out: dict[str, list[str]] = {k: [] for k in PERM_KINDS}
    for m in module_names:
        for entry in mods[m].get("payload") or []:
            if entry.get("dest") == dest_rel and entry.get("mode") == "merge":
                try:
                    perms = json.loads((mods[m]["_dir"] / "payload" / entry["src"]).read_text()).get("permissions") or {}
                except (OSError, ValueError):
                    continue
                for k in PERM_KINDS:
                    out[k].extend(p for p in perms.get(k, []) if p not in out[k])
    return out


def contributed_patterns(before: dict, incoming: dict, owned: dict[str, set[str]]) -> dict[str, list[str]]:
    """The fragment patterns a module owns after merging: every pattern that was not already in the
    file, plus every pattern another module (or this module's earlier install) already recorded.
    A pattern that was in the file before any module touched it belongs to the user and is never
    recorded, so `remove-module` can never take it out."""
    have = (before.get("permissions") or {})
    out: dict[str, list[str]] = {}
    for k in PERM_KINDS:
        present = set(have.get(k, []))
        out[k] = [p for p in (incoming.get("permissions") or {}).get(k, [])
                  if p not in present or p in owned[k]]
    return out


def _kept_line(dest_rel: str, unc: dict[str, list[str]]) -> str:
    pats = [p for k in PERM_KINDS for p in unc.get(k, [])]
    return (f"  kept   {len(pats)} pattern(s) in {dest_rel} that predate ownership tracking "
            f"(they may be yours; delete by hand if unwanted): {', '.join(pats)}")


def _safe_read(p: Path) -> str:
    try:
        return p.read_text(errors="ignore")
    except OSError:
        return ""


def _is_forked(dest: Path, state: dict) -> bool:
    return state["type"] == "file" and FORK_MARKER in _safe_read(dest)


def make_txn(target: Path) -> "txn.Txn":
    return txn.Txn(target, manifest_name=MANIFEST_NAME, txn_dirname=TXN_DIRNAME,
                   lockname=LOCKNAME)


def _recovered(tx: "txn.Txn") -> None:
    for line in tx.recover():
        print(f"[devkit] {line}")


# ---------------------------------------------------------------------------
# source state, fingerprints
# ---------------------------------------------------------------------------
def source_state(module_names: list[str], mods: dict[str, dict]) -> tuple[str | None, bool]:
    """(HEAD commit or None, dirty) for the payload sources of these modules.

    dirty = `git status --porcelain --untracked-files=all` over the modules'
    directories is non-empty, or the factory is not a git checkout at all."""
    def git(*a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(FACTORY), *a], capture_output=True,
                              text=True, timeout=30)
    try:
        head = git("rev-parse", "HEAD")
        commit = head.stdout.strip() if head.returncode == 0 else ""
        if not commit:
            return None, True
        paths = [str(mods[n]["_dir"]) for n in module_names if n in mods]
        st = git("status", "--porcelain", "--untracked-files=all", "--", *paths)
        if st.returncode != 0:
            return commit, True
        return commit, bool(st.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return None, True


def _script_mode(src: Path) -> str:
    return "0755" if src.suffix in (".sh", ".py") else "0644"


def module_rows(meta: dict) -> list[dict]:
    """Source-derived fingerprint rows of a module (independent of any target)."""
    rows = []
    for entry in meta.get("payload", []):
        src = meta["_dir"] / "payload" / entry["src"]
        if not src.is_file():
            raise FileNotFoundError(str(src))
        merge = entry.get("mode", "overwrite") == "merge"
        rows.append({"dest": entry["dest"], "sha256": sha256(src), "type": "file",
                     "mode": "merge" if merge else _script_mode(src)})
    return rows


def aggregate_fingerprint(installs: list[dict]) -> str:
    return txn.fingerprint([{"dest": "module:" + i.get("module", "?"),
                             "sha256": i.get("fingerprint"), "type": "module", "mode": None}
                            for i in installs])


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
class Plan:
    def __init__(self) -> None:
        self.ops: list = []
        self.lines: list[str] = []          # informational, printed after success
        self.conflicts: list[str] = []      # REFUSE lines
        self.errors: list[str] = []         # validation errors (exit 8)
        self.dropped: set[str] = set()      # paths that leave the manifest


def plan_prune(target: Path, records: list[dict], force: bool, plan: Plan, why: str) -> None:
    """Decide what to do with previously-installed records that are no longer wanted."""
    for rec in records:
        rel = rec.get("path", "")
        try:
            dest = txn.confine(target, rel)
        except txn.InstallError as e:
            plan.errors.append(f"manifest path {rel!r}: {e}")
            continue
        if rec.get("mode") == "merge":
            plan.lines.append(f"  drop   merge-file-kept  {rel} (a merged file is never deleted; left in place)")
            plan.dropped.add(rel)
            continue
        st = txn.lstate(dest)
        if st["type"] == "absent":
            plan.lines.append(f"  drop   already-gone     {rel}")
            plan.dropped.add(rel)
        elif _is_forked(dest, st):
            plan.lines.append(f"  drop   forked-kept      {rel} (target owns it)")
            plan.dropped.add(rel)
        elif st["type"] != "file":
            plan.conflicts.append(f"  REFUSE type-drift {rel} (found a {st['type']}, cannot remove)")
        elif st["sha256"] == rec.get("sha256") or force:
            plan.ops.append(txn.Op("delete", rel, expect=st))
            plan.lines.append(f"  prune  {why:<15} {rel}")
            plan.dropped.add(rel)
        else:
            plan.conflicts.append(f"  REFUSE modified-downstream {rel} ({why}; use --force)")


def plan_unmerge(target: Path, inst: dict, rest: list[dict], plan: Plan, force: bool = False) -> dict[str, str]:
    """Un-merge the permission patterns this module contributed to each merged file.

    Removes exactly the patterns recorded in `inst["merged"][dest]`, except a pattern another
    remaining module also recorded; the user's own entries (and every `allow`) are never in
    the record, so they are never touched. A record from before fragments existed has no
    `merged` info and keeps the old behavior (the file is left in place; see plan_prune). Patterns
    in `legacy_unclaimed` (fragment patterns that predate ownership tracking and may be the user's)
    are never removed; a line names them.
    Returns {dest: sha256 of the file after the un-merge} for the files handled here."""
    done: dict[str, str] = {}
    for dest_rel, mine in (inst.get("merged") or {}).items():
        if inst.get("merged_synthetic"):
            mine = {}                 # a retained 1.x record's stand-in ownership: protect, never delete
        if not any(f.get("path") == dest_rel and f.get("mode") == "merge" for f in inst.get("files", [])):
            continue
        try:
            dest = txn.confine(target, dest_rel)
        except txn.InstallError as e:
            plan.errors.append(f"manifest path {dest_rel!r}: {e}")
            continue
        st = txn.lstate(dest)
        if st["type"] == "absent":
            plan.lines.append(f"  drop   already-gone     {dest_rel}")
            plan.dropped.add(dest_rel)
            done[dest_rel] = ""
            continue
        def keep_or_refuse(reason: str) -> None:
            if force:
                plan.lines.append(f"  drop   merge-file-kept  {dest_rel} ({reason}; left untouched)")
                plan.dropped.add(dest_rel)
                done[dest_rel] = st.get("sha256") or ""
            else:
                plan.conflicts.append(f"  REFUSE {reason.split(':')[0]} {dest_rel} ({reason}; use --force)")

        if st["type"] != "file":
            keep_or_refuse(f"type-drift: found a {st['type']}, cannot un-merge")
            continue
        try:
            data = json.loads(dest.read_text())
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (ValueError, OSError) as e:
            keep_or_refuse(f"unparseable-json: {e}")
            continue
        keep = _owned_patterns(rest, dest_rel)
        perms = data.get("permissions")
        removed = 0
        if isinstance(perms, dict):
            for k in PERM_KINDS:
                drop = {p for p in mine.get(k, []) if p not in keep[k]}
                if drop and isinstance(perms.get(k), list):
                    before = len(perms[k])
                    perms[k] = [p for p in perms[k] if p not in drop]
                    removed += before - len(perms[k])
        out = (json.dumps(data, indent=2) + "\n").encode()
        new_sha = txn.sha256_bytes(out)
        if new_sha != st["sha256"]:
            plan.ops.append(txn.Op("write", dest_rel, out, None, st))
        plan.lines.append(f"  unmerge permissions     {dest_rel} ({removed} pattern(s) removed)")
        unc = (inst.get("legacy_unclaimed") or {}).get(dest_rel) or {}
        if any(unc.get(k) for k in PERM_KINDS):
            plan.lines.append(_kept_line(dest_rel, unc))
        plan.dropped.add(dest_rel)
        done[dest_rel] = new_sha
    return done


def build_install_plan(target: Path, order: list[str], mods: dict[str, dict],
                       prior: dict, force: bool) -> tuple[Plan, list[dict]]:
    plan = Plan()
    new_installs: dict[str, dict] = {}
    seen_dests: dict[str, str] = {}
    seen_keys: dict[str, str] = {}          # _norm(dest) -> dest
    seen_modes: dict[str, str] = {}         # dest -> mode of its first claim
    merge_state: dict[str, dict] = {}       # merged dest -> {"data", "st"}: the cumulative merge
    merged_rec_all: dict[str, dict] = {}    # module -> its contributed patterns this plan
    prior_keys: dict[str, set[str]] = {}    # _norm(recorded path) -> {recorded paths}
    for inst in prior.get("installs", []):
        for f in inst.get("files", []):
            if isinstance(f.get("path"), str):
                prior_keys.setdefault(_norm(f["path"]), set()).add(f["path"])
    prior_by_module = {i.get("module"): i for i in prior.get("installs", [])}
    for name in order:
        meta = mods[name]
        files_record = []
        merged_rec: dict[str, dict] = {}
        legacy_unc: dict[str, dict[str, list[str]]] = {}
        module_dests: set[str] = set()
        rows = []
        for entry in meta.get("payload", []):
            dest_rel, mode = entry.get("dest"), entry.get("mode", "overwrite")
            try:
                src = txn.confine(meta["_dir"] / "payload", entry["src"])
            except (txn.InstallError, KeyError) as e:
                plan.errors.append(f"{name}: payload src {entry.get('src')!r}: {e}")
                continue
            if not src.is_file() or src.is_symlink():
                plan.errors.append(f"{name}: payload source missing: {src}")
                continue
            try:
                dest = txn.confine(target, dest_rel)
            except txn.InstallError as e:
                plan.errors.append(f"{name}: dest {dest_rel!r}: {e}")
                continue
            if _is_reserved(dest_rel):
                plan.errors.append(f"{name}: dest {dest_rel!r} is installer metadata")
                continue
            # Several modules may each `merge` a fragment into one dest (settings.json); any other
            # second claim on a dest is still refused.
            shared_merge = (dest_rel in seen_dests and mode == "merge"
                            and seen_modes.get(dest_rel) == "merge" and dest_rel not in module_dests)
            if dest_rel in seen_dests and not shared_merge:
                plan.errors.append(f"{name}: dest {dest_rel!r} already claimed by {seen_dests[dest_rel]}")
                continue
            key = _norm(dest_rel)
            if key in seen_keys and not shared_merge:
                plan.errors.append(
                    f"{name}: dest {dest_rel!r} collides with {seen_keys[key]!r} "
                    f"(same file modulo case / Unicode normalization)")
                continue
            clash = sorted(p for p in prior_keys.get(key, ()) if p != dest_rel)
            if clash:
                plan.errors.append(
                    f"{name}: dest {dest_rel!r} collides with the previously installed "
                    f"{clash[0]!r} (same file modulo case / Unicode normalization)")
                continue
            module_dests.add(dest_rel)           # a module may claim a dest at most once
            seen_dests.setdefault(dest_rel, name)
            seen_modes.setdefault(dest_rel, mode)
            seen_keys[key] = dest_rel
            if mode not in ("overwrite", "merge") or (mode == "merge" and not dest_rel.endswith(".json")):
                plan.errors.append(f"{name}: {dest_rel}: unsupported mode {mode!r}")
                continue

            src_bytes = src.read_bytes()
            src_sha = txn.sha256_bytes(src_bytes)
            merge = mode == "merge"
            want_mode = _script_mode(src)
            rows.append({"dest": dest_rel, "sha256": src_sha, "type": "file",
                         "mode": "merge" if merge else want_mode})
            st = txn.lstate(dest)

            if _is_forked(dest, st):
                plan.lines.append(f"  skip   forked          {dest_rel}")
                # A forked file can still carry a pending-upstream
                # ledger entry, so its prior manifest record survives a skip.
                prior_rec = find_prior_record(prior, dest_rel)
                if prior_rec is not None:
                    prior_rec["mechanism"] = entry.get("mechanism")
                    files_record.append(prior_rec)
                continue

            if merge:
                if st["type"] not in ("file", "absent"):
                    plan.conflicts.append(f"  REFUSE type-drift {dest_rel} (found a {st['type']})")
                    continue
                try:
                    if dest_rel not in merge_state:
                        existing = json.loads(dest.read_text()) if st["type"] == "file" else {}
                        if not isinstance(existing, dict):
                            raise ValueError("not a JSON object")
                        merge_state[dest_rel] = {"data": existing, "st": st}
                    incoming = json.loads(src_bytes)
                    if not isinstance(incoming, dict):
                        raise ValueError("not a JSON object")
                except (ValueError, OSError) as e:
                    plan.conflicts.append(f"  REFUSE unparseable-json {dest_rel} ({e})")
                    continue
                ms = merge_state[dest_rel]
                # a retained 1.x record's stand-in ownership (merged_synthetic) protects but never attributes
                owned = _owned_patterns([i for i in prior.get("installs", []) if not i.get("merged_synthetic")],
                                        dest_rel)
                for other in merged_rec_all.values():
                    for k, v in (other.get(dest_rel) or {}).items():
                        owned[k].update(v)
                prior_inst = prior_by_module.get(name)
                legacy_owner = prior_inst is None and any(
                    is_alias(n, mods) and ("merged" not in i or i.get("merged_synthetic"))
                    and any(f.get("path") == dest_rel and f.get("mode") == "merge" for f in i.get("files", []))
                    for n, i in prior_by_module.items())
                contrib = contributed_patterns(ms["data"], incoming, owned)
                merged_rec[dest_rel] = contrib
                if legacy_owner or (prior_inst is not None and ("merged" not in prior_inst
                                                         or prior_inst.get("merged_synthetic"))):
                    # A record written before ownership was tracked. This module owns only what the
                    # normal rule gives it; a fragment pattern that was already in the file and that no
                    # recorded module owns may be the user's own, so it is recorded as `legacy_unclaimed`
                    # (never in `merged`, so remove-module and reconcile never delete it).
                    unc = {k: [p for p in (incoming.get("permissions") or {}).get(k, [])
                               if p not in contrib[k]] for k in PERM_KINDS}
                    unc = {k: v for k, v in unc.items() if v}
                    if unc:
                        cur = legacy_unc.setdefault(dest_rel, {})
                        for k, v in unc.items():
                            cur[k] = list(dict.fromkeys(cur.get(k, []) + v))
                        plan.lines.append(_kept_line(dest_rel, unc))
                merged_rec_all[name] = merged_rec
                ms["data"] = merge_json_permissions(ms["data"], incoming)
                files_record.append({"path": dest_rel, "sha256": None, "mode": "merge",
                                     "mechanism": entry.get("mechanism")})
                continue

            # overwrite
            if st["type"] not in ("file", "absent"):
                plan.conflicts.append(f"  REFUSE type-drift {dest_rel} (found a {st['type']})")
                continue
            if st["type"] == "file":
                rec = recorded_sha(prior, dest_rel)
                if rec is None and not force:
                    plan.conflicts.append(f"  REFUSE unmanaged-exists {dest_rel} (use --force to clobber)")
                    continue
                if rec is not None and st["sha256"] != rec and not force:
                    plan.conflicts.append(f"  REFUSE modified-downstream {dest_rel} (use --force)")
                    continue
                if st["sha256"] != src_sha:
                    plan.ops.append(txn.Op("write", dest_rel, src_bytes, want_mode, st))
                    plan.lines.append(f"  write  {'overwrite':<15} {dest_rel}")
            else:
                plan.ops.append(txn.Op("write", dest_rel, src_bytes, want_mode, st))
                plan.lines.append(f"  write  {'new':<15} {dest_rel}")
            files_record.append({"path": dest_rel, "sha256": src_sha, "mode": "overwrite",
                                 "mechanism": entry.get("mechanism")})

        secs = meta.get("_sections") or {}
        rec = {"module": name, "version": meta.get("version", "0.0.0"),
               "fingerprint": txn.fingerprint(rows), "domain": meta.get("domain"),
               "sections": {h.lower(): secs.get(h, "") for h in SECTIONS},
               "files": files_record}
        if merged_rec:
            rec["merged"] = merged_rec
        # Carry forward what an earlier install could not attribute, minus what left the file.
        for dest_rel, old_unc in (((prior_by_module.get(name) or {}).get("legacy_unclaimed")) or {}).items():
            known = dest_rel in merge_state
            cur = ((merge_state.get(dest_rel) or {}).get("data") or {}).get("permissions") or {}
            for k in PERM_KINDS:
                keepers = [p for p in old_unc.get(k, []) if isinstance(p, str)
                           and (not known or p in (cur.get(k) or []))]
                if keepers:
                    have = legacy_unc.setdefault(dest_rel, {}).setdefault(k, [])
                    have.extend(p for p in keepers if p not in have)
        if legacy_unc:
            rec["legacy_unclaimed"] = legacy_unc
        new_installs[name] = rec

    # Reconcile: a pattern a selected module owned before and no longer ships (changed fragment, or
    # a dropped merge entry) is un-merged, unless another module still owns it.
    def owned_elsewhere(me: str, dest_rel: str, kind: str) -> set[str]:
        out: set[str] = set()
        for n, inst in prior_by_module.items():
            if n != me and n not in new_installs:
                out.update(((inst.get("merged") or {}).get(dest_rel) or {}).get(kind, []))
        for n, inst in new_installs.items():
            if n != me:
                out.update(((inst.get("merged") or {}).get(dest_rel) or {}).get(kind, []))
        return out

    for name, inst in new_installs.items():
        old_all = (prior_by_module.get(name) or {}).get("merged") or {}
        for dest_rel, old in old_all.items():
            new = (inst.get("merged") or {}).get(dest_rel) or {}
            drops = {k: {p for p in old.get(k, []) if p not in new.get(k, [])
                         and p not in owned_elsewhere(name, dest_rel, k)} for k in PERM_KINDS}
            if not any(drops.values()):
                continue
            if dest_rel not in merge_state:
                try:
                    dest = txn.confine(target, dest_rel)
                    st = txn.lstate(dest)
                    if st["type"] == "absent":
                        continue
                    if st["type"] != "file":
                        raise ValueError(f"found a {st['type']}")
                    data = json.loads(dest.read_text())
                    if not isinstance(data, dict):
                        raise ValueError("not a JSON object")
                except (ValueError, OSError, txn.InstallError) as e:
                    plan.conflicts.append(f"  REFUSE unparseable-json {dest_rel} ({e})")
                    continue
                merge_state[dest_rel] = {"data": data, "st": st}
            perms = merge_state[dest_rel]["data"].get("permissions")
            if isinstance(perms, dict):
                for k in PERM_KINDS:
                    if drops[k] and isinstance(perms.get(k), list):
                        perms[k] = [p for p in perms[k] if p not in drops[k]]
                        plan.lines.append(f"  unmerge {k:<4} {len(drops[k])} pattern(s) no longer shipped by {name}")

    # One write per merged file, from the cumulative merge of every module that claimed it.
    final_sha: dict[str, str] = {}
    for dest_rel, ms in merge_state.items():
        data = (json.dumps(ms["data"], indent=2) + "\n").encode()
        final_sha[dest_rel] = txn.sha256_bytes(data)
        if ms["st"]["type"] != "file" or ms["st"]["sha256"] != final_sha[dest_rel]:
            plan.ops.append(txn.Op("write", dest_rel, data, None, ms["st"]))
            plan.lines.append(f"  merge  permissions     {dest_rel}")
    for inst in new_installs.values():
        for f in inst["files"]:
            if f.get("mode") == "merge" and f["path"] in final_sha:
                f["sha256"] = final_sha[f["path"]]

    # scoped prune: records of the SELECTED modules that the new plan no longer
    # mentions. A deselected module is never touched here.
    wanted = set(seen_dests)
    for inst in prior.get("installs", []):
        if inst.get("module") in new_installs:
            stale = [f for f in inst.get("files", []) if f.get("path") not in wanted]
            plan_prune(target, stale, force, plan, "removed-upstream")

    # merged install list: prior order, selected replaced in place, new appended
    merged: list[dict] = []
    done: set[str] = set()
    for inst in prior.get("installs", []):
        n = inst.get("module")
        if n in new_installs:
            merged.append(new_installs[n])
            done.add(n)
        else:
            keep = dict(inst)
            kept = []
            legacy = is_alias(n, mods)
            successors_done = legacy and all(x in new_installs or x in prior_by_module for x in ALIASES[n])
            for f in inst.get("files", []):
                if f.get("path") in final_sha and f.get("mode") == "merge":
                    if successors_done:
                        continue          # every 2.0 successor now records this merged file
                    f = dict(f, sha256=final_sha[f["path"]])      # still the same shared merged file
                elif f.get("path") in wanted:
                    continue
                kept.append(f)
            keep["files"] = kept
            if legacy and not kept:
                continue                  # a 1.x record whose files all moved to 2.0 records is retired
            if legacy and "merged" not in keep:
                # A retained 1.x record never wrote `merged`. It still provides every pattern its
                # successors ship, so record that: no successor's remove-module may un-merge a pattern
                # this record still provides.
                # Patterns a record already owns (written in this run, or a real prior record) are
                # excluded: a 2.0 install that newly added one must be able to remove it again.
                synth = {}
                for f in kept:
                    if f.get("mode") != "merge":
                        continue
                    frag = _fragment_patterns([x for x in ALIASES[n] if x in mods], f["path"], mods)
                    # Only patterns already in the file on disk before this run predate ownership
                    # tracking; the plan is not applied yet, so the file is still the "before".
                    try:
                        disk = json.loads(txn.confine(target, f["path"]).read_text())
                        have = disk.get("permissions") if isinstance(disk, dict) else None
                        have = have if isinstance(have, dict) else {}
                    except (OSError, ValueError, txn.ConfineError):
                        have = {}
                    frag = {k: [p for p in v if p in (have.get(k) if isinstance(have.get(k), list) else [])]
                            for k, v in frag.items()}
                    owners = [i.get("merged") or {} for i in new_installs.values()]
                    owners += [i.get("merged") or {} for i in prior.get("installs", [])
                               if i is not inst and not i.get("merged_synthetic")]
                    synth[f["path"]] = {
                        k: [p for p in v if not any(p in (o.get(f["path"]) or {}).get(k, []) for o in owners)]
                        for k, v in frag.items()}
                if synth:
                    keep["merged"] = synth
                    keep["merged_synthetic"] = True
            if keep.get("merged_synthetic") and isinstance(keep.get("merged"), dict):
                # A stand-in carried from an earlier run goes stale when a record written in this run
                # newly added a pattern the stand-in still claims (the user deleted it, the 2.0 install
                # re-added it): release it, so that module's remove-module removes it. Patterns no real
                # record owns stay claimed (never delete a pre-migration pattern).
                for path, kinds in keep["merged"].items():
                    if not isinstance(kinds, dict):
                        continue
                    for kind, pats in list(kinds.items()):
                        owned = {p for i in new_installs.values()
                                 for p in ((i.get("merged") or {}).get(path) or {}).get(kind, [])}
                        kinds[kind] = [p for p in pats if p not in owned]
            merged.append(keep)
    merged.extend(v for k, v in new_installs.items() if k not in done)
    return plan, merged


def _print_refusals(plan: Plan, target: Path) -> None:
    for c in plan.conflicts:
        print(c)


def build_manifest(prior: dict, installs: list[dict], dropped: set[str],
                   commit: str | None, dirty: bool) -> dict:
    manifest = {k: v for k, v in prior.items()}
    manifest["schema_version"] = 2
    manifest["installer"] = INSTALLER_ID
    manifest["kit_version"] = KIT_VERSION
    manifest["source_commit"] = commit
    manifest["source_dirty"] = dirty
    manifest["installs"] = installs
    ledger = manifest.get("patched_pending_upstream")
    if isinstance(ledger, list) and dropped:
        manifest["patched_pending_upstream"] = [
            e for e in ledger if not (isinstance(e, dict) and e.get("path") in dropped)]
    manifest["aggregate_fingerprint"] = aggregate_fingerprint(installs)
    return manifest


def _meaningful(m: dict) -> dict:
    """The parts of a manifest whose change means an install changed something
    (commit and txn_id alone do not)."""
    keys = ("schema_version", "source_dirty", "installs", "patched_pending_upstream")
    return {k: m.get(k) for k in keys}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _held_output():
    """Hold stdout/stderr while a SHARED-lock read runs, printing it only once the lock's
    clean exit passed. A reader that found no lock file proceeds unlocked; if a writer starts
    meanwhile, txn.Lock.recheck raises Locked on exit (exit 4) and what the read said is
    dropped rather than shown as if it were current. Wrap the `with tx.lock(shared=True)`."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield
    except txn.Locked:
        raise
    except BaseException:
        sys.stdout.write(out.getvalue())
        sys.stderr.write(err.getvalue())
        raise
    sys.stdout.write(out.getvalue())
    sys.stderr.write(err.getvalue())


def _two_phase(target: Path, tx: "txn.Txn", plan, create_target: bool) -> int:
    """Run a mutating command so that a refusal writes nothing at all.

    `plan()` is READ-ONLY and returns (rc, commit): rc is not None when the command
    finishes without writing (a refusal, or nothing to do); otherwise `commit()`
    performs the transaction.

    Phase A builds the plan under a shared lock, which never creates the lock file
    (no lock file means no installer, so nothing to wait for), and does not create
    the target directory. Only when the plan will commit does phase B create the
    target, take the exclusive lock, settle any interrupted transaction and rebuild
    the plan, so what is applied is what was verified under the lock."""
    rc = None
    with _held_output():
        with tx.lock(shared=True):
            if not tx.pending():       # a half-applied tree cannot be planned against
                rc, _ = plan()
    if rc is not None:
        return rc
    created = txn.make_missing_dirs(target) if create_target else []
    committed = False
    discarded = False
    try:
        with tx.lock() as lk:
            try:
                _recovered(tx)
                rc, commit = plan()
                if rc is not None:
                    return rc
                commit()
                committed = True
            finally:
                if not committed:
                    # take back what THIS call created while the lock is still held: the lock file
                    # through the inode it holds, then the directories (never by pathname afterwards)
                    if lk.created or created:
                        with contextlib.suppress(Exception):
                            lk.unlink_if_held()
                    if created:
                        txn.discard_created(created, None)
                        discarded = True
    finally:
        if created and not committed and not discarded:
            txn.discard_created(created, None)
    return 0


def _print_errors(plan: Plan) -> int:
    for e in plan.errors:
        print(f"[devkit] ERROR: {e}", file=sys.stderr)
    print("[devkit] nothing was written.", file=sys.stderr)
    return 8


def install(target: Path, modules: list[str], force: bool, allow_dirty: bool = False) -> int:
    mods = discover_modules()
    if not modules:
        modules = list(mods.keys())
    modules = expand_aliases(modules, mods)
    unknown = [m for m in modules if m not in mods]
    if unknown:
        print(f"[devkit] ERROR: unknown module(s): {unknown}. Known: {list(mods)}", file=sys.stderr)
        return 1

    bad = [e for m in modules for e in mods[m].get("_errors", [])]
    if bad:
        for e in bad:
            print(f"[devkit] ERROR: {e}", file=sys.stderr)
        print("[devkit] nothing was written.", file=sys.stderr)
        return 8

    order = topo_order(modules, mods)
    print(f"[devkit] install order: {order}")
    if os.path.lexists(target) and not target.is_dir():
        print(f"[devkit] ERROR: target {target} exists and is not a directory", file=sys.stderr)
        return 8
    tx = make_txn(target)

    def plan_once():
        commit, dirty = source_state(order, mods)
        if dirty and not allow_dirty:
            print("[devkit] REFUSE: the factory checkout has uncommitted changes under the selected "
                  "modules (or is not a git checkout). Commit them, or re-run with --allow-dirty "
                  "(the manifest will record source_dirty=true).", file=sys.stderr)
            return 7, None
        prior = load_manifest(target)
        plan, installs = build_install_plan(target, order, mods, prior, force)
        if plan.errors:
            return _print_errors(plan), None
        if plan.conflicts:
            _print_refusals(plan, target)
            print(f"[devkit] {len(plan.conflicts)} file(s) refused (modified/unmanaged). "
                  "Nothing was written. Re-run with --force to overwrite.")
            return 3, None
        manifest = build_manifest(prior, installs, plan.dropped, commit, dirty)
        if not plan.ops and _meaningful(manifest) == _meaningful(prior):
            print("[devkit] up to date; nothing to do.")
            return 0, None

        def do_commit() -> None:
            tx.run(plan.ops, manifest)
            for line in plan.lines:
                print(line)
            print(f"[devkit] wrote {target}/{MANIFEST_NAME} "
                  f"(source_commit={commit}, source_dirty={str(dirty).lower()})")
        return None, do_commit

    return _two_phase(target, tx, plan_once, create_target=True)


def update(target: Path, modules: list[str], force: bool, allow_dirty: bool = False) -> int:
    """Bring an installed target up to this kit: the same plan as `install`, over the modules the
    target's manifest records (1.x names mapped to their 2.0 modules), in one transaction that also
    rewrites the manifest's module records."""
    mpath = target / MANIFEST_NAME
    if not mpath.exists():
        print(f"[devkit] REFUSE: no {MANIFEST_NAME} in {target}; use `install` for a new target", file=sys.stderr)
        return 1
    if not modules:
        try:
            recorded = [i.get("module") for i in json.loads(mpath.read_text()).get("installs", [])]
        except (OSError, ValueError) as e:
            print(f"[devkit] ERROR: cannot read manifest: {e}", file=sys.stderr)
            return 1
        mods = discover_modules()
        legacy = [n for n in recorded if is_alias(n, mods)]
        modules = expand_aliases([n for n in recorded if n in mods or is_alias(n, mods)], mods, announce=False)
        if not modules:
            print(f"[devkit] nothing is installed in {target} that this kit provides; nothing to update "
                  "(use `install` to add modules)")
            return 0
        if legacy:
            print("[devkit] migrating 1.x records: " + "; ".join(f"{n} -> {', '.join(ALIASES[n])}" for n in legacy))
    return install(target, modules, force, allow_dirty)


def remove_module_named(target: Path, name: str, force: bool) -> int:
    """remove-module with 1.x alias handling: an installed record of that exact name is removed as
    before; otherwise a 1.x name acts on every installed module it maps to."""
    mods = discover_modules()
    mpath = target / MANIFEST_NAME
    have: list[str] = []
    if mpath.exists():
        try:
            have = [i.get("module") for i in json.loads(mpath.read_text()).get("installs", [])]
        except (OSError, ValueError):
            have = []
    if name in ALIASES and (name in have or is_alias(name, mods)):
        targets = [name] if name in have else [n for n in expand_aliases([name], mods, announce=False) if n in have]
        print(f"[devkit] DEPRECATED: module '{name}' was split in 2.0 into {', '.join(ALIASES[name])}; "
              f"the old name stops working in {ALIAS_REMOVED_IN}")
        if not targets:
            print(f"[devkit] none of {', '.join(ALIASES[name])} is installed in {target}; nothing to remove")
            return 0
    else:
        targets = [name]
    for n in targets:
        rc = remove_module(target, n, force)
        if rc:
            return rc
    return 0


def remove_module(target: Path, name: str, force: bool) -> int:
    if not (target / MANIFEST_NAME).exists():
        print(f"[devkit] REFUSE: no {MANIFEST_NAME} in {target}", file=sys.stderr)
        return 1
    tx = make_txn(target)

    def plan_once():
        prior = load_manifest(target)
        installs = prior.get("installs", [])
        inst = next((i for i in installs if i.get("module") == name), None)
        if inst is None:
            have = [i.get("module") for i in installs]
            print(f"[devkit] ERROR: module '{name}' is not installed in {target} (installed: {have})",
                  file=sys.stderr)
            return 1, None
        plan = Plan()
        rest = [i for i in installs if i is not inst]
        unmerged = plan_unmerge(target, inst, rest, plan, force)
        plan_prune(target, [f for f in inst.get("files", []) if f.get("path") not in unmerged],
                   force, plan, "module-removed")
        if plan.errors:
            for e in plan.errors:
                print(f"[devkit] ERROR: {e}", file=sys.stderr)
            return 8, None
        if plan.conflicts:
            _print_refusals(plan, target)
            print(f"[devkit] {len(plan.conflicts)} file(s) refused. Nothing was written. "
                  "Re-run with --force to remove them anyway.")
            return 3, None
        if unmerged:
            rest = [dict(i, files=[dict(f, sha256=unmerged[f["path"]]) if f.get("path") in unmerged
                                   and f.get("mode") == "merge" else f for f in i.get("files", [])])
                    for i in rest]
        manifest = build_manifest(prior, rest, plan.dropped,
                                  prior.get("source_commit"), bool(prior.get("source_dirty", False)))

        def do_commit() -> None:
            tx.run(plan.ops, manifest)
            for line in plan.lines:
                print(line)
            print(f"[devkit] removed module '{name}' from {target}")
        return None, do_commit

    return _two_phase(target, tx, plan_once, create_target=False)


def recover(target: Path) -> int:
    if not target.is_dir():
        print(f"[devkit] REFUSE: {target} is not a directory", file=sys.stderr)
        return 1
    tx = make_txn(target)
    # Nothing pending and no stray manifest temp file => nothing to do, and doing
    # nothing must not create a lock file in a directory that never had one.
    idle = False
    with _held_output():
        with tx.lock(shared=True):
            idle = not tx.pending() and not any(
                n.startswith(MANIFEST_NAME + ".tmp-") for n in os.listdir(target))
            if idle:
                print("[devkit] no interrupted transaction")
    if idle:
        return 0
    with tx.lock():
        log = tx.recover()
    for line in log or ["no interrupted transaction"]:
        print(f"[devkit] {line}")
    return 0


def _pending(target: Path) -> int:
    """Non-zero (6) when an interrupted transaction is present; never mutates."""
    pend = make_txn(target).pending()
    if pend:
        print(f"[devkit] REFUSE: {len(pend)} interrupted transaction(s) in {target}; "
              f"run: devkit-install.sh recover {shlex.quote(str(target))}", file=sys.stderr)
        return 6
    return 0


def outdated(target: Path, wanted: list[str] | None = None) -> int:
    mods = discover_modules()
    unknown = [w for w in wanted or [] if w not in mods and not is_alias(w, mods)]
    if unknown:
        print(f"[devkit] ERROR: unknown module(s): {unknown}. Known: {list(mods)}", file=sys.stderr)
        return 1
    only = set(expand_aliases(wanted, mods)) | {w for w in wanted if is_alias(w, mods)} if wanted else None
    with _held_output():
        with make_txn(target).lock(shared=True):
            return _outdated_locked(target, only)


def _outdated_locked(target: Path, only: set[str] | None = None) -> int:
    rc = _pending(target)
    if rc:
        return rc
    mpath = target / MANIFEST_NAME
    if not mpath.exists():
        return 0
    try:
        manifest = json.loads(mpath.read_text())
    except (OSError, ValueError) as e:
        print(f"[devkit] ERROR: cannot read manifest: {e}", file=sys.stderr)
        return 1
    mods = discover_modules()
    for inst in manifest.get("installs", []):
        name = inst.get("module", "?")
        have = inst.get("fingerprint")
        if only is not None and name not in only:
            continue
        if is_alias(name, mods):
            print(f"devkit: {name} outdated (a 1.x module, split in 2.0 into {', '.join(ALIASES[name])}) -> "
                  f"devkit-install.sh update {shlex.quote(str(target))}")
            continue
        if name not in mods:
            print(f"devkit: {name} outdated (module no longer exists in the factory)")
            continue
        try:
            cur = txn.fingerprint(module_rows(mods[name]))
        except OSError as e:
            print(f"devkit: {name} outdated (cannot read its source payload: {e})")
            continue
        if have != cur:
            print(f"devkit: {name} outdated (installed {str(have)[:8]}, source {cur[:8]}) -> "
                  f"devkit-install.sh install {shlex.quote(str(target))} {name}")
    return 0


def patch(target: Path, rel_path: str, reason: str) -> int:
    """Record a downstream edit as pending-upstream in the manifest ledger
    (see devkit/README.md, Patch ledger).

    Preconditions (all required, each refuses with a named reason):
      1. <path> is manifest-managed (exact match)
      2. its manifest record has mode: overwrite (merge-mode files are rejected)
      3. the file exists and is a regular file
      4. the file does not carry the fork marker
      5. the file's current sha differs from the recorded sha
    """
    manifest_path = target / MANIFEST_NAME
    if not manifest_path.exists():
        print(f"[devkit] REFUSE: no {MANIFEST_NAME} in {target}", file=sys.stderr)
        return 1
    tx = make_txn(target)

    def plan_once():
        manifest = json.loads(manifest_path.read_text())

        rec = find_prior_record(manifest, rel_path)
        if rec is None:
            print(f"[devkit] REFUSE: {rel_path} is not a manifest-managed path", file=sys.stderr)
            return 1, None

        mode = rec.get("mode", "overwrite")
        if mode != "overwrite":
            print(
                f"[devkit] REFUSE: {rel_path} has mode '{mode}', not 'overwrite' — "
                "a mode:merge file cannot carry a pending-upstream entry",
                file=sys.stderr,
            )
            return 1, None

        fpath = target / rel_path
        if not fpath.is_file():
            print(f"[devkit] REFUSE: {rel_path} does not exist or is not a regular file", file=sys.stderr)
            return 1, None

        if FORK_MARKER in _safe_read(fpath):
            print(f"[devkit] REFUSE: {rel_path} carries the fork marker — target owns it", file=sys.stderr)
            return 1, None

        cur = sha256(fpath)
        recorded = rec.get("sha256")
        if cur == recorded:
            print(f"[devkit] REFUSE: {rel_path} is unmodified (sha matches the manifest record) — nothing to patch", file=sys.stderr)
            return 1, None

        if not reason:
            print("[devkit] REFUSE: --reason is required", file=sys.stderr)
            return 1, None

        ledger = manifest.get("patched_pending_upstream")
        ledger = [e for e in ledger if isinstance(e, dict) and e.get("path") != rel_path] if isinstance(ledger, list) else []
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ledger.append({"path": rel_path, "sha256": cur, "reason": reason, "date": today})
        manifest["patched_pending_upstream"] = ledger

        def do_commit() -> None:
            tx.run([], manifest)
            print(f"[devkit] patch recorded: {rel_path} (sha256={cur}, date={today})")
        return None, do_commit

    return _two_phase(target, tx, plan_once, create_target=False)


def check(target: Path) -> int:
    with _held_output():
        with make_txn(target).lock(shared=True):
            rc = _pending(target)
            if rc:
                return rc
            if not AUDIT_SCRIPT.exists():
                print(f"[devkit] ERROR: audit script not found at {AUDIT_SCRIPT}", file=sys.stderr)
                return 1
            # captured (not inherited) so it can be held back if a writer appeared meanwhile
            r = subprocess.run([sys.executable, str(AUDIT_SCRIPT), str(target)],
                               capture_output=True, text=True)
            sys.stdout.write(r.stdout)
            sys.stderr.write(r.stderr)
            return r.returncode


def readme_rows(mods: dict[str, dict]) -> list[dict]:
    return [{"name": n, "version": str(m.get("version", "")), "domain": str(m.get("domain", "")),
             "mechanisms": [e.get("mechanism") for e in m.get("payload") or []],
             "prevents": (m.get("_sections") or {}).get("Prevents", "").split("\n\n")[0]}
            for n, m in mods.items()]


def readme(check: bool) -> int:
    mods = discover_modules()
    bad = [e for m in mods.values() for e in m.get("_errors", [])]
    if bad:
        for e in bad:
            print(f"[devkit] ERROR: {e}", file=sys.stderr)
        return 8
    path = FACTORY / "README.md"
    text = path.read_text()
    new = modtable.replace_table(text, readme_rows(mods))
    if new is None:
        print(f"[devkit] ERROR: {path} has no {modtable.BEGIN} ... {modtable.END} markers", file=sys.stderr)
        return 1
    if new == text:
        print("[devkit] README module table is up to date")
        return 0
    if check:
        print(f"[devkit] README module table differs from the generated one; run: "
              f"python3 devkit/install/_devkit.py readme", file=sys.stderr)
        return 1
    path.write_text(new)
    print(f"[devkit] wrote the module table in {path}")
    return 0


def domains_json() -> str:
    schema = yaml.safe_load(SCHEMA_FILE.read_text())
    doc = {"generated_from": "the kit's registry schema: domains, module-only domains, mechanisms",
           "domains": list(schema["domains"]) + list(schema.get("module_only_domains") or {}),
           "mechanisms": list(schema["mechanisms"])}
    return json.dumps(doc, indent=2) + "\n"


# The `prevents` text of each domain in the generated schema. This map supplies it, one entry per
# domain (a domain without one stops `domains`), so the generated file never depends on free text
# in the schema input.
PUBLIC_DOMAIN_PREVENTS: dict[str, str] = {
    "system-safety": "irreversible damage to the machine or the repository",
    "secrets-privacy": "credential and personal-data exposure",
    "git-multi-session": "parallel-session collisions, orphaned state",
    "cost-control": "runaway spend on metered services and usage quotas",
    "work-craft": "low-quality artifacts, process shortcuts",
    "decision-gating-business": "building the wrong thing (a check that each plan names its intended outcome)",
    "decision-gating-technical": "building the thing wrong (no rollback, unverified claims, false completion, "
                                 "an unreviewed build plan)",
    "outbound-comms": "wrong or embarrassing external sends",
    "skill-governance": "skill rot: skills that change without anyone noticing, skills that never trigger, and skills nothing checks automatically",
    "content-lifecycle": "ungoverned content accumulation: stale notes, corrections noted but never acted on, unclosed follow-ups",
    "observe": "nothing: a telemetry band, not counted among the protective domains; also a legal module domain",
    "all": "cross-cutting VERIFY and ROUTE artifacts (the audit module, the registry itself, the CLAUDE.md "
           "pointer kernel)",
    "template": "nothing: a copy-and-rename skeleton the repo owner fills in",
}


def _public_prevents(name: str) -> str:
    if name not in PUBLIC_DOMAIN_PREVENTS:
        raise SystemExit("[devkit] ERROR: domain %r has no entry in PUBLIC_DOMAIN_PREVENTS (_devkit.py); "
                         "add its public description before generating the shipped schema" % name)
    return PUBLIC_DOMAIN_PREVENTS[name]


def schema_json() -> str:
    """The downstream registry schema: vocabulary only (mechanisms, domains, enums), generated from the
    registry schema file with the public domain descriptions above. Free-text provenance and ownership
    keys of the source are not carried."""
    schema = yaml.safe_load(SCHEMA_FILE.read_text())
    fields = schema["field_definitions"]
    doc = {"generated_by": "_devkit.py domains",
           "schema_version": str(schema["schema_version"]),
           "mechanisms": dict(schema["mechanisms"]),
           "domains": {k: {"letter": str(v["letter"]), "prevents": _public_prevents(k)}
                       for k, v in schema["domains"].items()},
           "module_only_domains": {k: {"letter": str(v["letter"]), "prevents": _public_prevents(k)}
                                   for k, v in (schema.get("module_only_domains") or {}).items()},
           "artifact_types": list(fields["artifact_type"]["values"]),
           "statuses": list(fields["status"]["values"])}
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def domains(check: bool) -> int:
    if not SCHEMA_FILE.is_file():
        print(f"[devkit] ERROR: {SCHEMA_FILE} not found (this command needs the registry schema; "
              f"without it, only the generated {DOMAINS_FILE.name} is available)", file=sys.stderr)
        return 1
    wants = [(DOMAINS_FILE, domains_json()), (SCHEMA_PAYLOAD_FILE, schema_json())]
    stale = [(f, w) for f, w in wants if not f.is_file() or f.read_text() != w]
    if not stale:
        print("[devkit] domains.json is up to date")
        return 0
    if check:
        print("[devkit] %s differ from the schema; run: python3 devkit/install/_devkit.py domains"
              % " and ".join(f.name for f, _ in stale), file=sys.stderr)
        return 1
    for f, w in stale:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(w)
        print(f"[devkit] wrote {f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="devkit-install")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("install")
    pi.add_argument("target")
    pi.add_argument("modules", nargs="*")
    pi.add_argument("--force", action="store_true")
    pi.add_argument("--allow-dirty", action="store_true",
                    help="install from a factory checkout with uncommitted changes (recorded as source_dirty)")
    pc = sub.add_parser("check")
    pc.add_argument("target")
    pp = sub.add_parser("patch")
    pp.add_argument("target")
    pp.add_argument("path")
    pp.add_argument("--reason", required=True)
    po = sub.add_parser("outdated")
    po.add_argument("target")
    po.add_argument("modules", nargs="*")
    pu = sub.add_parser("update")
    pu.add_argument("target")
    pu.add_argument("modules", nargs="*")
    pu.add_argument("--force", action="store_true")
    pu.add_argument("--allow-dirty", action="store_true")
    pr = sub.add_parser("remove-module")
    pr.add_argument("target")
    pr.add_argument("name")
    pr.add_argument("--force", action="store_true")
    pv = sub.add_parser("recover")
    pv.add_argument("target")
    p_readme = sub.add_parser("readme")
    p_readme.add_argument("--check", action="store_true")
    pd = sub.add_parser("domains")
    pd.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.cmd == "readme":
        return readme(args.check)
    if args.cmd == "domains":
        return domains(args.check)

    target = Path(args.target).expanduser().resolve()
    try:
        if args.cmd in ("install", "update", "patch", "remove-module", "recover"):
            _guard_target(target)
        if args.cmd == "install":
            return install(target, args.modules, args.force, args.allow_dirty)
        if args.cmd == "update":
            return update(target, args.modules, args.force, args.allow_dirty)
        if args.cmd == "check":
            return check(target)
        if args.cmd == "patch":
            return patch(target, args.path, args.reason)
        if args.cmd == "outdated":
            return outdated(target, args.modules)
        if args.cmd == "remove-module":
            return remove_module_named(target, args.name, args.force)
        if args.cmd == "recover":
            return recover(target)
    except txn.InstallError as e:
        print(f"[devkit] ERROR: {e}", file=sys.stderr)
        return e.code
    return 1


if __name__ == "__main__":
    sys.exit(main())
