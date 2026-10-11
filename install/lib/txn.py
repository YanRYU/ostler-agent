"""Generic crash-safe transactions for file installers.

Used by the devkit installer (devkit/install/_devkit.py). Python 3 stdlib only. Nothing in this module knows about modules or
settings.json: it applies a list of file operations to a root directory
atomically enough that a kill -9 at any instant recovers to either the exact
pre-transaction state or the exact complete post-transaction state.

Confinement: every mutation of the live tree (temp create, fchmod, rename,
unlink, mkdir, rmdir, backup restore, manifest/derived-file replace) is
directory-fd-relative. The root is opened once per operation; each dest is
reached by walking its components from that fd with O_NOFOLLOW|O_DIRECTORY
and the final syscall runs against the pinned parent fd (`dir_fd=`), so no
pathname is re-resolved between the check and the use: a parent swapped for a
symlink at any instant cannot redirect a write outside the root.

Compare-and-swap: an `Op` may carry `expect` = the planner's view of the
live prior state ({"type","sha256","mode"}; type "absent" allowed). Preflight
refuses (InstallError code 3, zero writes) when the live state differs, and it
is re-checked immediately before that op is applied (mismatch aborts into
roll-back, code 3). There is no kernel-level CAS on a path, so a sub-millisecond
window between the last re-check and the rename remains.

Protocol (Txn.run), in this order:

  1. Preflight (no disk writes): confine every dest, record each dest's prior
     state, refuse anything but regular file / absent, check `expect`, plan
     `created_dirs`.
  2. Journal: `<root>/<txn_dirname>/<id>/` gets `new/<i>` payloads,
     `backup/<i>` (full prior bytes of every file replaced or deleted) and
     `journal.json`, written LAST via tmp + fsync + rename. [journal_written]
  3. Apply ops in list order; each op is atomic (temp file in the same
     directory, fsync, os.replace). [after_op:<i>]
  4. Manifest -> `<manifest_name>.tmp-<id>`, fsync [manifest_temp], then
     os.replace over the manifest. That rename is THE commit point.
     [manifest_renamed]
  5. Optional derived file (off unless `derived_name` is
     given): bytes computed from the manifest by `derived_bytes`, e.g. a
     summary for other tools to read -> tmp [derived_temp] -> replace
     [derived_renamed]. Tests: devkit/tests/test_txn.py.
  6. [before_cleanup] Remove the txn dir (rename to `.gone-<id>` first so a
     crash mid-delete can never leave a journal-less half-deleted dir that
     recovery would mistake for "nothing applied"). [cleanup_renamed]

Recovery (Txn.recover; call it under the Lock): a txn dir with no journal was
never applied -> delete. A journal whose txn_id equals the committed
manifest's is committed -> regenerate the derived file, delete stale temps, delete
the dir. Otherwise roll back from the backups, STATE-AWARE per op: live ==
what this txn wrote -> restore the prior; live == the prior -> nothing to do;
anything else (an outside edit, a file created at a dest that was absent) is
left untouched and reported as a warning line - roll-back never overwrites or
deletes bytes the transaction did not write. Every recovery step is
idempotent, so a crash during recovery is recoverable by running it again.
Stray manifest/derived-file temp files are swept even when no txn dir exists.

Fault injection (optional, for crash-safety testing): env
DEVKIT_INSTALL_FAULT=<point> makes `_fault(point)` call os._exit(137) (a
simulated kill -9, no cleanup). With DEVKIT_INSTALL_FAULT_MODE=raise it raises
InjectedCrash instead (in-process tests). `FAULT_POINTS`,
`RECOVER_FAULT_POINTS` and `EXTRA_FAULT_POINTS` list the point names. Neither
variable does anything when unset. The crash matrix in
devkit/tests/test_txn.py exercises every point.

Durability note: this uses os.fsync (file + parent directory). It does not
issue macOS F_FULLFSYNC, so survival of power loss on a drive with a volatile
write cache is not claimed; survival of process death (kill -9) is.
"""
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = [
    "InstallError", "Locked", "ConfineError", "InjectedCrash",
    "sha256_bytes", "sha256_file", "lstate", "confine", "Lock", "fingerprint",
    "Op", "Txn", "FAULT_POINTS", "RECOVER_FAULT_POINTS", "EXTRA_FAULT_POINTS",
]


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
class InstallError(Exception):
    """Carries a process exit code in `.code`."""
    code = 1

    def __init__(self, message="", code=None):
        super().__init__(message)
        if code is not None:
            self.code = code


class Locked(InstallError):
    code = 4


class ConfineError(InstallError):
    code = 8


class InjectedCrash(BaseException):
    """Test-only: raised by a fault point when DEVKIT_INSTALL_FAULT_MODE=raise."""


# --------------------------------------------------------------------------
# fault injection
# --------------------------------------------------------------------------
def FAULT_POINTS(n_ops):
    """Fault points of a successful `run` with `n_ops` ops, in execution order."""
    return (["journal_written"] + ["after_op:%d" % i for i in range(n_ops)]
            + ["manifest_temp", "manifest_renamed", "derived_temp",
               "derived_renamed", "before_cleanup"])


# reachable in run() after before_cleanup and inside every recovery cleanup
EXTRA_FAULT_POINTS = ["cleanup_renamed"]


def RECOVER_FAULT_POINTS(n_ops, committed=False):
    """Fault points reached by `recover` of a txn that had `n_ops` ops.

    committed=False -> the roll-back path; committed=True -> roll-forward."""
    if committed:
        return ["recover:after_derived", "recover:before_remove"]
    return (["recover:after_restore:%d" % i for i in range(n_ops)]
            + ["recover:after_dirs", "recover:before_remove"])


