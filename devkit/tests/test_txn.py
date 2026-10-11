"""Tests for install/lib/txn.py - generic crash-safe transactions.

Covers crash safety at every fault point, the optional derived file,
remove_paths, compare-and-swap, locking and the fingerprint.

Run from the repo root:
    bash devkit/tests/test-txn.sh

Every test uses a tempfile sandbox; no real directory outside it is touched.
Crash tests run a child python process that dies with exit 137 (os._exit) at a
named fault point, then verify recovery in this process.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LIB = Path(__file__).resolve().parents[2] / "install" / "lib"
sys.path.insert(0, str(LIB))
import txn  # noqa: E402

MANIFEST = ".m.json"
DERIVED = ".summary.json"
TXNDIR = ".txn"
LOCKNAME = ".lock"


def derived_bytes(m):
    return json.dumps({"fp": m["aggregate_fingerprint"]}, sort_keys=True).encode()


def make_txn(root, derived=True):
    return txn.Txn(
        Path(root), manifest_name=MANIFEST, txn_dirname=TXNDIR, lockname=LOCKNAME,
        derived_name=DERIVED if derived else None,
        derived_bytes=derived_bytes if derived else None)


def snapshot(root, exclude=(LOCKNAME,)):
    """Independent (not using txn.lstate) recursive lstat snapshot."""
    root = Path(root)
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if rel in exclude:
                continue
            st = os.lstat(p)
            mode = format(st.st_mode & 0o777, "04o")
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("symlink", mode, os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", mode, None)
            elif stat.S_ISREG(st.st_mode):
                out[rel] = ("file", mode, hashlib.sha256(p.read_bytes()).hexdigest())
            else:
                out[rel] = ("other", mode, None)
    return out


def put(root, rel, data, mode="0644"):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.chmod(p, int(mode, 8))
    return p


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="txn-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.tmp = Path(os.path.realpath(self._tmp))

    def sandbox(self, name="root"):
        p = self.tmp / name
        p.mkdir()
        return p


# --------------------------------------------------------------------------
# lstate / hashing
# --------------------------------------------------------------------------
class LstateTests(SandboxCase):
    def test_absent(self):
        self.assertEqual(txn.lstate(self.tmp / "nope"),
                         {"type": "absent", "mode": None, "sha256": None})

    def test_absent_under_a_file_is_absent(self):
        f = put(self.tmp, "f", b"x")
        self.assertEqual(txn.lstate(f / "child")["type"], "absent")

    def test_file(self):
        p = put(self.tmp, "a.sh", b"hello\n", "0755")
        self.assertEqual(txn.lstate(p), {
            "type": "file", "mode": "0755",
            "sha256": hashlib.sha256(b"hello\n").hexdigest()})

    def test_file_mode_is_four_digit_octal(self):
        p = put(self.tmp, "a", b"x", "0600")
        self.assertEqual(txn.lstate(p)["mode"], "0600")

    def test_dir(self):
        d = self.tmp / "d"
        d.mkdir()
        os.chmod(d, 0o750)
        self.assertEqual(txn.lstate(d), {"type": "dir", "mode": "0750", "sha256": None})

    def test_symlink_uses_lstat_and_hashes_target_text(self):
        t = put(self.tmp, "target", b"real content")
        link = self.tmp / "link"
        os.symlink("target", link)
        st = txn.lstate(link)
        self.assertEqual(st["type"], "symlink")
        self.assertEqual(st["sha256"], hashlib.sha256(b"target").hexdigest())
        self.assertNotEqual(st["sha256"], hashlib.sha256(t.read_bytes()).hexdigest())

    def test_dangling_symlink(self):
        link = self.tmp / "dangling"
        os.symlink("does-not-exist", link)
        st = txn.lstate(link)
        self.assertEqual(st["type"], "symlink")
        self.assertEqual(st["sha256"], hashlib.sha256(b"does-not-exist").hexdigest())

    def test_symlink_to_dir_is_symlink_not_dir(self):
        (self.tmp / "d").mkdir()
        os.symlink("d", self.tmp / "l")
        self.assertEqual(txn.lstate(self.tmp / "l")["type"], "symlink")

    def test_other_fifo(self):
        f = self.tmp / "fifo"
        os.mkfifo(f)
        st = txn.lstate(f)
        self.assertEqual(st["type"], "other")
        self.assertIsNone(st["sha256"])

    def test_sha256_helpers(self):
        p = put(self.tmp, "x", b"abc")
        want = hashlib.sha256(b"abc").hexdigest()
        self.assertEqual(txn.sha256_bytes(b"abc"), want)
        self.assertEqual(txn.sha256_file(p), want)


# --------------------------------------------------------------------------
# confine
# --------------------------------------------------------------------------
class ConfineTests(SandboxCase):
    def test_plain_relative_ok(self):
        root = self.sandbox()
        self.assertEqual(txn.confine(root, "hooks/a.sh"), root / "hooks" / "a.sh")

    def test_existing_real_dirs_ok(self):
        root = self.sandbox()
        (root / "hooks").mkdir()
        self.assertEqual(txn.confine(root, "hooks/a.sh"), root / "hooks" / "a.sh")

    def test_absolute_rejected(self):
        root = self.sandbox()
        with self.assertRaises(txn.ConfineError) as cm:
            txn.confine(root, "/etc/passwd")
        self.assertEqual(cm.exception.code, 8)

    def test_dotdot_rejected(self):
        root = self.sandbox()
        for rel in ("../x", "a/../../x", "a/..", ".."):
            with self.subTest(rel=rel), self.assertRaises(txn.ConfineError):
                txn.confine(root, rel)

    def test_empty_dot_and_nonnormalized_rejected(self):
        root = self.sandbox()
        for rel in ("", ".", "a//b", "a/./b", "a/", "./a"):
            with self.subTest(rel=rel), self.assertRaises(txn.ConfineError):
                txn.confine(root, rel)

    def test_nul_and_nonstring_rejected(self):
        root = self.sandbox()
        for rel in ("a\x00b", None, 5):
            with self.subTest(rel=rel), self.assertRaises(txn.ConfineError):
                txn.confine(root, rel)

    def test_symlinked_parent_rejected_even_when_inside_root(self):
        root = self.sandbox()
        (root / "real").mkdir()
        os.symlink("real", root / "link")
        with self.assertRaises(txn.ConfineError):
            txn.confine(root, "link/x")

    def test_symlinked_parent_pointing_outside_rejected(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        os.symlink(outside, root / "escape")
        with self.assertRaises(txn.ConfineError):
            txn.confine(root, "escape/x")

    def test_deep_symlinked_ancestor_rejected(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        (root / "a").mkdir()
        os.symlink(outside, root / "a" / "b")
        with self.assertRaises(txn.ConfineError):
            txn.confine(root, "a/b/c/d.txt")

    def test_non_directory_ancestor_rejected(self):
        root = self.sandbox()
        put(root, "afile", b"x")
        with self.assertRaises(txn.ConfineError):
            txn.confine(root, "afile/child")

    def test_symlinked_install_root_itself_is_ok(self):
        real = self.sandbox("real-root")
        link = self.tmp / "root-link"
        os.symlink(real, link)
        self.assertEqual(txn.confine(link, "hooks/a.sh"), link / "hooks" / "a.sh")

    def test_final_component_symlink_is_returned_not_followed(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        os.symlink(outside / "victim", root / "leaf")
        # the leaf is the caller's business (lstate reports symlink); parent is fine
        self.assertEqual(txn.confine(root, "leaf"), root / "leaf")

    def test_missing_ancestors_are_fine(self):
        root = self.sandbox()
        self.assertEqual(txn.confine(root, "x/y/z/w.txt"), root / "x/y/z/w.txt")


# --------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------
LOCK_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[1])
import txn
try:
    with txn.Lock(sys.argv[2], ".lock"):
        print("ACQUIRED")
except txn.Locked as e:
    print("LOCKED", e.code)
    sys.exit(e.code)
'''


class LockTests(SandboxCase):
    def run_child(self, root):
        return subprocess.run([sys.executable, "-c", LOCK_CHILD, str(LIB), str(root)],
                              capture_output=True, text=True)

    def test_second_process_gets_locked(self):
        root = self.sandbox()
        with txn.Lock(root, ".lock"):
            r = self.run_child(root)
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("LOCKED 4", r.stdout)

    def test_released_after_with_block(self):
        root = self.sandbox()
        with txn.Lock(root, ".lock"):
            pass
        r = self.run_child(root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ACQUIRED", r.stdout)

    def test_released_after_exception(self):
        root = self.sandbox()
        with self.assertRaises(ValueError):
            with txn.Lock(root, ".lock"):
                raise ValueError("boom")
        r = self.run_child(root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_in_process_contention_raises_locked_with_code_4(self):
        root = self.sandbox()
        with txn.Lock(root, ".lock"):
            with self.assertRaises(txn.Locked) as cm:
                with txn.Lock(root, ".lock"):
                    self.fail("should not acquire")
            self.assertEqual(cm.exception.code, 4)
            self.assertIsInstance(cm.exception, txn.InstallError)

    def test_lockfile_created_and_symlink_lockfile_refused(self):
        root = self.sandbox()
        with txn.Lock(root, ".lock"):
            self.assertTrue((root / ".lock").exists())
        other = self.sandbox("other")
        os.symlink(other / "elsewhere", other / ".lock")
        with self.assertRaises(txn.InstallError):
            with txn.Lock(other, ".lock"):
                pass
        self.assertFalse((other / "elsewhere").exists())

    def test_missing_root_is_install_error(self):
        with self.assertRaises(txn.InstallError):
            with txn.Lock(self.tmp / "no-such-root", ".lock"):
                pass


# --------------------------------------------------------------------------
# fingerprint
# --------------------------------------------------------------------------
def row(dest, sha="a" * 64, typ="file", mode="0644"):
    return {"dest": dest, "sha256": sha, "type": typ, "mode": mode}


class LockCreatedTests(SandboxCase):
    def test_created_is_true_only_for_the_creating_acquisition(self):
        root = self.sandbox()
        with txn.Lock(root, LOCKNAME) as first:
            self.assertTrue(first.created)
        with txn.Lock(root, LOCKNAME) as second:
            self.assertFalse(second.created)

    def test_created_is_false_when_the_file_appears_between_precheck_and_acquire(self):
        root = self.sandbox()
        self.assertFalse(os.path.lexists(root / LOCKNAME))     # the caller's pre-check

        def seam(point, *a, **k):
            if point == "lock:before_open" and not os.path.lexists(root / LOCKNAME):
                (root / LOCKNAME).write_bytes(b"")             # another installer got there first
        with mock.patch.object(txn, "_seam", seam):
            with txn.Lock(root, LOCKNAME) as lk:
                self.assertFalse(lk.created)

    def test_shared_lock_never_creates(self):
        root = self.sandbox()
        with txn.Lock(root, LOCKNAME, shared=True) as lk:
            self.assertFalse(lk.created)


class FingerprintTests(unittest.TestCase):
    def test_default_domain_is_the_devkit_domain(self):
        rows = [row("a"), row("b")]
        self.assertEqual(txn.fingerprint(rows), txn.fingerprint(rows, domain=b"devkit-fingerprint/1"))
        self.assertNotEqual(txn.fingerprint(rows), txn.fingerprint(rows, domain=b"other-domain/1"))

    def test_hex_and_deterministic(self):
        rows = [row("a"), row("b")]
        f = txn.fingerprint(rows)
        self.assertRegex(f, r"^[0-9a-f]{64}$")
        self.assertEqual(f, txn.fingerprint([dict(r) for r in rows]))

    def test_order_independent(self):
        rows = [row("a"), row("b"), row("c", mode="0755")]
        self.assertEqual(txn.fingerprint(rows), txn.fingerprint(list(reversed(rows))))
        self.assertEqual(txn.fingerprint(rows), txn.fingerprint([rows[1], rows[2], rows[0]]))

    def test_sensitive_to_every_field(self):
        base = txn.fingerprint([row("a")])
        variants = [row("b"), row("a", sha="b" * 64), row("a", typ="symlink"),
                    row("a", mode="0755")]
        for v in variants:
            with self.subTest(v=v):
                self.assertNotEqual(base, txn.fingerprint([v]))

    def test_extra_rows_change_it(self):
        base = txn.fingerprint([row("a")])
        extra = {"dest": "module:example", "sha256": "c" * 64,
                 "type": "module", "mode": None}
        self.assertNotEqual(base, txn.fingerprint([row("a"), extra]))
        self.assertEqual(txn.fingerprint([row("a"), extra]),
                         txn.fingerprint([extra, row("a")]))

    def test_empty_is_stable(self):
        self.assertEqual(txn.fingerprint([]), txn.fingerprint([]))
        self.assertNotEqual(txn.fingerprint([]), txn.fingerprint([row("a")]))

    def test_none_and_empty_string_are_distinct(self):
        self.assertNotEqual(txn.fingerprint([row("a", mode=None)]),
                            txn.fingerprint([row("a", mode="")]))

    def test_duplicates_matter(self):
        self.assertNotEqual(txn.fingerprint([row("a")]),
                            txn.fingerprint([row("a"), row("a")]))


# --------------------------------------------------------------------------
# Txn.run - success path
# --------------------------------------------------------------------------
def simple_manifest(fp="fp-new"):
    return {"schema_version": 2, "aggregate_fingerprint": fp, "installs": []}


class RunSuccessTests(SandboxCase):
    def test_writes_manifest_derived_and_removes_txn_dir(self):
        root = self.sandbox()
        t = make_txn(root)
        m = simple_manifest()
        tid = t.run([txn.Op("write", "hooks/a.sh", b"#!/bin/sh\n", "0755"),
                     txn.Op("write", "rules/r.md", b"rule", "0644")], m)
        self.assertEqual((root / "hooks/a.sh").read_bytes(), b"#!/bin/sh\n")
        self.assertEqual(txn.lstate(root / "hooks/a.sh")["mode"], "0755")
        self.assertEqual(txn.lstate(root / "rules/r.md")["mode"], "0644")
        on_disk = json.loads((root / MANIFEST).read_text())
        self.assertEqual(on_disk["txn_id"], tid)
        self.assertEqual(m["txn_id"], tid)   # run sets it on the caller's dict
        self.assertEqual(on_disk["aggregate_fingerprint"], "fp-new")
        self.assertEqual((root / DERIVED).read_bytes(), derived_bytes(m))
        self.assertFalse((root / TXNDIR).exists())
        self.assertEqual(t.pending(), [])
        leftovers = [n for n in os.listdir(root) if ".tmp-" in n]
        self.assertEqual(leftovers, [])

    def test_no_derived_configured(self):
        root = self.sandbox()
        t = make_txn(root, derived=False)
        t.run([txn.Op("write", "a", b"x", "0644")], simple_manifest())
        self.assertTrue((root / MANIFEST).exists())
        self.assertFalse((root / DERIVED).exists())

    def test_replace_delete_and_nested_dir_creation(self):
        root = self.sandbox()
        put(root, "hooks/a.sh", b"old", "0755")
        put(root, "rules/old.md", b"gone")
        t = make_txn(root)
        t.run([txn.Op("write", "hooks/a.sh", b"new", "0644"),
               txn.Op("delete", "rules/old.md"),
               txn.Op("write", "skills/deep/er/s.md", b"s", "0644")], simple_manifest())
        self.assertEqual((root / "hooks/a.sh").read_bytes(), b"new")
        self.assertEqual(txn.lstate(root / "hooks/a.sh")["mode"], "0644")
        self.assertFalse((root / "rules/old.md").exists())
        self.assertEqual((root / "skills/deep/er/s.md").read_bytes(), b"s")

    def test_mode_none_preserves_prior_mode_else_0644(self):
        root = self.sandbox()
        put(root, "settings.json", b"{}", "0600")
        make_txn(root).run([txn.Op("write", "settings.json", b'{"a":1}', None),
                            txn.Op("write", "fresh", b"x", None)], simple_manifest())
        self.assertEqual(txn.lstate(root / "settings.json")["mode"], "0600")
        self.assertEqual(txn.lstate(root / "fresh")["mode"], "0644")

    def test_delete_of_absent_is_noop(self):
        root = self.sandbox()
        make_txn(root).run([txn.Op("delete", "nothing/here")], simple_manifest())
        self.assertFalse((root / "nothing").exists())

    def test_manifest_replaces_prior_and_ids_differ(self):
        root = self.sandbox()
        t = make_txn(root)
        a = t.run([txn.Op("write", "a", b"1", "0644")], simple_manifest("fp1"))
        b = t.run([txn.Op("write", "a", b"2", "0644")], simple_manifest("fp2"))
        self.assertNotEqual(a, b)
        self.assertEqual(json.loads((root / MANIFEST).read_text())["txn_id"], b)
        self.assertEqual((root / DERIVED).read_bytes(),
                         derived_bytes(simple_manifest("fp2")))

    def test_explicit_txn_id_kwarg(self):
        root = self.sandbox()
        tid = make_txn(root).run([], simple_manifest(), txn_id="fixed-id-1")
        self.assertEqual(tid, "fixed-id-1")

    def test_empty_ops_still_commits_manifest(self):
        root = self.sandbox()
        make_txn(root).run([], simple_manifest())
        self.assertTrue((root / MANIFEST).exists())
        self.assertFalse((root / TXNDIR).exists())


class RunRefusalTests(SandboxCase):
    def assert_untouched(self, root, before):
        self.assertEqual(snapshot(root), before)

    def test_refuses_dir_or_symlink_at_dest_with_zero_writes(self):
        root = self.sandbox()
        (root / "d").mkdir()
        os.symlink("elsewhere", root / "l")
        put(root, "ok", b"old")
        before = snapshot(root)
        for dest in ("d", "l"):
            with self.subTest(dest=dest):
                with self.assertRaises(txn.InstallError) as cm:
                    make_txn(root).run([txn.Op("write", "ok", b"new", "0644"),
                                        txn.Op("write", dest, b"x", "0644")],
                                       simple_manifest())
                self.assertEqual(cm.exception.code, 3)
                self.assert_untouched(root, before)
        with self.assertRaises(txn.InstallError):
            make_txn(root).run([txn.Op("delete", "d")], simple_manifest())
        self.assert_untouched(root, before)

    def test_confine_violation_zero_writes(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        os.symlink(outside, root / "esc")
        put(root, "ok", b"old")
        before = snapshot(root)
        for dest in ("../x", "/abs", "esc/x", "a/../b"):
            with self.subTest(dest=dest), self.assertRaises(txn.ConfineError):
                make_txn(root).run([txn.Op("write", "ok", b"new", "0644"),
                                    txn.Op("write", dest, b"x", "0644")],
                                   simple_manifest())
            self.assert_untouched(root, before)
        self.assertEqual(os.listdir(outside), [])

    def test_bad_ops_rejected(self):
        root = self.sandbox()
        before = snapshot(root)
        bad = [
            [txn.Op("frob", "a", b"x", "0644")],
            [txn.Op("write", "a", "not-bytes", "0644")],
            [txn.Op("write", "a", None, "0644")],
            [txn.Op("write", "a", b"x", "644")],
            [txn.Op("write", "a", b"x", "0999")],
            [txn.Op("write", "a", b"x", "0644"), txn.Op("delete", "a")],
            [txn.Op("write", MANIFEST, b"x", "0644")],
            [txn.Op("write", DERIVED, b"x", "0644")],
            [txn.Op("write", LOCKNAME, b"x", "0644")],
            [txn.Op("write", TXNDIR + "/x", b"x", "0644")],
        ]
        for ops in bad:
            with self.subTest(ops=ops), self.assertRaises(txn.InstallError):
                make_txn(root).run(ops, simple_manifest())
            self.assert_untouched(root, before)

    def test_unserializable_manifest_fails_before_any_write(self):
        root = self.sandbox()
        before = snapshot(root)
        with self.assertRaises(Exception):
            make_txn(root).run([txn.Op("write", "a", b"x", "0644")], {"x": object()})
        self.assert_untouched(root, before)

    def test_refuses_when_a_txn_is_pending(self):
        root = self.sandbox()
        (root / TXNDIR / "stale-1").mkdir(parents=True)
        with self.assertRaises(txn.InstallError) as cm:
            make_txn(root).run([], simple_manifest())
        self.assertEqual(cm.exception.code, 6)

    def test_constructor_rejects_path_like_names(self):
        root = self.sandbox()
        for kw in ({"manifest_name": "a/b"}, {"txn_dirname": "../x"},
                   {"lockname": ""}):
            args = dict(manifest_name=MANIFEST, txn_dirname=TXNDIR, lockname=LOCKNAME)
            args.update(kw)
            with self.subTest(kw=kw), self.assertRaises(txn.InstallError):
                txn.Txn(root, **args)


class EnsureDerivedTests(SandboxCase):
    def test_regenerates_missing_and_different_but_not_equal(self):
        root = self.sandbox()
        t = make_txn(root)
        m = simple_manifest("fp-x")
        t.run([], m)
        want = derived_bytes(m)
        (root / DERIVED).unlink()
        t.ensure_derived()
        self.assertEqual((root / DERIVED).read_bytes(), want)
        (root / DERIVED).write_bytes(b"corrupt")
        t.ensure_derived()
        self.assertEqual((root / DERIVED).read_bytes(), want)
        ns = os.lstat(root / DERIVED).st_mtime_ns
        t.ensure_derived()
        self.assertEqual(os.lstat(root / DERIVED).st_mtime_ns, ns)
        self.assertEqual([n for n in os.listdir(root) if ".tmp-" in n], [])

    def test_noop_without_manifest_or_derived_config(self):
        root = self.sandbox()
        make_txn(root).ensure_derived()
        self.assertEqual(os.listdir(root), [])
        t = make_txn(root, derived=False)
        t.run([], simple_manifest())
        t.ensure_derived()
        self.assertFalse((root / DERIVED).exists())


# --------------------------------------------------------------------------
# Crash injection
# --------------------------------------------------------------------------
CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import txn
root = Path(sys.argv[2])
spec = json.loads(sys.argv[3])
t = txn.Txn(root, manifest_name=".m.json", txn_dirname=".txn", lockname=".lock",
            derived_name=".summary.json",
            derived_bytes=lambda m: json.dumps({"fp": m["aggregate_fingerprint"]},
                                               sort_keys=True).encode())
with txn.Lock(root, ".lock"):
    t.recover()
    if spec["action"] == "run":
        ops = [txn.Op(o["kind"], o["dest"],
                      o["data"].encode() if o.get("data") is not None else None,
                      o.get("mode")) for o in spec["ops"]]
        t.run(ops, spec["manifest"], txn_id=spec["txn_id"])
print("OK")
'''

PRE_POINTS = {"journal_written", "manifest_temp"}


def is_post(point):
    return point in {"manifest_renamed", "derived_temp", "derived_renamed",
                     "before_cleanup", "cleanup_renamed"}


def build_existing(root):
    put(root, MANIFEST, json.dumps({"txn_id": "prior-1", "aggregate_fingerprint": "fp-old"},
                                   indent=2, sort_keys=True).encode() + b"\n")
    put(root, DERIVED, derived_bytes({"aggregate_fingerprint": "fp-old"}))
    put(root, "keep.txt", b"keep")
    put(root, "hooks/a.sh", b"#!/bin/sh\nold\n", "0755")
    put(root, "hooks/b.sh", b"#!/bin/sh\nb\n", "0755")
    put(root, "rules/old.md", b"to be deleted")
    put(root, "settings.json", b'{"a": 1}', "0600")
    os.symlink("keep.txt", root / "unrelated-link")


EXISTING_OPS = [
    {"kind": "write", "dest": "skills/deep/nested/new.md", "data": "brand new\n", "mode": "0644"},
    {"kind": "write", "dest": "hooks/a.sh", "data": "#!/bin/sh\nnew\n", "mode": "0644"},
    {"kind": "delete", "dest": "rules/old.md"},
    {"kind": "write", "dest": "settings.json", "data": '{"a": 2}', "mode": None},
]

FRESH_OPS = [
    {"kind": "write", "dest": "hooks/a.sh", "data": "#!/bin/sh\n", "mode": "0755"},
    {"kind": "write", "dest": "skills/x/y/z.md", "data": "z", "mode": "0644"},
    {"kind": "write", "dest": "CLAUDE.md", "data": "top", "mode": "0644"},
]

SCENARIOS = {
    "existing": (build_existing, EXISTING_OPS),
    "fresh": (lambda root: None, FRESH_OPS),
}


def spec_for(name, action="run"):
    return json.dumps({"action": action, "ops": SCENARIOS[name][1],
                       "manifest": {"schema_version": 2, "aggregate_fingerprint": "fp-new"},
                       "txn_id": "crash-test-1"})


class CrashBase(SandboxCase):
    def child(self, root, spec, fault=None):
        env = dict(os.environ)
        env.pop("DEVKIT_INSTALL_FAULT", None)
        env.pop("DEVKIT_INSTALL_FAULT_MODE", None)
        if fault:
            env["DEVKIT_INSTALL_FAULT"] = fault
        return subprocess.run([sys.executable, "-c", CHILD, str(LIB), str(root), spec],
                              capture_output=True, text=True, env=env)

    def recover_here(self, root):
        with txn.Lock(root, LOCKNAME):
            return make_txn(root).recover()

    def states(self, scenario):
        """(pre_snapshot, post_snapshot) built from independent sandboxes."""
        build = SCENARIOS[scenario][0]
        a = self.sandbox(scenario + "-pre")
        build(a)
        pre = snapshot(a)
        b = self.sandbox(scenario + "-post")
        build(b)
        r = self.child(b, spec_for(scenario))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        post = snapshot(b)
        self.assertNotEqual(pre, post, "scenario must actually change the tree")
        self.assertNotIn(TXNDIR, post)
        return pre, post

    def crashed_sandbox(self, scenario, point, name):
        root = self.sandbox(name)
        SCENARIOS[scenario][0](root)
        r = self.child(root, spec_for(scenario), fault=point)
        self.assertEqual(r.returncode, 137,
                         "fault %r did not fire: rc=%s %s%s" % (point, r.returncode, r.stdout, r.stderr))
        return root


class FaultPointListTests(unittest.TestCase):
    def test_fault_points_shape(self):
        self.assertEqual(txn.FAULT_POINTS(3), [
            "journal_written", "after_op:0", "after_op:1", "after_op:2",
            "manifest_temp", "manifest_renamed", "derived_temp", "derived_renamed",
            "before_cleanup"])
        self.assertEqual(txn.FAULT_POINTS(0)[0:2], ["journal_written", "manifest_temp"])

    def test_recover_fault_points_shape(self):
        rb = txn.RECOVER_FAULT_POINTS(3)
        for i in range(3):
            self.assertIn("recover:after_restore:%d" % i, rb)
        self.assertIn("recover:after_dirs", rb)
        self.assertIn("recover:before_remove", rb)
        cm = txn.RECOVER_FAULT_POINTS(3, committed=True)
        self.assertIn("recover:after_derived", cm)
        self.assertIn("recover:before_remove", cm)
        self.assertNotIn("recover:after_restore:0", cm)


class CrashMatrixTests(CrashBase):
    def test_every_fault_point_recovers_to_pre_or_post_never_mixed(self):
        for scenario in SCENARIOS:
            n = len(SCENARIOS[scenario][1])
            pre, post = self.states(scenario)
            for point in txn.FAULT_POINTS(n):
                with self.subTest(scenario=scenario, point=point):
                    root = self.crashed_sandbox(scenario, point,
                                                "c-%s-%s" % (scenario, point.replace(":", "_")))
                    lines = self.recover_here(root)
                    want, other = (post, pre) if is_post(point) else (pre, post)
                    got = snapshot(root)
                    self.assertEqual(got, want,
                                     "recovered tree differs from expected %s state"
                                     % ("POST" if is_post(point) else "PRE"))
                    self.assertNotEqual(got, other)
                    self.assertTrue(lines, "recovery should report what it did")
                    self.assertEqual(make_txn(root).pending(), [])
                    # idempotent: second recover changes nothing and reports nothing
                    self.assertEqual(self.recover_here(root), [])
                    self.assertEqual(snapshot(root), want)

    def test_crash_at_cleanup_rename_is_post_and_recoverable(self):
        for scenario in SCENARIOS:
            pre, post = self.states(scenario)
            with self.subTest(scenario=scenario):
                root = self.crashed_sandbox(scenario, "cleanup_renamed", "cr-" + scenario)
                self.recover_here(root)
                self.assertEqual(snapshot(root), post)
                self.assertEqual(make_txn(root).pending(), [])

    def test_live_tree_untouched_until_journal_is_durable(self):
        pre, _ = self.states("existing")
        root = self.crashed_sandbox("existing", "journal_written", "jw")
        live = {k: v for k, v in snapshot(root).items()
                if k != TXNDIR and not k.startswith(TXNDIR + "/")}
        self.assertEqual(live, pre)

    def test_journal_and_backup_contents(self):
        root = self.crashed_sandbox("existing", "journal_written", "jc")
        dirs = sorted(os.listdir(root / TXNDIR))
        self.assertEqual(dirs, ["crash-test-1"])
        d = root / TXNDIR / "crash-test-1"
        j = json.loads((d / "journal.json").read_text())
        self.assertEqual(j["txn_id"], "crash-test-1")
        self.assertEqual(j["manifest_name"], MANIFEST)
        self.assertEqual(j["derived_name"], DERIVED)
        self.assertEqual([o["i"] for o in j["ops"]], [0, 1, 2, 3])
        self.assertEqual([o["kind"] for o in j["ops"]], ["write", "write", "delete", "write"])
        self.assertEqual(j["ops"][0]["dest"], "skills/deep/nested/new.md")
        self.assertEqual(j["ops"][0]["prior"]["type"], "absent")
        self.assertEqual(j["ops"][1]["prior"]["type"], "file")
        self.assertEqual(j["ops"][1]["prior"]["mode"], "0755")
        self.assertEqual(j["ops"][1]["new_mode"], "0644")
        self.assertEqual(j["ops"][1]["new_sha"],
                         hashlib.sha256(b"#!/bin/sh\nnew\n").hexdigest())
        self.assertEqual(j["ops"][3]["prior"]["mode"], "0600")
        self.assertEqual(j["ops"][3]["new_mode"], "0600")   # None => preserved
        # full prior bytes of every replaced / deleted file are backed up
        self.assertEqual((d / j["ops"][1]["prior"]["backup"]).read_bytes(), b"#!/bin/sh\nold\n")
        self.assertEqual((d / j["ops"][2]["prior"]["backup"]).read_bytes(), b"to be deleted")
        self.assertEqual((d / j["ops"][3]["prior"]["backup"]).read_bytes(), b'{"a": 1}')
        self.assertNotIn("backup", j["ops"][0]["prior"])
        self.assertEqual((d / "new" / "1").read_bytes(), b"#!/bin/sh\nnew\n")
        self.assertEqual(j["created_dirs"], ["skills", "skills/deep", "skills/deep/nested"])
        # the payload names inside journal never escape the txn dir
        for o in j["ops"]:
            b = o["prior"].get("backup")
            if b:
                self.assertFalse(os.path.isabs(b) or ".." in b.split("/"))

    def test_crash_mid_apply_leaves_partial_live_tree_until_recovery(self):
        # sanity that the harness really tests a *mixed* intermediate state
        pre, post = self.states("existing")
        root = self.crashed_sandbox("existing", "after_op:1", "mid")
        live = {k: v for k, v in snapshot(root).items()
                if k != TXNDIR and not k.startswith(TXNDIR + "/")}
        self.assertNotEqual(live, pre)
        self.assertNotEqual(live, post)
        self.recover_here(root)
        self.assertEqual(snapshot(root), pre)

    def test_crash_during_recovery_is_recoverable(self):
        for scenario in SCENARIOS:
            n = len(SCENARIOS[scenario][1])
            pre, post = self.states(scenario)
            for point in txn.FAULT_POINTS(n):
                committed = is_post(point)
                for rpoint in txn.RECOVER_FAULT_POINTS(n, committed=committed):
                    with self.subTest(scenario=scenario, point=point, recover_point=rpoint):
                        name = "cr-%s-%s-%s" % (scenario, point, rpoint)
                        root = self.crashed_sandbox(scenario, point, name.replace(":", "_"))
                        r = self.child(root, spec_for(scenario, "recover"), fault=rpoint)
                        self.assertEqual(r.returncode, 137,
                                         "recover fault %r did not fire: %s%s"
                                         % (rpoint, r.stdout, r.stderr))
                        self.recover_here(root)
                        self.assertEqual(snapshot(root), post if committed else pre)
                        self.assertEqual(make_txn(root).pending(), [])
                        self.assertEqual(self.recover_here(root), [])

    def test_crash_during_recovery_cleanup_rename(self):
        pre, post = self.states("existing")
        for point, want in (("after_op:2", pre), ("manifest_renamed", post)):
            with self.subTest(point=point):
                root = self.crashed_sandbox("existing", point, "rc-" + point.replace(":", "_"))
                r = self.child(root, spec_for("existing", "recover"), fault="cleanup_renamed")
                self.assertEqual(r.returncode, 137)
                self.recover_here(root)
                self.assertEqual(snapshot(root), want)
                self.assertEqual(make_txn(root).pending(), [])

    def test_repeated_crash_and_recover_cycles_converge(self):
        pre, post = self.states("existing")
        root = self.crashed_sandbox("existing", "after_op:2", "cycle")
        for rp in ("recover:after_restore:3", "recover:after_restore:1", "recover:after_dirs"):
            r = self.child(root, spec_for("existing", "recover"), fault=rp)
            self.assertEqual(r.returncode, 137, rp + r.stdout + r.stderr)
        self.recover_here(root)
        self.assertEqual(snapshot(root), pre)

    def test_pending_reports_txn_dir_until_recovered(self):
        root = self.crashed_sandbox("existing", "after_op:0", "pend")
        p = make_txn(root).pending()
        self.assertEqual([x.name for x in p], ["crash-test-1"])
        self.recover_here(root)
        self.assertEqual(make_txn(root).pending(), [])

    def test_run_after_recover_succeeds(self):
        pre, post = self.states("existing")
        root = self.crashed_sandbox("existing", "after_op:1", "again")
        self.recover_here(root)
        r = self.child(root, spec_for("existing"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(snapshot(root), post)


class InProcessFaultTests(SandboxCase):
    def test_raise_mode_raises_injected_crash_and_recover_restores(self):
        root = self.sandbox()
        put(root, "a", b"old", "0640")
        before = snapshot(root)
        with mock.patch.dict(os.environ, {"DEVKIT_INSTALL_FAULT": "after_op:0",
                                          "DEVKIT_INSTALL_FAULT_MODE": "raise"}):
            with self.assertRaises(txn.InjectedCrash):
                make_txn(root).run([txn.Op("write", "a", b"new", "0644")], simple_manifest())
        self.assertNotEqual(snapshot(root), before)
        self.assertEqual((root / "a").read_bytes(), b"new")
        make_txn(root).recover()
        self.assertEqual(snapshot(root), before)

    def test_fault_env_unset_or_other_point_is_inert(self):
        root = self.sandbox()
        with mock.patch.dict(os.environ, {"DEVKIT_INSTALL_FAULT": "after_op:9",
                                          "DEVKIT_INSTALL_FAULT_MODE": "raise"}):
            make_txn(root).run([txn.Op("write", "a", b"x", "0644")], simple_manifest())
        self.assertTrue((root / MANIFEST).exists())


class RecoverEdgeTests(SandboxCase):
    def test_no_journal_means_nothing_applied_and_dir_is_deleted(self):
        root = self.sandbox()
        put(root, "a", b"untouched")
        d = root / TXNDIR / "half-1"
        (d / "new").mkdir(parents=True)
        (d / "new" / "0").write_bytes(b"partial payload")
        (d / "journal.json.tmp").write_bytes(b"{partial")
        before = {k: v for k, v in snapshot(root).items()
                  if k != TXNDIR and not k.startswith(TXNDIR + "/")}
        lines = make_txn(root).recover()
        self.assertTrue(lines)
        self.assertEqual(snapshot(root), before)
        self.assertFalse((root / TXNDIR).exists())

    def test_gone_dirs_are_swept(self):
        root = self.sandbox()
        (root / TXNDIR / ".gone-x" / "backup").mkdir(parents=True)
        (root / TXNDIR / ".gone-x" / "backup" / "0").write_bytes(b"x")
        self.assertEqual(len(make_txn(root).pending()), 1)
        make_txn(root).recover()
        self.assertFalse((root / TXNDIR).exists())

    def test_recover_with_no_txn_dir_is_noop(self):
        root = self.sandbox()
        put(root, "a", b"x")
        before = snapshot(root)
        self.assertEqual(make_txn(root).recover(), [])
        self.assertEqual(snapshot(root), before)

    def test_committed_recovery_removes_stale_temps_and_regenerates_derived(self):
        root = self.sandbox()
        with mock.patch.dict(os.environ, {"DEVKIT_INSTALL_FAULT": "derived_temp",
                                          "DEVKIT_INSTALL_FAULT_MODE": "raise"}):
            with self.assertRaises(txn.InjectedCrash):
                make_txn(root).run([txn.Op("write", "a", b"x", "0644")], simple_manifest("fp9"))
        self.assertTrue([n for n in os.listdir(root) if n.startswith(DERIVED + ".tmp-")])
        self.assertFalse((root / DERIVED).exists())
        make_txn(root).recover()
        self.assertEqual((root / DERIVED).read_bytes(), derived_bytes(simple_manifest("fp9")))
        self.assertEqual([n for n in os.listdir(root) if ".tmp-" in n], [])
        self.assertFalse((root / TXNDIR).exists())

    def test_rollback_leaves_created_dir_that_is_no_longer_empty(self):
        root = self.sandbox()
        with mock.patch.dict(os.environ, {"DEVKIT_INSTALL_FAULT": "after_op:0",
                                          "DEVKIT_INSTALL_FAULT_MODE": "raise"}):
            with self.assertRaises(txn.InjectedCrash):
                make_txn(root).run([txn.Op("write", "sk/deep/n.md", b"x", "0644")],
                                   simple_manifest())
        (root / "sk" / "deep" / "user-file").write_bytes(b"not ours")
        lines = make_txn(root).recover()
        self.assertFalse((root / "sk/deep/n.md").exists())
        self.assertEqual((root / "sk/deep/user-file").read_bytes(), b"not ours")
        self.assertTrue(any("not empty" in l for l in lines), lines)
        self.assertFalse((root / TXNDIR).exists())

    def test_journal_with_escaping_dest_is_refused_not_followed(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        (outside / "victim").write_bytes(b"precious")
        d = root / TXNDIR / "evil-1"
        d.mkdir(parents=True)
        (d / "journal.json").write_text(json.dumps({
            "txn_id": "evil-1", "manifest_name": MANIFEST, "derived_name": DERIVED,
            "created_dirs": [],
            "ops": [{"i": 0, "kind": "write", "dest": "../outside/victim",
                     "new_sha": "0" * 64, "new_mode": "0644",
                     "prior": {"type": "absent", "mode": None, "sha256": None}}]}))
        with self.assertRaises(txn.ConfineError):
            make_txn(root).recover()
        self.assertEqual((outside / "victim").read_bytes(), b"precious")

    def test_corrupt_journal_is_an_error_not_silently_deleted(self):
        root = self.sandbox()
        d = root / TXNDIR / "bad-1"
        d.mkdir(parents=True)
        (d / "journal.json").write_text("{not json")
        with self.assertRaises(txn.InstallError):
            make_txn(root).recover()
        self.assertTrue((d / "journal.json").exists())

    def test_backup_tamper_detected_before_restore(self):
        root = self.sandbox()
        put(root, "a", b"old")
        with mock.patch.dict(os.environ, {"DEVKIT_INSTALL_FAULT": "after_op:0",
                                          "DEVKIT_INSTALL_FAULT_MODE": "raise"}):
            with self.assertRaises(txn.InjectedCrash):
                make_txn(root).run([txn.Op("write", "a", b"new", "0644")], simple_manifest())
        (d,) = make_txn(root).pending()
        (d / "backup" / "0").write_bytes(b"tampered")
        with self.assertRaises(txn.InstallError):
            make_txn(root).recover()
        self.assertEqual((root / "a").read_bytes(), b"new")   # nothing half-restored

    def test_txn_lock_helper_uses_configured_lockname(self):
        root = self.sandbox()
        t = make_txn(root)
        with t.lock():
            self.assertTrue((root / LOCKNAME).exists())
            with self.assertRaises(txn.Locked):
                with t.lock():
                    pass


class OrdinaryFailureTests(SandboxCase):
    """A plain exception (disk full, EIO, ...) mid-run must not leave a mixed tree."""

    def _fail_replace_for(self, needle, exc=None):
        real = os.replace
        exc = exc or OSError(28, "No space left on device")
        fired = []

        def flaky(src, dst, *a, **k):
            # match the destination's final component: live-tree replaces are
            # directory-fd-relative (bare names), journal replaces are full paths
            if os.path.basename(str(dst)) == needle and not fired:   # transient: fails exactly once
                fired.append(dst)
                raise exc
            return real(src, dst, *a, **k)
        return mock.patch("os.replace", flaky)

    def test_failure_during_apply_rolls_back_in_process(self):
        root = self.sandbox()
        put(root, "a", b"old-a", "0640")
        put(root, "b", b"old-b", "0755")
        before = snapshot(root)
        ops = [txn.Op("write", "a", b"new-a", "0644"),
               txn.Op("write", "sk/deep/c", b"new-c", "0644"),
               txn.Op("write", "b", b"new-b", "0644")]
        with self._fail_replace_for("b"):
            with self.assertRaises(OSError):
                make_txn(root).run(ops, simple_manifest())
        self.assertEqual(snapshot(root), before)
        self.assertEqual(make_txn(root).pending(), [])

    def test_failure_after_commit_leaves_committed_state_and_no_pending(self):
        root = self.sandbox()
        put(root, "a", b"old-a")
        with self._fail_replace_for(DERIVED):
            with self.assertRaises(OSError):
                make_txn(root).run([txn.Op("write", "a", b"new-a", "0644")],
                                   simple_manifest("fp-c"))
        self.assertEqual((root / "a").read_bytes(), b"new-a")
        self.assertEqual(json.loads((root / MANIFEST).read_text())["aggregate_fingerprint"], "fp-c")
        self.assertEqual(make_txn(root).pending(), [])
        self.assertEqual((root / DERIVED).read_bytes(), derived_bytes(simple_manifest("fp-c")))

    def test_failure_while_journaling_leaves_no_pending_and_live_tree_untouched(self):
        root = self.sandbox()
        put(root, "a", b"old-a")
        before = snapshot(root)
        with self._fail_replace_for("journal.json"):
            with self.assertRaises(OSError):
                make_txn(root).run([txn.Op("write", "a", b"new-a", "0644")], simple_manifest())
        self.assertEqual(snapshot(root), before)
        self.assertEqual(make_txn(root).pending(), [])

    def test_injected_crash_is_not_swallowed_by_the_in_process_rollback(self):
        # a simulated kill must stay a kill: the tree keeps its mixed state for recover()
        root = self.sandbox()
        put(root, "a", b"old")
        with mock.patch.dict(os.environ, {"DEVKIT_INSTALL_FAULT": "after_op:0",
                                          "DEVKIT_INSTALL_FAULT_MODE": "raise"}):
            with self.assertRaises(txn.InjectedCrash):
                make_txn(root).run([txn.Op("write", "a", b"new", "0644")], simple_manifest())
        self.assertEqual((root / "a").read_bytes(), b"new")
        self.assertEqual(len(make_txn(root).pending()), 1)


class ApplyTimeConfinementTests(SandboxCase):
    def test_directory_swapped_for_symlink_after_preflight_is_not_followed(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        real_fault = txn._fault

        def swap(point):
            if point == "journal_written":      # preflight is over, apply not started
                os.rmdir(root / "hooks") if (root / "hooks").is_dir() else None
                os.symlink(outside, root / "hooks")
            return real_fault(point)
        with mock.patch.object(txn, "_fault", swap):
            with self.assertRaises(txn.ConfineError):
                make_txn(root).run([txn.Op("write", "hooks/a.sh", b"x", "0755")],
                                   simple_manifest())
        self.assertEqual(os.listdir(outside), [])
        self.assertFalse((root / MANIFEST).exists())


# --------------------------------------------------------------------------
# Hardening: fd-relative confinement, state-aware roll-back,
# compare-and-swap, lock hardening, stray-temp sweeping
# --------------------------------------------------------------------------
def crash_in_process(root, ops, point, manifest=None):
    """Run a txn in this process and die (InjectedCrash) at `point`."""
    with mock.patch.dict(os.environ, {"DEVKIT_INSTALL_FAULT": point,
                                      "DEVKIT_INSTALL_FAULT_MODE": "raise"}):
        with unittest.TestCase().assertRaises(txn.InjectedCrash):
            make_txn(root).run(ops, manifest or simple_manifest())


def swap_dir_for_symlink(root, name, target, keep_as):
    os.rename(root / name, root / keep_as)
    os.symlink(target, root / name)


class FdRelativeConfinementTests(SandboxCase):
    """confine() must not check a pathname that the write later re-resolves."""

    def test_parent_swapped_for_symlink_after_the_final_confine_check(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        put(root, "hooks/keep", b"k")
        real_confine = txn.confine
        calls, swapped = [], []

        def confine_then_swap(r, rel):
            res = real_confine(r, rel)
            if rel == "hooks/a.sh":
                calls.append(rel)
                if len(calls) == 2 and not swapped:   # 1st = preflight, 2nd = apply-time check
                    swap_dir_for_symlink(root, "hooks", outside, "hooks.orig")
                    swapped.append(1)
            return res
        with mock.patch.object(txn, "confine", confine_then_swap):
            with self.assertRaises(txn.ConfineError):
                make_txn(root).run([txn.Op("write", "hooks/a.sh", b"x", "0755")],
                                   simple_manifest())
        self.assertTrue(swapped, "the swap seam was never reached (test is vacuous)")
        self.assertEqual(os.listdir(outside), [], "a file landed outside the root")
        self.assertFalse((root / MANIFEST).exists())

    def test_parent_swapped_after_its_directory_fd_was_pinned_lands_inside_root(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        put(root, "hooks/keep", b"k")
        swapped = []

        def swap_once():
            if not swapped:
                swap_dir_for_symlink(root, "hooks", outside, "hooks.orig")
                swapped.append(1)
        real_write_tmp = txn._write_tmp

        def write_tmp_seam(tmp, *a, **k):     # swap here too, in case a write goes through _write_tmp
            if Path(tmp).parent == root / "hooks":
                swap_once()
            return real_write_tmp(tmp, *a, **k)

        def seam(point):                       # fd-relative writers reach it here
            if point == "write:parent_open":
                swap_once()
        with mock.patch.object(txn, "_write_tmp", write_tmp_seam), \
                mock.patch.object(txn, "_seam", seam, create=True):
            try:
                make_txn(root).run([txn.Op("write", "hooks/a.sh", b"x", "0755")],
                                   simple_manifest())
            except txn.InstallError:
                pass
        self.assertTrue(swapped, "the swap seam was never reached (test is vacuous)")
        self.assertEqual(os.listdir(outside), [], "a file landed outside the root")
        self.assertEqual((root / "hooks.orig" / "a.sh").read_bytes(), b"x")

    def test_directory_created_by_the_txn_is_swapped_before_its_first_write(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        (outside / "deep").mkdir()      # so a followed symlink really would land a file
        swapped = []
        real_confine = txn.confine
        n = []

        def confine_then_swap(r, rel):
            res = real_confine(r, rel)
            if rel == "sk/deep/f":
                n.append(1)
                if len(n) == 2 and not swapped:
                    swap_dir_for_symlink(root, "sk", outside, "sk.orig")
                    swapped.append(1)
            return res
        with mock.patch.object(txn, "confine", confine_then_swap):
            with self.assertRaises(txn.InstallError):
                make_txn(root).run([txn.Op("write", "sk/deep/f", b"x", "0644")],
                                   simple_manifest())
        self.assertTrue(swapped)
        self.assertEqual(os.listdir(outside / "deep"), [])

    def test_rollback_never_follows_a_symlinked_ancestor(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        (outside / "a.sh").write_bytes(b"precious")
        put(root, "hooks/a.sh", b"old")
        crash_in_process(root, [txn.Op("write", "hooks/a.sh", b"new", "0644")], "after_op:0")
        swap_dir_for_symlink(root, "hooks", outside, "hooks.orig")
        try:
            make_txn(root).recover()
        except txn.InstallError:
            pass
        self.assertEqual((outside / "a.sh").read_bytes(), b"precious")
        self.assertEqual(os.listdir(outside), ["a.sh"])

    def test_normal_nested_write_delete_and_root_symlink_still_work(self):
        real = self.sandbox("real-root")
        link = self.tmp / "root-link"
        os.symlink(real, link)
        put(real, "d/old", b"o")
        make_txn(link).run([txn.Op("write", "d/e/f/g", b"g", "0640"),
                            txn.Op("delete", "d/old")], simple_manifest())
        self.assertEqual((real / "d/e/f/g").read_bytes(), b"g")
        self.assertEqual(txn.lstate(real / "d/e/f/g")["mode"], "0640")
        self.assertFalse((real / "d/old").exists())
        self.assertEqual(txn.lstate(real / "d/e")["mode"], "0755")


class StateAwareRollbackTests(SandboxCase):
    """Roll-back must never overwrite/delete what the txn did not write."""

    def test_outside_edit_to_untouched_settings_between_crash_and_recover_is_kept(self):
        root = self.sandbox()
        put(root, "a", b"old-a", "0644")
        put(root, "settings.json", b'{"permissions":"v1"}', "0600")
        crash_in_process(root, [txn.Op("write", "a", b"new-a", "0644"),
                                txn.Op("write", "settings.json", b'{"permissions":"merged"}', None)],
                         "after_op:0")
        (root / "settings.json").write_bytes(b'{"permissions":"v2-user-added-rule"}')
        lines = make_txn(root).recover()
        self.assertEqual((root / "settings.json").read_bytes(),
                         b'{"permissions":"v2-user-added-rule"}')
        self.assertEqual((root / "a").read_bytes(), b"old-a")       # our own change is undone
        self.assertTrue(any("settings.json" in l and "untouched" in l for l in lines), lines)
        self.assertEqual(make_txn(root).pending(), [])

    def test_file_created_at_an_absent_dest_after_the_crash_is_kept(self):
        root = self.sandbox()
        crash_in_process(root, [txn.Op("write", "settings.json", b"{}", "0600")],
                         "journal_written")
        (root / "settings.json").write_bytes(b'{"made":"by-the-user"}')
        lines = make_txn(root).recover()
        self.assertEqual((root / "settings.json").read_bytes(), b'{"made":"by-the-user"}')
        self.assertTrue(any("settings.json" in l and "untouched" in l for l in lines), lines)

    def test_file_we_created_is_still_removed_when_it_is_exactly_what_we_wrote(self):
        root = self.sandbox()
        crash_in_process(root, [txn.Op("write", "n/new.md", b"ours", "0644")], "after_op:0")
        self.assertTrue((root / "n/new.md").exists())
        make_txn(root).recover()
        self.assertFalse((root / "n").exists())

    def test_deleted_file_recreated_by_someone_else_is_kept(self):
        root = self.sandbox()
        put(root, "gone.md", b"original")
        put(root, "b", b"b0")
        crash_in_process(root, [txn.Op("write", "b", b"b1", "0644"),
                                txn.Op("delete", "gone.md")], "after_op:1")
        self.assertFalse((root / "gone.md").exists())
        (root / "gone.md").write_bytes(b"someone else made this")
        lines = make_txn(root).recover()
        self.assertEqual((root / "gone.md").read_bytes(), b"someone else made this")
        self.assertEqual((root / "b").read_bytes(), b"b0")
        self.assertTrue(any("gone.md" in l for l in lines))

    def test_deleted_file_still_absent_is_restored(self):
        root = self.sandbox()
        put(root, "gone.md", b"original", "0640")
        crash_in_process(root, [txn.Op("delete", "gone.md")], "after_op:0")
        make_txn(root).recover()
        self.assertEqual((root / "gone.md").read_bytes(), b"original")
        self.assertEqual(txn.lstate(root / "gone.md")["mode"], "0640")

    def test_file_whose_mode_was_changed_after_the_crash_is_kept(self):
        root = self.sandbox()
        put(root, "a", b"old", "0644")
        crash_in_process(root, [txn.Op("write", "a", b"new", "0644")], "after_op:0")
        os.chmod(root / "a", 0o600)
        make_txn(root).recover()
        self.assertEqual((root / "a").read_bytes(), b"new")
        self.assertEqual(txn.lstate(root / "a")["mode"], "0600")

    def test_created_file_whose_mode_was_changed_after_the_crash_is_kept(self):
        root = self.sandbox()
        crash_in_process(root, [txn.Op("write", "n/new.md", b"ours", "0644")], "after_op:0")
        os.chmod(root / "n/new.md", 0o600)      # same bytes, someone changed the mode
        lines = make_txn(root).recover()
        self.assertTrue((root / "n/new.md").exists(), "a mode-changed file we created was deleted")
        self.assertEqual((root / "n/new.md").read_bytes(), b"ours")
        self.assertEqual(txn.lstate(root / "n/new.md")["mode"], "0600")
        self.assertTrue(any("new.md" in l and "untouched" in l for l in lines), lines)
        self.assertEqual(make_txn(root).pending(), [])

    def test_a_directory_where_a_file_was_journaled_is_reported_not_raised(self):
        root = self.sandbox()
        crash_in_process(root, [txn.Op("write", "x", b"1", "0644")], "journal_written")
        (root / "x").mkdir()
        lines = make_txn(root).recover()
        self.assertTrue((root / "x").is_dir())
        self.assertTrue(any("x" in l and "untouched" in l for l in lines), lines)
        self.assertEqual(make_txn(root).pending(), [])

    def test_recover_is_idempotent_after_a_foreign_edit_was_kept(self):
        root = self.sandbox()
        put(root, "s", b"v1")
        crash_in_process(root, [txn.Op("write", "s", b"v-txn", "0644")], "journal_written")
        (root / "s").write_bytes(b"v2")
        make_txn(root).recover()
        self.assertEqual(make_txn(root).recover(), [])
        self.assertEqual((root / "s").read_bytes(), b"v2")


class CompareAndSwapTests(SandboxCase):
    """Op.expect is the planner's view of the live prior state."""

    @staticmethod
    def expect_of(path):
        st = txn.lstate(path)
        return {"type": st["type"], "sha256": st["sha256"], "mode": st["mode"]}

    def test_preflight_refuses_with_code_3_and_zero_writes_when_live_differs(self):
        root = self.sandbox()
        put(root, "settings.json", b"v1", "0600")
        put(root, "other", b"o")
        exp = self.expect_of(root / "settings.json")
        (root / "settings.json").write_bytes(b"v2-concurrent")
        before = snapshot(root)
        with self.assertRaises(txn.InstallError) as cm:
            make_txn(root).run([txn.Op("write", "other", b"o2", "0644"),
                                txn.Op("write", "settings.json", b"merged", None, exp)],
                               simple_manifest())
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(snapshot(root), before)
        self.assertFalse((root / TXNDIR).exists())

    def test_absent_expectation_refuses_a_file_that_appeared(self):
        root = self.sandbox()
        exp = {"type": "absent", "sha256": None, "mode": None}
        put(root, "seed.json", b"written by another process")
        before = snapshot(root)
        with self.assertRaises(txn.InstallError) as cm:
            make_txn(root).run([txn.Op("write", "seed.json", b"seed", "0644", exp)],
                               simple_manifest())
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(snapshot(root), before)

    def test_delete_with_stale_expectation_is_refused_and_file_kept(self):
        root = self.sandbox()
        put(root, "old.sh", b"v1")
        exp = self.expect_of(root / "old.sh")
        (root / "old.sh").write_bytes(b"user edit")
        with self.assertRaises(txn.InstallError) as cm:
            make_txn(root).run([txn.Op("delete", "old.sh", expect=exp)], simple_manifest())
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual((root / "old.sh").read_bytes(), b"user edit")

    def test_mode_drift_is_a_mismatch_too(self):
        root = self.sandbox()
        put(root, "f", b"same", "0644")
        exp = self.expect_of(root / "f")
        os.chmod(root / "f", 0o600)
        with self.assertRaises(txn.InstallError) as cm:
            make_txn(root).run([txn.Op("write", "f", b"new", None, exp)], simple_manifest())
        self.assertEqual(cm.exception.code, 3)

    def test_matching_expectations_apply_normally(self):
        root = self.sandbox()
        put(root, "f", b"v1", "0600")
        make_txn(root).run([txn.Op("write", "f", b"v2", None, self.expect_of(root / "f")),
                            txn.Op("write", "n", b"n", "0644",
                                   {"type": "absent", "sha256": None, "mode": None})],
                           simple_manifest())
        self.assertEqual((root / "f").read_bytes(), b"v2")
        self.assertEqual((root / "n").read_bytes(), b"n")

    def test_op_without_expect_is_applied_unconditionally(self):
        root = self.sandbox()
        put(root, "f", b"v1")
        make_txn(root).run([txn.Op("write", "f", b"v2", "0644")], simple_manifest())
        self.assertEqual((root / "f").read_bytes(), b"v2")
        self.assertIsNone(txn.Op("write", "f", b"x").expect)

    def test_malformed_expect_is_rejected_before_any_write(self):
        root = self.sandbox()
        put(root, "f", b"v1")
        before = snapshot(root)
        for bad in ({"type": "bogus"}, {"type": "file"}, "file", {}):
            with self.subTest(bad=bad), self.assertRaises(txn.InstallError):
                make_txn(root).run([txn.Op("write", "f", b"v2", "0644", bad)],
                                   simple_manifest())
            self.assertEqual(snapshot(root), before)

    def _concurrent_writer(self, root, at, rel, data):
        real = txn._fault

        def fault(point):
            if point == at:
                (root / rel).write_bytes(data)
            return real(point)
        return mock.patch.object(txn, "_fault", fault)

    def test_writer_landing_after_the_journal_aborts_into_rollback_and_is_preserved(self):
        root = self.sandbox()
        put(root, "settings.json", b'{"permissions":"v1"}', "0600")
        put(root, "hooks-a", b"a0")
        ops = [txn.Op("write", "hooks-a", b"a1", "0644", self.expect_of(root / "hooks-a")),
               txn.Op("write", "settings.json", b'{"permissions":"merge-of-v1"}', None,
                      self.expect_of(root / "settings.json"))]
        before_manifest = (root / MANIFEST).exists()
        with self._concurrent_writer(root, "after_op:0", "settings.json",
                                     b'{"permissions":"v2-concurrent"}'):
            with self.assertRaises(txn.InstallError) as cm:
                make_txn(root).run(ops, simple_manifest())
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual((root / "settings.json").read_bytes(),
                         b'{"permissions":"v2-concurrent"}')
        self.assertEqual((root / "hooks-a").read_bytes(), b"a0")     # applied op undone
        self.assertEqual((root / MANIFEST).exists(), before_manifest)
        self.assertEqual(make_txn(root).pending(), [])

    def test_writer_landing_between_journal_and_the_first_op_is_caught(self):
        root = self.sandbox()
        put(root, "settings.json", b"v1", "0600")
        ops = [txn.Op("write", "settings.json", b"merge-of-v1", None,
                      self.expect_of(root / "settings.json"))]
        with self._concurrent_writer(root, "journal_written", "settings.json", b"v2-concurrent"):
            with self.assertRaises(txn.InstallError) as cm:
                make_txn(root).run(ops, simple_manifest())
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual((root / "settings.json").read_bytes(), b"v2-concurrent")
        self.assertFalse((root / MANIFEST).exists())

    def test_file_created_by_another_writer_between_plan_and_apply_is_not_overwritten(self):
        root = self.sandbox()
        absent = {"type": "absent", "sha256": None, "mode": None}
        with self._concurrent_writer(root, "journal_written", "data.json", b"concurrent data"):
            with self.assertRaises(txn.InstallError) as cm:
                make_txn(root).run([txn.Op("write", "data.json", b"seed", "0644", absent)],
                                   simple_manifest())
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual((root / "data.json").read_bytes(), b"concurrent data")


