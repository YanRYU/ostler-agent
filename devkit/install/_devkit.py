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
import shlex
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml  # PyYAML — factory-side only

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


def parse_module(module_md: Path) -> dict:
    text = module_md.read_text()
    if not text.startswith("---"):
        raise ValueError(f"{module_md}: no frontmatter")
    _, fm, _body = text.split("---", 2)
    meta = yaml.safe_load(fm)
    meta["_dir"] = module_md.parent
    return meta


def discover_modules() -> dict[str, dict]:
    mods = {}
    for md in sorted(MODULES_DIR.glob("*/MODULE.md")):
        meta = parse_module(md)
        mods[meta["name"]] = meta
    return mods


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
        for dep in mods[name].get("depends-on") or []:
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
            plan.lines.append(f"  drop   merge-file-kept  {rel} (a merged file cannot be un-merged; left in place)")
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


def build_install_plan(target: Path, order: list[str], mods: dict[str, dict],
                       prior: dict, force: bool) -> tuple[Plan, list[dict]]:
    plan = Plan()
    new_installs: dict[str, dict] = {}
    seen_dests: dict[str, str] = {}
    seen_keys: dict[str, str] = {}          # _norm(dest) -> dest
    prior_keys: dict[str, set[str]] = {}    # _norm(recorded path) -> {recorded paths}
    for inst in prior.get("installs", []):
        for f in inst.get("files", []):
            if isinstance(f.get("path"), str):
                prior_keys.setdefault(_norm(f["path"]), set()).add(f["path"])
    for name in order:
        meta = mods[name]
        files_record = []
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
            if dest_rel in seen_dests:
                plan.errors.append(f"{name}: dest {dest_rel!r} already claimed by {seen_dests[dest_rel]}")
                continue
            key = _norm(dest_rel)
            if key in seen_keys:
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
            seen_dests[dest_rel] = name
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
                    files_record.append(prior_rec)
                continue

            if merge:
                if st["type"] not in ("file", "absent"):
                    plan.conflicts.append(f"  REFUSE type-drift {dest_rel} (found a {st['type']})")
                    continue
                try:
                    existing = json.loads(dest.read_text()) if st["type"] == "file" else {}
                    incoming = json.loads(src_bytes)
                    if not isinstance(existing, dict) or not isinstance(incoming, dict):
                        raise ValueError("not a JSON object")
                except (ValueError, OSError) as e:
                    plan.conflicts.append(f"  REFUSE unparseable-json {dest_rel} ({e})")
                    continue
                data = (json.dumps(merge_json_permissions(existing, incoming), indent=2) + "\n").encode()
                new_sha = txn.sha256_bytes(data)
                if st["type"] != "file" or st["sha256"] != new_sha:
                    plan.ops.append(txn.Op("write", dest_rel, data, None, st))
                    plan.lines.append(f"  merge  permissions     {dest_rel}")
                files_record.append({"path": dest_rel, "sha256": new_sha, "mode": "merge"})
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
            files_record.append({"path": dest_rel, "sha256": src_sha, "mode": "overwrite"})

        new_installs[name] = {"module": name, "version": meta.get("version", "0.0.0"),
                              "fingerprint": txn.fingerprint(rows), "files": files_record}

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
            keep["files"] = [f for f in inst.get("files", []) if f.get("path") not in wanted]
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
    unknown = [m for m in modules if m not in mods]
    if unknown:
        print(f"[devkit] ERROR: unknown module(s): {unknown}. Known: {list(mods)}", file=sys.stderr)
        return 1

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
        plan_prune(target, list(inst.get("files", [])), force, plan, "module-removed")
        if plan.errors:
            for e in plan.errors:
                print(f"[devkit] ERROR: {e}", file=sys.stderr)
            return 8, None
        if plan.conflicts:
            _print_refusals(plan, target)
            print(f"[devkit] {len(plan.conflicts)} file(s) refused. Nothing was written. "
                  "Re-run with --force to remove them anyway.")
            return 3, None
        rest = [i for i in installs if i is not inst]
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


def outdated(target: Path) -> int:
    with _held_output():
        with make_txn(target).lock(shared=True):
            return _outdated_locked(target)


def _outdated_locked(target: Path) -> int:
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
    pr = sub.add_parser("remove-module")
    pr.add_argument("target")
    pr.add_argument("name")
    pr.add_argument("--force", action="store_true")
    pv = sub.add_parser("recover")
    pv.add_argument("target")
    args = ap.parse_args()

    target = Path(args.target).expanduser().resolve()
    try:
        if args.cmd in ("install", "patch", "remove-module", "recover"):
            _guard_target(target)
        if args.cmd == "install":
            return install(target, args.modules, args.force, args.allow_dirty)
        if args.cmd == "check":
            return check(target)
        if args.cmd == "patch":
            return patch(target, args.path, args.reason)
        if args.cmd == "outdated":
            return outdated(target)
        if args.cmd == "remove-module":
            return remove_module(target, args.name, args.force)
        if args.cmd == "recover":
            return recover(target)
    except txn.InstallError as e:
        print(f"[devkit] ERROR: {e}", file=sys.stderr)
        return e.code
    return 1


if __name__ == "__main__":
    sys.exit(main())