def _fault(point):
    if os.environ.get("DEVKIT_INSTALL_FAULT") != point:
        return
    if os.environ.get("DEVKIT_INSTALL_FAULT_MODE") == "raise":
        raise InjectedCrash(point)
    os._exit(137)


def _seam(point):
    """No-op test seam called at exact instants inside a live-tree mutation (for
    example right after a directory fd is pinned). Tests patch it to run code
    at that instant, such as swapping the directory
    (devkit/tests/test_txn.py)."""
    return None


# --------------------------------------------------------------------------
# hashing / lstate / confine
# --------------------------------------------------------------------------
def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def lstate(path):
    """lstat-based state of `path`; never follows a symlink.

    {"type": file|dir|symlink|absent|other, "mode": "0644"|None, "sha256": hex|None}
    A symlink's sha256 is the hash of its target text."""
    path = Path(path)
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return {"type": "absent", "mode": None, "sha256": None}
    mode = format(st.st_mode & 0o777, "04o")
    m = st.st_mode
    if stat.S_ISLNK(m):
        return {"type": "symlink", "mode": mode,
                "sha256": sha256_bytes(os.fsencode(os.readlink(path)))}
    if stat.S_ISREG(m):
        return {"type": "file", "mode": mode, "sha256": sha256_file(path)}
    if stat.S_ISDIR(m):
        return {"type": "dir", "mode": mode, "sha256": None}
    return {"type": "other", "mode": mode, "sha256": None}


def _check_rel(rel):
    if not isinstance(rel, str) or rel == "" or "\x00" in rel:
        raise ConfineError("invalid relative path: %r" % (rel,))
    if rel.startswith("/") or os.path.isabs(rel):
        raise ConfineError("absolute path not allowed: %r" % rel)
    parts = rel.split("/")
    for p in parts:
        if p in ("", ".", ".."):
            raise ConfineError("non-normalized path segment in %r" % rel)
    return parts


def confine(root, rel):
    """Return root/rel after proving it cannot leave `root`.

    `rel` must be relative and normalized. Every EXISTING ancestor strictly
    below `root` must be a real directory (never a symlink); the real path of
    the result's parent must lie inside the real path of `root` (the root
    itself may be a symlink). The final component is not followed."""
    root = Path(root)
    parts = _check_rel(rel)
    cur = root
    for p in parts[:-1]:
        cur = cur / p
        try:
            st = os.lstat(cur)
        except (FileNotFoundError, NotADirectoryError):
            break   # this and everything below it is absent
        if stat.S_ISLNK(st.st_mode):
            raise ConfineError("symlinked directory in path: %s" % rel)
        if not stat.S_ISDIR(st.st_mode):
            raise ConfineError("non-directory ancestor in path: %s" % rel)
    result = root / rel
    real_root = os.path.realpath(root)
    real_parent = os.path.realpath(result.parent)
    if real_parent != real_root and not real_parent.startswith(real_root + os.sep):
        raise ConfineError("path escapes root: %s" % rel)
    return result


# --------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------
def _check_name(name, what):
    if (not isinstance(name, str) or name in ("", ".", "..") or "/" in name
            or "\x00" in name):
        raise InstallError("%s must be a plain file name, got %r" % (what, name))
    return name