class PendingAndLockHardeningTests(SandboxCase):
    def test_pending_tolerates_the_txn_dir_vanishing_between_stat_and_listdir(self):
        root = self.sandbox()
        (root / TXNDIR).mkdir()
        real = os.listdir

        def racing(path=".", *a, **k):
            if str(path) == str(root / TXNDIR):
                raise FileNotFoundError(2, "gone", str(path))
            return real(path, *a, **k)
        with mock.patch("os.listdir", racing):
            self.assertEqual(make_txn(root).pending(), [])

        def not_a_dir(path=".", *a, **k):
            if str(path) == str(root / TXNDIR):
                raise NotADirectoryError(20, "not a dir", str(path))
            return real(path, *a, **k)
        with mock.patch("os.listdir", not_a_dir):
            self.assertEqual(make_txn(root).pending(), [])

    def test_shared_locks_coexist_and_exclude_the_exclusive_lock(self):
        root = self.sandbox()
        with txn.Lock(root, LOCKNAME):
            pass                                   # creates the lock file
        with txn.Lock(root, LOCKNAME, shared=True) as a:
            with txn.Lock(root, LOCKNAME, shared=True) as b:
                self.assertTrue(a.held and b.held)
                with self.assertRaises(txn.Locked):
                    with txn.Lock(root, LOCKNAME):
                        pass
        with txn.Lock(root, LOCKNAME):             # released again
            with self.assertRaises(txn.Locked) as cm:
                with txn.Lock(root, LOCKNAME, shared=True):
                    pass
            self.assertEqual(cm.exception.code, 4)

    def test_shared_lock_from_another_process_is_excluded_by_an_exclusive_holder(self):
        root = self.sandbox()
        child = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport txn\n"
                 "try:\n    with txn.Lock(sys.argv[2], '.lock', shared=True):\n"
                 "        print('ACQUIRED')\nexcept txn.Locked:\n    print('LOCKED')\n")
        run = lambda: subprocess.run([sys.executable, "-c", child, str(LIB), str(root)],
                                     capture_output=True, text=True)
        with txn.Lock(root, LOCKNAME):
            self.assertIn("LOCKED", run().stdout)
        with txn.Lock(root, LOCKNAME, shared=True):
            self.assertIn("ACQUIRED", run().stdout)

    def test_shared_lock_never_creates_the_lock_file(self):
        root = self.sandbox()
        before = snapshot(root, exclude=())
        with txn.Lock(root, LOCKNAME, shared=True) as lk:
            self.assertFalse(lk.held)
        self.assertEqual(snapshot(root, exclude=()), before)
        with txn.Lock(self.tmp / "no-such-root", LOCKNAME, shared=True) as lk:
            self.assertFalse(lk.held)

    def test_unlinked_lock_file_cannot_break_exclusion(self):
        root = self.sandbox()
        real_flock = txn.fcntl.flock
        calls = []

        def flock_then_unlink(fd, op):
            r = real_flock(fd, op)
            calls.append(op)
            if len(calls) == 1:           # another process unlinks the lock file we just locked
                os.unlink(root / LOCKNAME)
            return r
        with mock.patch.object(txn.fcntl, "flock", flock_then_unlink):
            lk = txn.Lock(root, LOCKNAME).acquire()
        try:
            self.assertTrue((root / LOCKNAME).exists(), "lock path must exist after acquire")
            self.assertEqual(os.fstat(lk._fd).st_ino, os.lstat(root / LOCKNAME).st_ino)
            with self.assertRaises(txn.Locked):
                txn.Lock(root, LOCKNAME).acquire()
        finally:
            lk.release()

    def test_lock_that_keeps_being_replaced_gives_up_with_an_install_error(self):
        root = self.sandbox()
        real_flock = txn.fcntl.flock

        def always_unlink(fd, op):
            r = real_flock(fd, op)
            if op & txn.fcntl.LOCK_UN == 0:
                os.unlink(root / LOCKNAME)
            return r
        with mock.patch.object(txn.fcntl, "flock", always_unlink):
            with self.assertRaises(txn.InstallError):
                txn.Lock(root, LOCKNAME).acquire()