class Lock:
    """flock on <root>/<lockname>, non-blocking; Locked on contention.

    Exclusive by default. `shared=True` takes LOCK_SH for read-only commands: any
    number of shared holders coexist, an exclusive holder excludes them and
    vice versa. A shared Lock never creates the lock file: when there is none
    (no installer ever ran here) nothing can be
    writing, so it succeeds without holding anything (`held` is False).

    After acquiring, the fd's inode is compared with the path's current inode; if
    the file was unlinked or replaced in between, the
    lock just taken protects nothing, so it is dropped and taken again. That is
    what keeps an unlinked lock file from letting two installers in at once.

    A shared reader that found NO lock file proceeds unlocked, which leaves a
    window: a writer may create and exclusively lock the file right afterwards.
    `recheck()` closes it - call it at the end of a read (it is also run on a
    clean `with` exit): if the file now exists and someone else holds it
    exclusively, it raises Locked, so the reader knows its read overlapped a
    writer and must be retried. It never creates the file."""

    RETRIES = 5

    def __init__(self, root, lockname, shared=False):
        self.root = Path(root)
        self.lockname = _check_name(lockname, "lockname")
        self.path = self.root / self.lockname
        self.shared = bool(shared)
        self.held = False
        self._fd = None
        self._unlocked_read = False    # shared, and no lock file existed when we looked
        self.created = False           # THIS acquisition created the lock file (exclusive only)

    def _try_once(self):
        flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= os.O_RDONLY if self.shared else (os.O_RDWR | os.O_CREAT)
        _seam("lock:before_open")
        created = False
        try:
            fd = None
            if not self.shared:
                try:
                    fd = os.open(self.path, flags | os.O_EXCL, 0o600)
                    created = True
                except FileExistsError:
                    fd = None              # pre-existing (or a symlink: O_EXCL refuses it too)
                except OSError as e:
                    if e.errno != errno.ELOOP:
                        raise
            if fd is None:
                fd = os.open(self.path, flags, 0o600)
        except FileNotFoundError as e:
            if self.shared:
                self._unlocked_read = True
                _seam("lock:shared_absent")
                return True          # no lock file => no installer => nothing to wait for
            raise InstallError("cannot open lock file %s: %s" % (self.path, e))
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise ConfineError("lock file is a symlink: %s" % self.path)
            raise InstallError("cannot open lock file %s: %s" % (self.path, e))
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise Locked("another install is running (lock held: %s)" % self.path)
            raise InstallError("cannot lock %s: %s" % (self.path, e))
        try:
            fst = os.fstat(fd)
            try:
                pst = os.lstat(self.path)
            except FileNotFoundError:
                pst = None
            if pst is not None and (pst.st_dev, pst.st_ino) == (fst.st_dev, fst.st_ino):
                self._fd = fd
                self.held = True
                self.created = created
                return True
        except BaseException:
            os.close(fd)
            raise
        # the path no longer names the file we hold: drop it and try again
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return False

    def acquire(self):
        for _ in range(self.RETRIES):
            if self._try_once():
                return self
        raise InstallError("lock file %s keeps being replaced; try again" % self.path)

    def recheck(self):
        """For a shared lock that holds nothing (no lock file at acquire time): raise
        Locked if the file has since appeared (a writer is running, or ran to completion
        inside the read window). A no-op for an exclusive lock, for a lock actually held, and
        when there is still no lock file (or no install root). Never creates the file."""
        if not self.shared or self.held or not self._unlocked_read:
            return
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.path, flags)
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise ConfineError("lock file is a symlink: %s" % self.path)
            raise InstallError("cannot open lock file %s: %s" % (self.path, e))
        try:
            # The file did not exist when the read began and exists now: an installer started
            # (and may already have finished) inside the read window, so what the read saw may be
            # a mixed state. Held or already released makes no difference — retry the read.
            # (A writer that also REMOVED the file is not detectable here.)
            raise Locked("an install ran while this read was running "
                         "(lock file appeared: %s); retry" % self.path)
        finally:
            os.close(fd)

    def unlink_if_held(self):
        """Unlink the lock file ONLY if the path still names the inode this exclusive acquisition
        holds. Safe to call twice and safe against another installer's newer lock file: a path that
        is absent, replaced by a different inode, or a lock this call does not hold is left alone
        (returns False). Callers use this instead of removing the lock by pathname."""
        if self._fd is None or self.shared or not self.held:
            return False
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            dfd = os.open(self.root, flags)
        except OSError:
            return False
        try:
            try:
                fst = os.fstat(self._fd)
                pst = os.stat(self.lockname, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                return False
            if (pst.st_dev, pst.st_ino) != (fst.st_dev, fst.st_ino):
                return False
            try:
                os.unlink(self.lockname, dir_fd=dfd)
            except OSError:
                return False
            return True
        finally:
            os.close(dfd)

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
                self.held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:      # never mask the body's own exception
                self.recheck()
        finally:
            self.release()
        return False


# --------------------------------------------------------------------------
# directories a run created (and takes back when it ends without committing)
# --------------------------------------------------------------------------
def make_missing_dirs(path):
    """Create `path` (and missing parents); returns the directories this call created,
    innermost first."""
    missing, p = [], Path(path)
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    os.makedirs(path, exist_ok=True)
    return missing


def discard_created(created, lockname=None):
    """Take back directories a run created when it ended without committing anything.

    fd-relative: each directory is opened from its parent's descriptor with O_NOFOLLOW, its lock
    file is unlinked through THAT descriptor and the directory itself is rmdir'd through the
    parent's, so a directory swapped for a symlink meanwhile is neither entered nor removed.

    `lockname=None` leaves the lock file alone: a caller that still HOLDS the lock removes it itself
    with Lock.unlink_if_held() (bound to the inode it holds) BEFORE calling this, so no pathname
    removal can ever delete another installer's live lock; a directory that still holds one is then
    simply not empty and stays."""
    cloexec = getattr(os, "O_CLOEXEC", 0)
    dflags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | cloexec
    chain = [Path(d) for d in reversed(created)]        # outermost -> innermost, each the child of the last
    if not chain:
        return
    fds = []                                            # fds[i] = open fd of chain[i-1] (fds[0] = parent of chain[0])
    try:
        try:
            fds.append(os.open(chain[0].parent, dflags))    # pre-existing, may legitimately be a symlink path
        except OSError:
            return
        for d in chain:                                 # pin every created level, no-follow, from its parent's fd
            try:
                fds.append(os.open(d.name, dflags | getattr(os, "O_NOFOLLOW", 0), dir_fd=fds[-1]))
            except OSError:
                break                                   # gone, or no longer a real directory: stop, touch nothing above
        if len(fds) - 1 < len(chain):
            return                                      # a level was swapped: never remove anything through it
        if lockname:
            with contextlib.suppress(OSError):
                os.unlink(lockname, dir_fd=fds[-1])
        for i in range(len(chain) - 1, -1, -1):         # innermost first, each rmdir'd through its parent's fd
            with contextlib.suppress(OSError):
                os.rmdir(chain[i].name, dir_fd=fds[i])
    finally:
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)


# --------------------------------------------------------------------------
# fingerprint
# --------------------------------------------------------------------------
def fingerprint(entries, domain=b"devkit-fingerprint/1"):
    """sha256 over the sorted (dest, sha256, type, mode) rows of `entries`.

    `entries` are dicts; missing keys count as null. Extra rows (e.g. a whole
    module) use the same shape, e.g. {"dest": "module:<name>", "sha256":
    <sha of its content>, "type": "module", "mode": None}. Order-independent;
    duplicates are significant; null and "" are distinct. `domain` is a
    namespace prefix mixed into the hash."""
    rows = sorted(
        json.dumps([e.get("dest"), e.get("sha256"), e.get("type"), e.get("mode")],
                   separators=(",", ":"), ensure_ascii=True)
        for e in entries)
    h = hashlib.sha256()
    h.update(domain + b"\n")
    h.update("\n".join(rows).encode("ascii"))
    return h.hexdigest()