class SharedLockAbsentRaceTests(SandboxCase):
    """A shared reader that saw no lock file must not proceed unlocked while a
    writer creates + exclusively locks it right after."""

    def _writer_appears_after_failed_open(self, root, writers):
        def seam(point):
            if point == "lock:shared_absent" and not writers:
                writers.append(txn.Lock(root, LOCKNAME).acquire())
        return mock.patch.object(txn, "_seam", seam)

    def test_reader_ends_with_locked_when_a_writer_took_the_lock_after_its_failed_open(self):
        root = self.sandbox()
        writers = []
        try:
            with self._writer_appears_after_failed_open(root, writers):
                with self.assertRaises(txn.Locked) as cm:
                    with txn.Lock(root, LOCKNAME, shared=True) as lk:
                        self.assertFalse(lk.held)
                self.assertEqual(cm.exception.code, 4)
            self.assertTrue(writers, "the writer seam was never reached (test is vacuous)")
        finally:
            for w in writers:
                w.release()

    def test_reader_ends_with_locked_when_a_writer_came_and_went_during_the_read(self):
        # A writer that creates the lock, finishes and unlocks inside the read
        # window leaves an UNLOCKED file behind; the read may have seen a mixed state.
        root = self.sandbox()
        reached = []

        def seam(point):
            if point == "lock:shared_absent" and not reached:
                reached.append(1)
                txn.Lock(root, LOCKNAME).acquire().release()      # writer ran to completion
        with mock.patch.object(txn, "_seam", seam):
            with self.assertRaises(txn.Locked):
                with txn.Lock(root, LOCKNAME, shared=True):
                    pass
        self.assertTrue(reached, "the writer seam was never reached (test is vacuous)")

    def test_unlink_if_held_removes_only_the_file_this_lock_holds(self):
        # Any pathname removal of the lock can delete ANOTHER installer's live
        # lock. The removal must be bound to the inode this acquisition holds.
        root = self.sandbox()
        lk = txn.Lock(root, LOCKNAME).acquire()
        try:
            self.assertTrue(lk.unlink_if_held())
            self.assertFalse((root / LOCKNAME).exists())
            self.assertFalse(lk.unlink_if_held(), "a second removal must be a no-op")
            other = txn.Lock(root, LOCKNAME).acquire()           # a second installer's NEW lock file
            try:
                self.assertFalse(lk.unlink_if_held(), "removed a lock file this acquisition does not hold")
                self.assertTrue((root / LOCKNAME).exists())
            finally:
                other.release()
        finally:
            lk.release()

    def test_unlink_if_held_refuses_a_shared_or_released_lock(self):
        root = self.sandbox()
        txn.Lock(root, LOCKNAME).acquire().release()
        sh = txn.Lock(root, LOCKNAME, shared=True).acquire()
        try:
            self.assertFalse(sh.unlink_if_held())
        finally:
            sh.release()
        self.assertTrue((root / LOCKNAME).exists())
        ex = txn.Lock(root, LOCKNAME).acquire()
        ex.release()
        self.assertFalse(ex.unlink_if_held())
        self.assertTrue((root / LOCKNAME).exists())

    def test_explicit_recheck_raises_locked_for_the_same_race(self):
        root = self.sandbox()
        writers = []
        try:
            self.assertTrue(hasattr(txn.Lock, "recheck"), "Lock.recheck() does not exist")
            with self._writer_appears_after_failed_open(root, writers):
                lk = txn.Lock(root, LOCKNAME, shared=True).acquire()
                try:
                    self.assertFalse(lk.held)
                    with self.assertRaises(txn.Locked):
                        lk.recheck()
                finally:
                    lk.release()
            self.assertTrue(writers)
        finally:
            for w in writers:
                w.release()

    def test_recheck_is_quiet_when_nothing_is_locked(self):
        root = self.sandbox()
        self.assertTrue(hasattr(txn.Lock, "recheck"), "Lock.recheck() does not exist")
        with txn.Lock(root, LOCKNAME, shared=True) as lk:      # no lock file at all
            lk.recheck()
            self.assertFalse((root / LOCKNAME).exists(), "recheck created the lock file")
        with txn.Lock(root, LOCKNAME):
            pass                                                # file exists, unlocked
        with txn.Lock(root, LOCKNAME, shared=True) as lk:
            self.assertTrue(lk.held)
            lk.recheck()
        with txn.Lock(self.tmp / "no-such-root", LOCKNAME, shared=True) as lk:
            lk.recheck()                                        # missing root

    def test_exit_check_does_not_mask_a_body_exception(self):
        root = self.sandbox()
        writers = []
        try:
            with self._writer_appears_after_failed_open(root, writers):
                with self.assertRaises(ValueError):
                    with txn.Lock(root, LOCKNAME, shared=True):
                        raise ValueError("boom")
        finally:
            for w in writers:
                w.release()

    def test_reader_still_never_creates_the_lock_file(self):
        root = self.sandbox()
        with txn.Lock(root, LOCKNAME, shared=True) as lk:
            lk.recheck() if hasattr(lk, "recheck") else None
        self.assertFalse((root / LOCKNAME).exists())