# --------------------------------------------------------------------------
# file helpers
# --------------------------------------------------------------------------
def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_quiet(path):
    try:
        os.unlink(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


def _write_tmp(tmp, data, mode):
    """Write data to `tmp` (fresh, no-follow), fchmod, fsync. No rename."""
    _unlink_quiet(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), int(mode, 8))
            os.fsync(f.fileno())
    except BaseException:
        _unlink_quiet(tmp)
        raise


def _replace(tmp, dest):
    os.replace(tmp, dest)
    _fsync_dir(Path(dest).parent)


def _write_atomic(dest, data, mode, tmp):
    _write_tmp(tmp, data, mode)
    _replace(tmp, dest)


def _tmp_for(path, txn_id, tag):
    return path.with_name("%s.tmp-%s-%s" % (path.name, txn_id, tag))


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_MODE_RE = re.compile(r"^0[0-7]{3}$")


def _check_id(txn_id):
    if not isinstance(txn_id, str) or not _ID_RE.match(txn_id):
        raise InstallError("invalid txn id: %r" % (txn_id,))
    return txn_id


def _new_id():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)


def _json_bytes(obj):
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


# --------------------------------------------------------------------------
# directory-fd-relative access to the live tree
# --------------------------------------------------------------------------
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_NOFOLLOW | _O_CLOEXEC


def _tmp_name(name, txn_id, tag):
    return "%s.tmp-%s-%s" % (name, txn_id, tag)


def _sibling(rel, name):
    """`rel` with its final component replaced by `name` (both relative to the root)."""
    head, sep, _ = rel.rpartition("/")
    return head + sep + name


def _lstate_at(pfd, name):
    """lstate() of `name` inside the directory `pfd`, without re-resolving any path."""
    try:
        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return {"type": "absent", "mode": None, "sha256": None}
    mode = format(st.st_mode & 0o777, "04o")
    m = st.st_mode
    if stat.S_ISLNK(m):
        try:
            target = os.readlink(name, dir_fd=pfd)
        except (FileNotFoundError, NotADirectoryError):
            return {"type": "absent", "mode": None, "sha256": None}
        return {"type": "symlink", "mode": mode, "sha256": sha256_bytes(os.fsencode(target))}
    if stat.S_ISREG(m):
        try:
            fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | getattr(os, "O_NONBLOCK", 0),
                         dir_fd=pfd)
        except (FileNotFoundError, NotADirectoryError):
            return {"type": "absent", "mode": None, "sha256": None}
        except OSError as e:
            if e.errno == errno.ELOOP:      # became a symlink under us
                return {"type": "symlink", "mode": mode, "sha256": None}
            raise
        with os.fdopen(fd, "rb") as f:
            fst = os.fstat(f.fileno())
            if not stat.S_ISREG(fst.st_mode):
                return {"type": "other", "mode": format(fst.st_mode & 0o777, "04o"), "sha256": None}
            h = hashlib.sha256()
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
            return {"type": "file", "mode": format(fst.st_mode & 0o777, "04o"),
                    "sha256": h.hexdigest()}
    if stat.S_ISDIR(m):
        return {"type": "dir", "mode": mode, "sha256": None}
    return {"type": "other", "mode": mode, "sha256": None}


class _Tree:
    """The live root, held as ONE directory fd. Every path below it is reached by
    walking its components from that fd with O_NOFOLLOW|O_DIRECTORY, and the final
    syscall uses the pinned parent (`dir_fd=`); nothing is ever re-resolved by
    pathname. A symlinked or non-directory ancestor raises ConfineError (the root
    itself may be a symlink: it is followed exactly once, here)."""

    def __init__(self, root):
        try:
            self.fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_CLOEXEC)
        except FileNotFoundError:
            raise
        except OSError as e:
            raise InstallError("cannot open install root %s: %s" % (root, e))
        self.root = Path(root)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- walking ---------------------------------------------------------------
    def _open_dir(self, parent_fd, comp, rel):
        try:
            return os.open(comp, _O_DIR, dir_fd=parent_fd)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ConfineError("symlinked or non-directory ancestor in path: %s" % rel)
            raise

    def _walk(self, rel, create):
        parts = _check_rel(rel)
        opened, cur = [], self.fd
        try:
            for comp in parts[:-1]:
                try:
                    nfd = self._open_dir(cur, comp, rel)
                except FileNotFoundError:
                    if not create:
                        for fd in opened:
                            os.close(fd)
                        return [], None, parts[-1]
                    try:
                        os.mkdir(comp, 0o755, dir_fd=cur)
                    except FileExistsError:
                        pass
                    nfd = self._open_dir(cur, comp, rel)
                opened.append(nfd)
                cur = nfd
        except BaseException:
            for fd in opened:
                os.close(fd)
            raise
        return opened, cur, parts[-1]

    @contextlib.contextmanager
    def parent(self, rel, create=False):
        """Yield (parent_fd | None, final_name). None: an ancestor does not exist."""
        opened, pfd, name = self._walk(rel, create)
        try:
            yield pfd, name
        finally:
            for fd in opened:
                os.close(fd)

    # -- reads -----------------------------------------------------------------
    def lstate(self, rel):
        with self.parent(rel) as (pfd, name):
            if pfd is None:
                return {"type": "absent", "mode": None, "sha256": None}
            return _lstate_at(pfd, name)

    def read_bytes(self, rel):
        with self.parent(rel) as (pfd, name):
            if pfd is None:
                raise FileNotFoundError(rel)
            fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=pfd)
            with os.fdopen(fd, "rb") as f:
                return f.read()

    def listdir(self):
        return os.listdir(self.fd)

    # -- mutations -------------------------------------------------------------
    def write_atomic(self, rel, data, mode, tmpname, between=None):
        """temp file next to the dest (fresh, no-follow) -> fchmod -> fsync ->
        [between()] -> rename. `between` is the crash-injection point that sits
        after the temp is durable and before it replaces the dest."""
        with self.parent(rel, create=True) as (pfd, name):
            _seam("write:parent_open")
            try:
                os.unlink(tmpname, dir_fd=pfd)
            except (FileNotFoundError, NotADirectoryError):
                pass
            fd = os.open(tmpname, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                         0o600, dir_fd=pfd)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fchmod(f.fileno(), int(mode, 8))
                    os.fsync(f.fileno())
            except BaseException:
                try:
                    os.unlink(tmpname, dir_fd=pfd)
                except OSError:
                    pass
                raise
            if between is not None:
                between()
            os.replace(tmpname, name, src_dir_fd=pfd, dst_dir_fd=pfd)
            os.fsync(pfd)

    def unlink(self, rel):
        """Remove the file; True when something was removed."""
        with self.parent(rel) as (pfd, name):
            if pfd is None:
                return False
            try:
                os.unlink(name, dir_fd=pfd)
            except (FileNotFoundError, NotADirectoryError):
                return False
            os.fsync(pfd)
            return True

    def mkdir(self, rel):
        """Create one directory (0755, umask-proof); True when it was created."""
        with self.parent(rel) as (pfd, name):
            if pfd is None:
                raise FileNotFoundError("parent of %s does not exist" % rel)
            try:
                os.mkdir(name, 0o755, dir_fd=pfd)
            except FileExistsError:
                st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
                if not stat.S_ISDIR(st.st_mode):
                    raise ConfineError("%s exists and is not a real directory" % rel)
                return False
            dfd = os.open(name, _O_DIR, dir_fd=pfd)
            try:
                os.fchmod(dfd, 0o755)
            finally:
                os.close(dfd)
            os.fsync(pfd)
            return True

    def rmdir(self, rel):
        """rmdir; OSError (e.g. not empty) propagates, an absent dir is a no-op."""
        with self.parent(rel) as (pfd, name):
            if pfd is None:
                return
            try:
                os.rmdir(name, dir_fd=pfd)
            except FileNotFoundError:
                return
            os.fsync(pfd)

    def fsync_root(self):
        os.fsync(self.fd)


# --------------------------------------------------------------------------
# Op / Txn
# --------------------------------------------------------------------------
@dataclass
class Op:
    """One live-tree mutation. kind: "write" | "delete". dest is relative to
    the root. For "write", `mode` None preserves the prior file's mode, else
    0644.

    `expect` (optional) is the planner's view of the live prior state of `dest`:
    {"type": "file"|"absent", "sha256": hex, "mode": "0644"} (an lstate() dict
    works as is). Txn.run refuses with InstallError code 3 - before writing
    anything - when the live state differs, and checks again right before the op
    is applied. None skips the check (the op is applied unconditionally)."""
    kind: str
    dest: str
    data: Optional[bytes] = None
    mode: Optional[str] = None
    expect: Optional[dict] = None


def _check_expect(op):
    e = op.expect
    if (not isinstance(e, dict) or e.get("type") not in ("file", "absent")
            or (e["type"] == "file" and not isinstance(e.get("sha256"), str))
            or (e.get("mode") is not None and not (isinstance(e["mode"], str)
                                                     and _MODE_RE.match(e["mode"])))):
        raise InstallError("invalid expect for %s: %r" % (op.dest, e))


def _expect_mismatch(expect, live):
    """None when `live` (an lstate dict) is what the planner saw, else a description."""
    if live["type"] != expect["type"]:
        return "expected %s, found %s" % (expect["type"], live["type"])
    if expect["type"] == "file":
        if live["sha256"] != expect["sha256"]:
            return "content changed"
        if expect.get("mode") is not None and live["mode"] != expect["mode"]:
            return "mode changed (%s -> %s)" % (expect["mode"], live["mode"])
    return None


def _classify(op, prior, cur):
    """Roll-back verdict for one journaled op given the live state `cur`:
    "skip" (already the prior state), "restore" / "unlink" (live is exactly what
    this transaction wrote), or "foreign" (anything else: leave it alone)."""
    is_file = cur["type"] == "file"
    if prior["type"] == "absent":
        if cur["type"] == "absent":
            return "skip"
        if (op["kind"] == "write" and is_file and cur["sha256"] == op["new_sha"]
                and cur["mode"] == op["new_mode"]):
            return "unlink"
        return "foreign"
    if is_file and cur["sha256"] == prior["sha256"] and cur["mode"] == prior["mode"]:
        return "skip"
    if op["kind"] == "delete":
        wrote = cur["type"] == "absent"
    else:
        wrote = (is_file and cur["sha256"] == op["new_sha"] and cur["mode"] == op["new_mode"])
    return "restore" if wrote else "foreign"