class RemovePathsTests(SandboxCase):
    """Post-commit cleanup must not use pathname rmdir after a confine() check."""

    def test_api_exists(self):
        self.assertTrue(hasattr(txn.Txn, "remove_paths"), "Txn.remove_paths() does not exist")

    def test_removes_files_and_empty_dirs_deepest_first_as_given(self):
        root = self.sandbox()
        put(root, "a/b/f", b"1")
        (root / "a/b/c").mkdir()
        self.assertTrue(hasattr(txn.Txn, "remove_paths"))
        removed = make_txn(root).remove_paths(["a/b/f", "a/b/c", "a/b", "a"])
        self.assertEqual(removed, ["a/b/f", "a/b/c", "a/b", "a"])
        self.assertEqual(os.listdir(root), [])

    def test_missing_is_ok_and_not_reported(self):
        root = self.sandbox()
        self.assertTrue(hasattr(txn.Txn, "remove_paths"))
        self.assertEqual(make_txn(root).remove_paths(["nope", "x/y/z"]), [])

    def test_non_empty_dir_is_skipped(self):
        root = self.sandbox()
        put(root, "a/keep", b"k")
        self.assertTrue(hasattr(txn.Txn, "remove_paths"))
        self.assertEqual(make_txn(root).remove_paths(["a"]), [])
        self.assertEqual((root / "a/keep").read_bytes(), b"k")

    def test_dirs_false_leaves_directories_alone(self):
        root = self.sandbox()
        put(root, "a/f", b"1")
        (root / "e").mkdir()
        self.assertTrue(hasattr(txn.Txn, "remove_paths"))
        removed = make_txn(root).remove_paths(["a/f", "e", "a"], dirs=False)
        self.assertEqual(removed, ["a/f"])
        self.assertTrue((root / "e").is_dir() and (root / "a").is_dir())

    def test_final_symlink_is_unlinked_not_followed(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        (outside / "t").write_bytes(b"precious")
        os.symlink(outside / "t", root / "lnk")
        os.symlink(outside, root / "dlnk")
        self.assertTrue(hasattr(txn.Txn, "remove_paths"))
        self.assertEqual(make_txn(root).remove_paths(["lnk", "dlnk"]), ["lnk", "dlnk"])
        self.assertEqual((outside / "t").read_bytes(), b"precious")
        self.assertFalse(os.path.lexists(root / "lnk") or os.path.lexists(root / "dlnk"))

    def test_symlinked_ancestor_is_a_confine_error_and_removes_nothing_outside(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        (outside / "b").mkdir()
        (outside / "f").write_bytes(b"precious")
        os.symlink(outside, root / "a")
        self.assertTrue(hasattr(txn.Txn, "remove_paths"))
        with self.assertRaises(txn.ConfineError):
            make_txn(root).remove_paths(["a/b"])
        with self.assertRaises(txn.ConfineError):
            make_txn(root).remove_paths(["a/f"])
        self.assertTrue((outside / "b").is_dir())
        self.assertEqual((outside / "f").read_bytes(), b"precious")

    def test_invalid_paths_are_confine_errors(self):
        root = self.sandbox()
        self.assertTrue(hasattr(txn.Txn, "remove_paths"))
        for bad in ("/abs", "../x", "a/../b", "", "a//b"):
            with self.assertRaises(txn.ConfineError, msg=bad):
                make_txn(root).remove_paths([bad])

    def test_ancestor_swapped_for_symlink_between_check_and_rmdir_cannot_escape(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        (root / "a/b").mkdir(parents=True)
        (outside / "b").mkdir()
        swapped = []

        def seam(point):
            if point == "remove:parent_open" and not swapped:
                swap_dir_for_symlink(root, "a", outside, "a.orig")
                swapped.append(1)
        self.assertTrue(hasattr(txn.Txn, "remove_paths"))
        with mock.patch.object(txn, "_seam", seam):
            try:
                make_txn(root).remove_paths(["a/b"])
            except txn.InstallError:
                pass
        self.assertTrue(swapped, "the swap seam was never reached (test is vacuous)")
        self.assertTrue((outside / "b").is_dir(), "a directory outside the root was removed")
        self.assertEqual(os.listdir(outside), ["b"])

    def test_ancestor_swapped_for_symlink_between_check_and_unlink_cannot_escape(self):
        root = self.sandbox()
        outside = self.sandbox("outside")
        put(root, "a/f", b"mine")
        (outside / "f").write_bytes(b"precious")
        swapped = []

        def seam(point):
            if point == "remove:parent_open" and not swapped:
                swap_dir_for_symlink(root, "a", outside, "a.orig")
                swapped.append(1)
        self.assertTrue(hasattr(txn.Txn, "remove_paths"))
        with mock.patch.object(txn, "_seam", seam):
            try:
                make_txn(root).remove_paths(["a/f"])
            except txn.InstallError:
                pass
        self.assertTrue(swapped)
        self.assertEqual((outside / "f").read_bytes(), b"precious")


class StrayMetaTempTests(SandboxCase):
    def test_recover_sweeps_stray_meta_temps_even_without_a_txn_dir(self):
        root = self.sandbox()
        put(root, DERIVED + ".tmp-ensure", b"partial")
        put(root, MANIFEST + ".tmp-20260101T000000Z-abcd", b"partial")
        put(root, "unrelated.tmp-1", b"mine")
        put(root, "keep.txt", b"k")
        lines = make_txn(root).recover()
        self.assertEqual(sorted(os.listdir(root)), ["keep.txt", "unrelated.tmp-1"])
        self.assertTrue(lines)
        self.assertEqual(make_txn(root).recover(), [])

    def test_ensure_derived_sweeps_stray_temps(self):
        root = self.sandbox()
        t = make_txn(root)
        t.run([], simple_manifest("fp-s"))
        put(root, DERIVED + ".tmp-ensure", b"partial")
        put(root, MANIFEST + ".tmp-dead", b"partial")
        t.ensure_derived()
        self.assertEqual([n for n in os.listdir(root) if ".tmp-" in n], [])
        self.assertEqual((root / DERIVED).read_bytes(), derived_bytes(simple_manifest("fp-s")))

    def test_ensure_derived_sweeps_even_when_there_is_no_manifest(self):
        root = self.sandbox()
        put(root, DERIVED + ".tmp-ensure", b"partial")
        make_txn(root).ensure_derived()
        self.assertEqual(os.listdir(root), [])

    def test_public_sweep_is_scoped_to_the_two_namespaces(self):
        root = self.sandbox()
        put(root, DERIVED + ".tmp-x", b"1")
        put(root, MANIFEST + ".tmp-y", b"2")
        put(root, DERIVED + "-not-a-tmp", b"3")
        put(root, "hooks/a.sh.tmp-x", b"4")
        removed = make_txn(root).sweep_meta_tmps()
        self.assertEqual(sorted(removed), sorted([DERIVED + ".tmp-x", MANIFEST + ".tmp-y"]))
        self.assertTrue((root / (DERIVED + "-not-a-tmp")).exists())
        self.assertTrue((root / "hooks/a.sh.tmp-x").exists())


if __name__ == "__main__":
    unittest.main()