class Txn:
    def __init__(self, root, *, manifest_name, txn_dirname, lockname,
                 derived_name=None, derived_bytes=None):
        self.root = Path(root)
        self.manifest_name = _check_name(manifest_name, "manifest_name")
        self.txn_dirname = _check_name(txn_dirname, "txn_dirname")
        self.lockname = _check_name(lockname, "lockname")
        if derived_name is not None:
            _check_name(derived_name, "derived_name")
            if derived_bytes is None:
                raise InstallError("derived_name requires derived_bytes")
        self.derived_name = derived_name
        self.derived_bytes = derived_bytes
        self.base = self.root / self.txn_dirname
        reserved = {self.manifest_name, self.lockname, self.txn_dirname}
        if derived_name:
            reserved.add(derived_name)
        if len(reserved) != (3 + bool(derived_name)):
            raise InstallError("manifest/derived/lock/txn names must be distinct")

    # -- helpers -----------------------------------------------------------
    def lock(self, shared=False):
        return Lock(self.root, self.lockname, shared=shared)

    def _base_state(self):
        st = lstate(self.base)
        if st["type"] not in ("absent", "dir"):
            raise ConfineError("txn dir %s is a %s" % (self.txn_dirname, st["type"]))
        return st["type"]

    def pending(self):
        """Txn dirs left behind. Lock-free callers race a committing install that
        removes the dir between the stat and the listing: that means none."""
        try:
            if self._base_state() == "absent":
                return []
            return sorted(self.base / n for n in os.listdir(self.base))
        except (FileNotFoundError, NotADirectoryError):
            return []

    def _read_manifest(self):
        p = self.root / self.manifest_name
        st = lstate(p)
        if st["type"] == "absent":
            return None
        if st["type"] != "file":
            raise InstallError("manifest %s is a %s" % (self.manifest_name, st["type"]))
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise InstallError("manifest %s is not valid JSON: %s" % (self.manifest_name, e))
        if not isinstance(m, dict):
            raise InstallError("manifest %s is not an object" % self.manifest_name)
        return m

    def _derived_path(self):
        return self.root / self.derived_name

    def _render_derived(self, manifest):
        """Derived-file bytes computed from a manifest dict (never from anything else)."""
        data = self.derived_bytes(manifest)
        if not isinstance(data, (bytes, bytearray)):
            raise InstallError("derived_bytes must return bytes")
        return bytes(data)

    @contextlib.contextmanager
    def _tree(self):
        with _Tree(self.root) as tree:
            yield tree

    def ensure_derived(self):
        """Optional: regenerate the derived file from the committed
        manifest if missing/different (tested in devkit/tests/test_txn.py).
        Also sweeps stray manifest/derived-file temp files (call it under the Lock)."""
        with self._tree() as tree:
            self._ensure_derived(tree)

    def _ensure_derived(self, tree):
        self._sweep_meta_tmps(tree)
        if not self.derived_name:
            return
        m = self._read_manifest()
        if m is None:
            return
        want = self._render_derived(m)
        st = tree.lstate(self.derived_name)
        if st["type"] == "file" and st["sha256"] == sha256_bytes(want):
            return
        if st["type"] not in ("file", "absent"):
            raise InstallError("derived file %s is a %s" % (self.derived_name, st["type"]))
        tree.write_atomic(self.derived_name, want, "0644", self.derived_name + ".tmp-ensure")

    def _meta_prefixes(self):
        prefixes = [self.manifest_name + ".tmp-"]
        if self.derived_name:
            prefixes.append(self.derived_name + ".tmp-")
        return prefixes

    def _sweep_meta_tmps(self, tree):
        """Delete manifest/derived-file temp files (our own namespaces only)."""
        removed = []
        prefixes = self._meta_prefixes()
        for n in sorted(tree.listdir()):
            if any(n.startswith(p) for p in prefixes):
                try:
                    if tree.unlink(n):
                        removed.append(n)
                except OSError:
                    continue      # e.g. a directory that happens to carry the prefix
        return removed

    def sweep_meta_tmps(self):
        """Public form of the sweep (call it under the Lock; tested in
        devkit/tests/test_txn.py). Removes `<manifest>.tmp-*` and `<derived>.tmp-*`; returns the names."""
        try:
            with self._tree() as tree:
                return self._sweep_meta_tmps(tree)
        except FileNotFoundError:
            return []

    def remove_paths(self, rels, dirs=True):
        """Optional: remove files/empty directories after a commit,
        fd-relative like every other live-tree mutation (call it under the
        Lock; tested in devkit/tests/test_txn.py). For each `rel`, in the order given: a file or
        symlink is unlinked (a symlink itself, never its target); with `dirs` an
        EMPTY directory is rmdir'd (a non-empty one is skipped, never emptied;
        with dirs=False every directory is skipped). Missing is fine. The parent
        is reached from one pinned root fd with O_NOFOLLOW, so an ancestor
        swapped for a symlink at any instant cannot redirect a removal outside
        the root: a symlinked/non-directory ancestor raises ConfineError, and a
        swap after the parent was pinned only affects the real (moved) directory.
        Returns the list of rels actually removed."""
        removed = []
        try:
            tree_cm = self._tree()
            tree = tree_cm.__enter__()
        except FileNotFoundError:
            for rel in rels:
                _check_rel(rel)
            return removed
        try:
            for rel in rels:
                with tree.parent(rel) as (pfd, name):
                    if pfd is None:
                        continue
                    _seam("remove:parent_open")
                    try:
                        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    try:
                        if stat.S_ISDIR(st.st_mode):
                            if not dirs:
                                continue
                            os.rmdir(name, dir_fd=pfd)
                        else:
                            os.unlink(name, dir_fd=pfd)
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    except OSError as e:
                        if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
                            continue          # non-empty dir: never emptied by us
                        raise
                    os.fsync(pfd)
                    removed.append(rel)
        finally:
            tree_cm.__exit__(None, None, None)
        return removed

    def _cleanup_dir(self, d):
        """Atomic-ish removal of a txn dir (see module docstring)."""
        if d.name.startswith(".gone-"):
            gone = d
        else:
            gone = d.parent / (".gone-" + d.name)
            if lstate(gone)["type"] != "absent":
                shutil.rmtree(gone)
            os.rename(d, gone)
            _fsync_dir(d.parent)
        _fault("cleanup_renamed")
        shutil.rmtree(gone)
        try:
            os.rmdir(self.base)   # only if now empty
        except OSError:
            pass
        _fsync_dir(self.root)

    # -- run ---------------------------------------------------------------
    def run(self, ops, new_manifest, *, txn_id=None):
        if self.pending():
            raise InstallError("pending transaction(s) present; run recover first", 6)
        txn_id = _check_id(txn_id or _new_id())
        self._base_state()

        with self._tree() as tree:
            return self._run(tree, ops, new_manifest, txn_id)

    def _run(self, tree, ops, new_manifest, txn_id):
        root = self.root
        # 1. preflight: validate and describe every op; nothing is written yet
        reserved = {self.manifest_name, self.lockname}
        if self.derived_name:
            reserved.add(self.derived_name)
        plan, seen, planned_dirs, created_dirs = [], set(), set(), []
        for i, op in enumerate(ops):
            if op.kind not in ("write", "delete"):
                raise InstallError("unknown op kind %r" % (op.kind,))
            confine(root, op.dest)
            first = op.dest.split("/")[0]
            if op.dest in reserved or first == self.txn_dirname:
                raise ConfineError("op targets installer metadata: %s" % op.dest)
            if op.dest in seen:
                raise InstallError("duplicate op for %s" % op.dest)
            seen.add(op.dest)
            prior = tree.lstate(op.dest)
            if prior["type"] not in ("file", "absent"):
                raise InstallError("refusing to touch %s at %s (conflict)"
                                   % (prior["type"], op.dest), 3)
            if op.expect is not None:
                _check_expect(op)
                why = _expect_mismatch(op.expect, prior)
                if why:
                    raise InstallError("%s changed since it was planned (%s); "
                                       "nothing was written" % (op.dest, why), 3)
            entry = {"i": i, "kind": op.kind, "dest": op.dest,
                     "prior": prior, "data": None, "new_sha": None, "new_mode": None,
                     "expect": op.expect}
            if op.kind == "write":
                if not isinstance(op.data, (bytes, bytearray)):
                    raise InstallError("write op for %s needs bytes data" % op.dest)
                mode = op.mode
                if mode is None:
                    mode = prior["mode"] if prior["type"] == "file" else "0644"
                if not isinstance(mode, str) or not _MODE_RE.match(mode):
                    raise InstallError("invalid mode %r for %s" % (mode, op.dest))
                entry["data"] = bytes(op.data)
                entry["new_sha"] = sha256_bytes(entry["data"])
                entry["new_mode"] = mode
                parts = op.dest.split("/")[:-1]
                for k in range(len(parts)):
                    rel = "/".join(parts[:k + 1])
                    if rel in planned_dirs:
                        continue
                    if tree.lstate(rel)["type"] == "absent":
                        planned_dirs.add(rel)
                        created_dirs.append(rel)
            elif op.data is not None or op.mode is not None:
                raise InstallError("delete op for %s takes no data/mode" % op.dest)
            plan.append(entry)

        new_manifest["txn_id"] = txn_id
        manifest_bytes = _json_bytes(new_manifest)
        derived_data = self._render_derived(new_manifest) if self.derived_name else None

        # 2. journal (everything durable before the first live mutation)
        d = self.base / txn_id
        if not self.base.exists():
            os.mkdir(self.base, 0o700)
        os.mkdir(d, 0o700)
        try:
            self._journal_and_apply(tree, d, txn_id, plan, created_dirs, manifest_bytes,
                                    derived_data)
        except Exception:
            # An ordinary failure (disk full, EIO, ...) is not a kill: undo or
            # finish in-process so the caller is never left with a mixed tree.
            # InjectedCrash / KeyboardInterrupt are BaseExceptions and pass
            # through untouched (a real kill leaves this for recover()).
            try:
                self.recover()
            except Exception:
                pass    # the original error is the one worth reporting
            raise
        return txn_id

    def _journal_and_apply(self, tree, d, txn_id, plan, created_dirs, manifest_bytes,
                           derived_data):
        root = self.root
        os.mkdir(d / "new", 0o700)
        os.mkdir(d / "backup", 0o700)
        jops = []
        for e in plan:
            prior = dict(e["prior"])
            if e["kind"] == "write":
                _write_tmp(d / "new" / str(e["i"]), e["data"], "0600")
            if prior["type"] == "file":
                bak = "backup/%d" % e["i"]
                _write_tmp(d / bak, tree.read_bytes(e["dest"]), "0600")
                prior["backup"] = bak
                if sha256_file(d / bak) != prior["sha256"]:
                    raise InstallError("%s changed while being backed up" % e["dest"], 3)
            jops.append({"i": e["i"], "kind": e["kind"], "dest": e["dest"],
                         "new_sha": e["new_sha"], "new_mode": e["new_mode"],
                         "prior": prior})
        journal = {"journal_version": 1, "txn_id": txn_id, "ops": jops,
                   "created_dirs": created_dirs, "manifest_name": self.manifest_name,
                   "derived_name": self.derived_name}
        for sub in ("new", "backup"):
            _fsync_dir(d / sub)
        _write_atomic(d / "journal.json", _json_bytes(journal), "0600", d / "journal.json.tmp")
        _fsync_dir(d)
        _fsync_dir(self.base)
        tree.fsync_root()
        _fault("journal_written")

        # 3. apply (every mutation is fd-relative; see module docstring)
        for rel in created_dirs:
            tree.mkdir(rel)
        for e in plan:
            dest = e["dest"]
            confine(root, dest)   # early, readable refusal; the fd walk is the real guard
            if e["expect"] is not None:
                why = _expect_mismatch(e["expect"], tree.lstate(dest))
                if why:
                    raise InstallError("%s changed while the install was running (%s); "
                                       "rolling back" % (dest, why), 3)
            if e["kind"] == "write":
                tree.write_atomic(dest, e["data"], e["new_mode"],
                                  _tmp_name(dest.rsplit("/", 1)[-1], txn_id, e["i"]))
            else:
                tree.unlink(dest)
            _fault("after_op:%d" % e["i"])

        # 4. commit point
        mtmp_name = "%s.tmp-%s" % (self.manifest_name, txn_id)
        tree.write_atomic(self.manifest_name, manifest_bytes, "0644", mtmp_name,
                          between=lambda: _fault("manifest_temp"))
        _fault("manifest_renamed")

        # 5. derived file
        if self.derived_name:
            tree.write_atomic(self.derived_name, derived_data, "0644",
                              "%s.tmp-%s" % (self.derived_name, txn_id),
                              between=lambda: _fault("derived_temp"))
            _fault("derived_renamed")

        # 6. cleanup
        _fault("before_cleanup")
        self._cleanup_dir(d)

    # -- recover -----------------------------------------------------------
    def recover(self):
        log = []
        try:
            with self._tree() as tree:
                if self._base_state() != "absent":
                    self._recover_pending(tree, log)
                for n in self._sweep_meta_tmps(tree):
                    log.append("recover: removed stray temp file %s" % n)
        except FileNotFoundError:
            pass      # no install root at all: nothing to recover
        return log

    def _recover_pending(self, tree, log):
        committed = self._read_manifest()
        committed_id = committed.get("txn_id") if committed else None
        for d in reversed(self.pending()):     # newest first
            name = d.name
            if name.startswith(".gone-"):
                self._cleanup_dir(d)
                log.append("recover: removed leftover %s" % name)
                continue
            if lstate(d)["type"] != "dir":
                raise InstallError("unexpected non-directory in %s: %s" % (self.txn_dirname, name))
            jp = d / "journal.json"
            if lstate(jp)["type"] == "absent":
                self._cleanup_dir(d)
                log.append("recover: %s had no journal (nothing was applied); discarded" % name)
                continue
            try:
                journal = json.loads(jp.read_text(encoding="utf-8"))
                if (not isinstance(journal, dict) or journal.get("txn_id") != name
                        or not isinstance(journal.get("ops"), list)
                        or not isinstance(journal.get("created_dirs"), list)):
                    raise ValueError("unexpected journal shape")
            except (ValueError, UnicodeDecodeError, OSError) as e:
                raise InstallError("corrupt journal in %s: %s" % (name, e))
            if journal["txn_id"] == committed_id:
                self._roll_forward(tree, d, journal, log)
            else:
                self._roll_back(tree, d, journal, log)

    def _roll_forward(self, tree, d, journal, log):
        name = d.name
        self._ensure_derived(tree)
        _fault("recover:after_derived")
        for op in journal["ops"]:
            confine(self.root, op["dest"])
            for tag in (op["i"], "r"):
                try:
                    tree.unlink(_sibling(op["dest"], _tmp_name(op["dest"].rsplit("/", 1)[-1], name, tag)))
                except ConfineError:
                    pass      # best-effort temp cleanup never follows a swapped ancestor
        self._sweep_meta_tmps(tree)
        _fault("recover:before_remove")
        self._cleanup_dir(d)
        log.append("recover: %s was committed; finished cleanup" % name)

    def _roll_back(self, tree, d, journal, log):
        name = d.name
        ops = journal["ops"]
        # resolve + verify EVERYTHING before restoring anything
        todo = []
        for op in ops:
            confine(self.root, op["dest"])
            prior = op["prior"]
            data = None
            if prior["type"] == "file":
                bak = d / prior["backup"]
                if (os.path.isabs(prior["backup"]) or ".." in prior["backup"].split("/")
                        or lstate(bak)["type"] != "file"):
                    raise InstallError("missing/invalid backup for %s in %s" % (op["dest"], name))
                data = bak.read_bytes()
                if sha256_bytes(data) != prior["sha256"]:
                    raise InstallError("backup for %s in %s does not match its recorded hash"
                                       % (op["dest"], name))
            elif prior["type"] != "absent":
                raise InstallError("journal %s records unsupported prior type %r"
                                   % (name, prior["type"]))
            todo.append((op, prior, data))
        restored = 0
        for op, prior, data in reversed(todo):
            dest = op["dest"]
            base = dest.rsplit("/", 1)[-1]
            for tag in (op["i"], "r"):
                tree.unlink(_sibling(dest, _tmp_name(base, name, tag)))
            cur = tree.lstate(dest)
            verdict = _classify(op, prior, cur)
            if verdict == "unlink":
                tree.unlink(dest)
                restored += 1
            elif verdict == "restore":
                tree.write_atomic(dest, data, prior["mode"], _tmp_name(base, name, "r"))
                restored += 1
            elif verdict == "foreign":
                log.append("recover: left %s untouched: it changed outside this transaction "
                           "(found %s%s)" % (dest, cur["type"],
                                             "" if cur["type"] == "absent" else
                                             ", sha256 %s" % (cur["sha256"] or "n/a")[:12]))
            _fault("recover:after_restore:%d" % op["i"])
        for rel in reversed(journal["created_dirs"]):
            confine(self.root, rel)
            st = tree.lstate(rel)
            if st["type"] == "absent":
                continue
            if st["type"] != "dir":
                log.append("recover: left %s alone (not a directory)" % rel)
                continue
            try:
                tree.rmdir(rel)
            except OSError:
                log.append("recover: kept directory %s (not empty)" % rel)
        _fault("recover:after_dirs")
        self._sweep_meta_tmps(tree)
        _fault("recover:before_remove")
        self._cleanup_dir(d)
        log.append("recover: %s was not committed; rolled back %d change(s)" % (name, restored))
