"""Tests for the harness-governance module: a downstream repo audits its own harness against a baseline.

Run from the repo root:
    bash devkit/tests/test-harness-governance.sh

What is checked:
  factory-unreachable  the installed scan.sh, diff-registry.py and `registry.py init` run in a 2.0
                       target with the source checkout unreachable and no PyYAML
  baseline-then-drift  `registry.py init` seeds each artifact's domain from the install manifest; a
                       second audit reports zero drift; a planted unwired hook is then reported;
                       deleting a managed rule is reported
  schema-shared        the shipped schema is generated from the one source the installer validates
                       `domain:` against; a test asserts equality

The equality test needs the optional registry schema file (harness-registry/_schema.yaml or DEVKIT_REGISTRY_SCHEMA); without it it
skips and the vocabulary test (devkit/domains.json) still runs.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "devkit/modules/harness-governance"
PAY = MOD / "payload"
SCAN_SRC = PAY / "scripts/harness-audit/scan.sh"
DIFF_SRC = PAY / "scripts/harness-audit/diff-registry.py"
REG_SRC = PAY / "scripts/harness-audit/registry.py"
SCHEMA = PAY / "harness-registry/_schema.json"
SKILL = PAY / "skills/harness-audit/SKILL.md"
RULE = PAY / "rules/harness-governance.md"
RUNNER = REPO / "devkit/tests/test-harness-governance.sh"
INSTALLER = REPO / "devkit/install/devkit-install.sh"
SHIPPED = [MOD / "MODULE.md", SCAN_SRC, DIFF_SRC, REG_SRC, SCHEMA, SKILL, RULE, RUNNER, Path(__file__)]

SCRIPTS = ".claude/scripts/harness-audit"
REGISTRY = ".claude/harness-registry"

sys.path.insert(0, str(REPO / "publish"))
try:
    import build  # noqa: E402
except ImportError:  # pragma: no cover
    build = None

sys.path.insert(0, str(REPO / "devkit/install"))
try:
    import yaml  # noqa: E402
    import _devkit  # noqa: E402
except ImportError:  # pragma: no cover
    yaml = None
    _devkit = None

HAVE_JQ = shutil.which("jq") is not None
SCHEMA_YAML = _devkit.SCHEMA_FILE if _devkit is not None else Path("/nonexistent")

# Run a script as __main__ with PyYAML made unimportable (on top of -I -S, which also drops
# site-packages): `import yaml` raises ImportError whatever the interpreter's layout.
BLOCK_YAML = ("import sys,runpy;sys.modules['yaml']=None;p=sys.argv[1];sys.argv=sys.argv[1:];"
              "runpy.run_path(p,run_name='__main__')")


def clean_env(root):
    """Environment built from scratch: no inherited variables."""
    dirs = []
    for exe in ("python3", "bash", "jq", "git"):
        p = shutil.which(exe)
        if p:
            dirs.append(str(Path(p).parent))
    dirs += ["/usr/bin", "/bin"]
    home = Path(root) / "emptyhome"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "PATH": ":".join(dict.fromkeys(dirs)), "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1"}


def py(script, *args, env, cwd):
    return subprocess.run([sys.executable, "-I", "-S", "-c", BLOCK_YAML, str(script), *map(str, args)],
                          capture_output=True, text=True, env=env, cwd=str(cwd))


def sh(script, *args, env, cwd, bash="bash"):
    return subprocess.run([bash, str(script), *map(str, args)], capture_output=True, text=True,
                          env=env, cwd=str(cwd))


def write(path, text, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode:
        path.chmod(mode)


def install_into(dest, modules):
    r = subprocess.run(["bash", str(INSTALLER), "install", str(dest), *modules, "--allow-dirty"],
                       capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert r.returncode == 0, r.stdout + r.stderr


_FULL = {}


def full_install():
    """One install of EVERY module in the factory, made once and copied per test."""
    if "dir" not in _FULL:
        d = Path(tempfile.mkdtemp(prefix="hg-full-")).resolve()
        install_into(d / "t", [])  # no names: the installer installs every module
        _FULL["dir"] = d
    return _FULL["dir"] / "t"


def tearDownModule():
    if "dir" in _FULL:
        shutil.rmtree(str(_FULL["dir"]), True)


class Harness(unittest.TestCase):
    """A 2.0 target with the harness-governance module installed, moved away from the installer."""

    def make_target(self, modules=None):
        """modules=None installs every module of the factory; a list installs just those."""
        self.root = Path(tempfile.mkdtemp(prefix="hg-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.root), True)
        if modules is None:
            staging = full_install()
        else:
            staging = self.root / "staging" / "t"
            install_into(staging, modules)
        # the install is moved: nothing in it may depend on the path it was installed to
        self.target = self.root / "moved" / "project"
        self.target.parent.mkdir()
        shutil.copytree(staging, self.target)
        if modules is not None:
            shutil.rmtree(str(self.root / "staging"))
        self.env = clean_env(self.root)
        return self.target

    # command helpers: installed copies only, yaml blocked, cwd = the target
    def scan(self, out=None, target=None):
        args = [target or self.target]
        if out:
            args += ["--out", out]
        return sh(self.target / SCRIPTS / "scan.sh", *args, env=self.env, cwd=self.target)

    def scan_file(self):
        out = self.root / "scan.json"
        r = self.scan(out=out)
        self.assertEqual(r.returncode, 0, r.stderr)
        return out

    def diff(self, scan=None):
        scan = scan or self.scan_file()
        return py(self.target / SCRIPTS / "diff-registry.py", scan, self.target / REGISTRY,
                  env=self.env, cwd=self.target)

    def init(self, *extra):
        return py(self.target / SCRIPTS / "registry.py", "init", self.target, *extra, env=self.env,
                  cwd=self.target)

    def registry(self):
        out = {}
        for f in sorted((self.target / REGISTRY).glob("*.json")):
            if f.name not in ("_schema.json", "_ignored.json"):
                out[f.name] = json.loads(f.read_text())
        return out

    def entries(self):
        ents = {}
        for doc in self.registry().values():
            for e in doc["entries"]:
                ents[e["id"]] = e
        return ents

    def manifest(self):
        return json.loads((self.target / ".devkit-manifest.json").read_text())


# ------------------------------------------------------------------ module shape

@unittest.skipUnless(_devkit is not None, "needs PyYAML (the installer)")
class ModuleShape(unittest.TestCase):
    def meta(self):
        return _devkit.discover_modules()["harness-governance"]

    def test_valid_v2_module_in_domain_all_at_its_current_version(self):
        m = self.meta()
        self.assertEqual(m["_errors"], [])
        self.assertEqual(m["domain"], "all")
        self.assertEqual(str(m["version"]), "1.0.16")

    def test_every_payload_entry_declares_its_mechanism(self):
        mech = {e["dest"]: e["mechanism"] for e in self.meta()["payload"]}
        self.assertEqual(mech, {
            SCRIPTS + "/scan.sh": "VERIFY",
            SCRIPTS + "/diff-registry.py": "VERIFY",
            SCRIPTS + "/registry.py": "VERIFY",
            REGISTRY + "/_schema.json": "VERIFY",
            ".claude/skills/harness-audit/SKILL.md": "INSTRUCT",
            ".claude/rules/harness-governance.md": "INSTRUCT",
        })

    def test_body_has_the_three_sections_and_documents_the_registry_location(self):
        secs = self.meta()["_sections"]
        for h in ("Prevents", "Procedure", "State"):
            self.assertTrue(secs[h].strip(), h)
        self.assertIn(REGISTRY, secs["State"])
        self.assertIn("registry.py init", secs["Procedure"])

    def test_prevents_names_what_the_scan_does_not_cover(self):
        text = " ".join(self.meta()["_sections"]["Prevents"].split())
        self.assertIn("devkit-audit.py", text)
        self.assertIn("not scanned", text)

    def test_payload_files_exist(self):
        for f in (SCAN_SRC, DIFF_SRC, REG_SRC, SCHEMA, SKILL, RULE):
            self.assertTrue(f.is_file(), str(f))


# ------------------------------------------------------------------ factory-unreachable

@unittest.skipUnless(HAVE_JQ, "jq is not installed (the scanner needs it)")
@unittest.skipUnless(_devkit is not None, "needs PyYAML (the installer) to build the target")
class FactoryUnreachable(Harness):
    def test_the_yaml_block_is_real(self):
        probe = Path(tempfile.mkdtemp(prefix="hg-probe-")) / "probe.py"
        self.addCleanup(shutil.rmtree, str(probe.parent), True)
        probe.write_text("import yaml\n")
        r = subprocess.run([sys.executable, "-I", "-S", "-c", BLOCK_YAML, str(probe)],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("yaml", r.stderr)

    def test_scan_diff_and_registry_init_run_without_factory_and_without_yaml(self):
        self.make_target()
        self.assertFalse((self.root / "staging").exists())
        # scan
        out = self.root / "scan.json"
        r = self.scan(out=out)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(out.read_text())
        self.assertEqual(sorted(doc), ["entries", "findings", "scan"])
        self.assertNotIn(str(REPO), out.read_text())
        # registry init (yaml blocked)
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(str(REPO), r.stdout + r.stderr)
        # diff (yaml blocked)
        r = self.diff(out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CLEAN", r.stdout)

    def test_scripts_import_no_yaml_and_name_no_factory_path(self):
        for f in (SCAN_SRC, DIFF_SRC, REG_SRC):
            text = f.read_text()
            self.assertNotRegex(text, r"(?m)^\s*(import|from)\s+yaml\b", f.name)
            for bad in ("devkit/", "$HOME", "~/"):
                self.assertNotIn(bad, text, "%s mentions %s" % (f.name, bad))

    def test_scan_needs_only_the_target(self):
        # a target that was never installed by the kit: no manifest, no module files
        self.make_target(["audit", "harness-governance"])
        bare = self.root / "bare"
        write(bare / ".claude/hooks/x.sh", "#!/bin/bash\n# Enforces: rules/x.md\nexit 0\n")
        out = self.root / "bare.json"
        r = self.scan(out=out, target=bare)
        self.assertEqual(r.returncode, 0, r.stderr)
        ids = [e["id"] for e in json.loads(out.read_text())["entries"]]
        self.assertEqual(ids, ["hook-x"])

    def test_scan_runs_under_the_system_bash(self):
        bash = "/bin/bash"
        if not os.path.exists(bash):
            self.skipTest("no /bin/bash")
        self.make_target(["audit", "harness-governance", "git-multi-session"])
        r = sh(self.target / SCRIPTS / "scan.sh", self.target, env=self.env, cwd=self.target, bash=bash)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["entries"])

    def test_scan_leaves_no_stale_report_when_it_fails(self):
        self.make_target(["audit", "harness-governance"])
        out = self.root / "scan.json"
        out.write_text('{"stale": true}')
        r = self.scan(out=out, target=self.root / "no-such-dir")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(out.exists(), "a stale report survived a failed scan")
        out.write_text('{"stale": true}')
        empty = self.root / "nojq2"
        empty.mkdir()
        for tool in ("bash", "cat", "grep", "sed", "awk", "sort", "tr", "wc", "basename", "dirname", "mktemp",
                     "rm", "head", "cut", "ls", "git", "env"):
            p = shutil.which(tool)
            if p:
                os.symlink(p, str(empty / tool))
        env = dict(self.env, PATH=str(empty))
        r = sh(self.target / SCRIPTS / "scan.sh", self.target, "--out", out, env=env, cwd=self.target,
               bash=shutil.which("bash"))
        self.assertEqual(r.returncode, 2)
        self.assertFalse(out.exists(), "a stale report survived a scan without jq")

    def test_invalid_arguments_leave_an_existing_report_alone(self):
        self.make_target(["audit", "harness-governance"])
        out = self.root / "keep.json"
        out.write_text('{"keep": true}')
        r = sh(self.target / SCRIPTS / "scan.sh", self.target, "--out", out, "--bogus", env=self.env, cwd=self.target)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(out.read_text(), '{"keep": true}')
        r = sh(self.target / SCRIPTS / "scan.sh", self.target, "--out", env=self.env, cwd=self.target)
        self.assertEqual(r.returncode, 1)

    def test_the_skill_stops_when_the_scan_fails(self):
        text = SKILL.read_text()
        self.assertRegex(text, r"(?i)non-zero[^.]*stop|stop[^.]*non-zero")

    def sandbox_profile(self):
        """macOS sandbox profile denying every read below the factory checkout, or None."""
        exe = shutil.which("sandbox-exec")
        if not exe:
            return None
        prof = '(version 1)(allow default)(deny file-read* (subpath "%s"))' % REPO
        probe = subprocess.run([exe, "-p", prof, "true"], capture_output=True, text=True)
        if probe.returncode != 0:
            return None
        # prove the sandbox is real: the checkout must not be listable inside it
        ls = subprocess.run([exe, "-p", prof, "ls", str(REPO)], capture_output=True, text=True)
        return prof if ls.returncode != 0 else None

    def test_everything_runs_with_the_factory_checkout_unreadable(self):
        prof = self.sandbox_profile()
        if prof is None:
            self.skipTest("needs macOS sandbox-exec (denying reads of the factory checkout); not available here")
        self.make_target()
        # the manifest names no factory path; point its recorded source at nothing as well
        man = self.manifest()
        man["source_commit"] = "0" * 40
        (self.target / ".devkit-manifest.json").write_text(json.dumps(man))
        self.assertNotIn(str(REPO), (self.target / ".devkit-manifest.json").read_text())
        exe = shutil.which("sandbox-exec")

        def boxed(cmd):
            return subprocess.run([exe, "-p", prof, *cmd], capture_output=True, text=True, env=self.env,
                                  cwd=str(self.target))
        out = self.root / "scan.json"
        r = boxed(["bash", str(self.target / SCRIPTS / "scan.sh"), str(self.target), "--out", str(out)])
        self.assertEqual(r.returncode, 0, r.stderr)
        r = boxed([sys.executable, "-I", "-S", "-c", BLOCK_YAML, str(self.target / SCRIPTS / "registry.py"),
                   "init", str(self.target)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = boxed([sys.executable, "-I", "-S", "-c", BLOCK_YAML, str(self.target / SCRIPTS / "diff-registry.py"),
                   str(out), str(self.target / REGISTRY)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CLEAN", r.stdout)

    def test_scan_without_jq_refuses_plainly(self):
        self.make_target(["audit", "harness-governance"])
        empty = self.root / "nojq"
        empty.mkdir()
        for tool in ("bash", "cat", "grep", "sed", "awk", "sort", "tr", "wc", "basename", "dirname", "mktemp",
                     "rm", "head", "cut", "ls", "git", "env"):
            p = shutil.which(tool)
            if p:
                os.symlink(p, str(empty / tool))
        env = dict(self.env, PATH=str(empty))
        r = sh(self.target / SCRIPTS / "scan.sh", self.target, env=env, cwd=self.target, bash=shutil.which("bash"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("jq", r.stderr)


# ------------------------------------------------------------------ scanner behavior

@unittest.skipUnless(HAVE_JQ, "jq is not installed (the scanner needs it)")
class ScannerBehavior(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="hg-scan-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.env = clean_env(self.root)
        t = self.t = self.root / "proj"
        write(t / ".claude/hooks/blocker.sh", "#!/bin/bash\n# Enforces: rules/r.md\n# exit 2 in a comment\n"
              "[ -n \"$X\" ] && exit 2\nexit 0\n# BLOCKER_DISABLED mentioned in a comment\n")
        write(t / ".claude/hooks/hatch.sh", "#!/bin/bash\n# Enforces: rules/r.md\n"
              "[ \"$HATCH_DISABLED\" = 1 ] && exit 0\nexit 2\n")
        write(t / ".claude/hooks/soft.sh", "#!/bin/bash\n# Enforces: rules/r.md\n# exit 2 only in a comment\nexit 0\n")
        write(t / ".claude/hooks/loose.sh", "#!/bin/bash\necho hi\n")
        write(t / ".claude/hooks/dangling.sh", "#!/bin/bash\n# Enforces: rules/nowhere.md\nexit 0\n")
        write(t / ".claude/rules/r.md", "# R\nrun `bash .claude/hooks/manual.sh` after edits\n")
        write(t / ".claude/hooks/manual.sh", "#!/bin/bash\n# Enforces: rules/r.md\nexit 0\n")
        write(t / ".claude/skills/good/SKILL.md", "---\nname: good\n---\n")
        (t / ".claude/skills/hollow").mkdir(parents=True)
        write(t / ".claude/agents/helper.md", "# helper\n")
        write(t / ".claude/scripts/tool/run.sh", "echo\n")
        write(t / "CLAUDE.md", "| Constraint | Home |\n|---|---|\n| be nice | rules/r.md |\n")
        write(t / ".claude/settings.json", json.dumps({
            "permissions": {"deny": ["Bash(rm -rf:*)"], "ask": ["Bash(sudo:*)"]},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "bash .claude/hooks/blocker.sh"},
                {"type": "command", "command": "bash .claude/hooks/hatch.sh"},
                {"type": "command", "command": "bash .claude/hooks/soft.sh"},
                {"type": "command", "command": "bash .claude/hooks/dangling.sh"},
                {"type": "command", "command": "bash -c 'echo hi; exit 2'"}]}]}}))
        r = sh(SCAN_SRC, t, env=self.env, cwd=t)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.doc = json.loads(r.stdout)
        self.ent = {e["id"]: e for e in self.doc["entries"]}
        self.find = [(f["check"], f["artifact"]) for f in self.doc["findings"]]

    def test_mechanism_is_behavior_not_filetype(self):
        self.assertEqual(self.ent["hook-blocker"]["mechanism"], "BLOCK")
        self.assertEqual(self.ent["hook-soft"]["mechanism"], "DETECT")  # `exit 2` only in a comment
        self.assertEqual(self.ent["settings-deny"]["mechanism"], "BLOCK")
        self.assertEqual(self.ent["settings-ask"]["mechanism"], "ASK")
        self.assertEqual(self.ent["rule-r"]["mechanism"], "INSTRUCT")
        self.assertEqual(self.ent["skill-good"]["mechanism"], "INSTRUCT")
        self.assertEqual(self.ent["script-tool"]["mechanism"], "VERIFY")
        self.assertEqual(self.ent["claude-md-pointers"]["mechanism"], "ROUTE")
        self.assertEqual(self.ent["settings-deny"]["patterns"], ["Bash(rm -rf:*)"])

    def test_wiring_manual_invocation_and_unwired(self):
        self.assertEqual(self.ent["hook-blocker"]["wired_at"], "PreToolUse:Bash")
        self.assertEqual(self.ent["hook-loose"]["status"], "unwired")
        self.assertEqual(self.ent["hook-loose"]["wired_at"], "UNWIRED")
        self.assertEqual(self.ent["hook-manual"]["status"], "active")
        self.assertIn("manual", self.ent["hook-manual"]["wired_at"])

    def test_escape_hatch_is_read_from_code_not_comments(self):
        self.assertEqual(self.ent["hook-hatch"]["escape_hatch"], "HATCH_DISABLED")
        self.assertEqual(self.ent["hook-blocker"]["escape_hatch"], "none")

    def test_external_wiring_is_an_entry(self):
        ext = [e for e in self.doc["entries"] if e["id"].startswith("ext-")]
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]["mechanism"], "BLOCK")

    def test_mechanical_findings(self):
        self.assertIn(("unwired-file", "hooks/loose.sh"), self.find)
        self.assertIn(("missing-enforces", "hooks/loose.sh"), self.find)
        self.assertIn(("dangling-enforces", "hooks/dangling.sh"), self.find)
        self.assertIn(("block-no-escape-hatch", "hooks/blocker.sh"), self.find)
        self.assertNotIn(("block-no-escape-hatch", "hooks/hatch.sh"), self.find)
        self.assertIn(("skill-without-manifest", "skills/hollow"), self.find)
        self.assertNotIn(("unwired-file", "hooks/manual.sh"), self.find)

    def test_malformed_settings_is_a_finding_not_an_empty_file(self):
        t = self.root / "bad"
        write(t / ".claude/hooks/h.sh", "#!/bin/bash\n# Enforces: none\nexit 0\n")
        write(t / ".claude/settings.json", "{ not json")
        write(t / ".claude/settings.local.json", "{}")
        r = sh(SCAN_SRC, t, env=self.env, cwd=t)
        self.assertEqual(r.returncode, 0, r.stderr)
        found = [(f["check"], f["artifact"]) for f in json.loads(r.stdout)["findings"]]
        self.assertIn(("settings-unreadable", "settings.json"), found)
        self.assertNotIn(("settings-unreadable", "settings.local.json"), found)
        write(t / ".claude/settings.local.json", "[1,")
        found = [(f["check"], f["artifact"]) for f in json.loads(sh(SCAN_SRC, t, env=self.env, cwd=t).stdout)["findings"]]
        self.assertIn(("settings-unreadable", "settings.local.json"), found)

    def test_a_valid_settings_file_is_not_flagged(self):
        self.assertNotIn("settings-unreadable", [c for c, _ in self.find])

    def test_output_is_deterministic_and_target_is_not_modified(self):
        before = sorted(str(p) for p in self.t.rglob("*"))
        a = sh(SCAN_SRC, self.t, env=self.env, cwd=self.t).stdout
        b = sh(SCAN_SRC, self.t, env=self.env, cwd=self.t).stdout
        self.assertEqual(a, b)
        self.assertEqual(before, sorted(str(p) for p in self.t.rglob("*")))

    def test_unknown_flags_and_factory_flags_are_refused(self):
        for flag in (["--surface", "src"], ["--home", "/x"], ["--bogus"]):
            r = sh(SCAN_SRC, self.t, *flag, env=self.env, cwd=self.t)
            self.assertNotEqual(r.returncode, 0, flag)


# ------------------------------------------------------------------ baseline-then-drift

@unittest.skipUnless(HAVE_JQ, "jq is not installed (the scanner needs it)")
@unittest.skipUnless(_devkit is not None, "needs PyYAML (the installer) to build the target")
class BaselineThenDrift(Harness):
    OWN_PREFIX = {"hook": ".claude/hooks/", "rule-file": ".claude/rules/"}

    def owner_domains(self):
        """{installed path: module domain}, computed from the manifest alone."""
        out = {}
        for inst in self.manifest()["installs"]:
            for f in inst["files"]:
                out[f["path"]] = inst["domain"]
        return out

    def test_init_seeds_domain_from_each_modules_manifest_projection(self):
        self.make_target()
        write(self.target / ".claude/rules/house-style.md", "# house style\n")  # not owned by a module
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        own = self.owner_domains()
        ents = self.entries()
        checked = 0
        for e in ents.values():
            if e["artifact_type"] == "hook":
                path = ".claude/hooks/" + e["name"]
            elif e["artifact_type"] == "rule-file":
                path = ".claude/" + e["name"]
            else:
                continue
            want = own.get(path, "TODO-curate")
            self.assertEqual(e["domain"], want, e["id"])
            checked += 1
        self.assertGreaterEqual(checked, 6)
        self.assertEqual(ents["rule-house-style"]["domain"], "TODO-curate")
        self.assertEqual(ents["rule-biz-outcome-gate"]["domain"], "decision-gating-business")
        self.assertEqual(ents["rule-technical-gate"]["domain"], "decision-gating-technical")
        self.assertEqual(ents["rule-git-hygiene"]["domain"], "git-multi-session")
        self.assertEqual(ents["rule-harness-governance"]["domain"], "all")
        self.assertEqual(ents["skill-harness-audit"]["domain"], "all")
        self.assertEqual(ents["script-harness-audit"]["domain"], "all")
        self.assertEqual(ents["claude-md-pointers"]["domain"], "all")
        # one file per seeded domain, named after it; the uncurated ones sit apart
        files = self.registry()
        self.assertIn("git-multi-session.json", files)
        self.assertIn("all.json", files)
        for name, doc in files.items():
            doms = {e["domain"] for e in doc["entries"]}
            if name.startswith("_"):
                self.assertEqual(doms, {"TODO-curate"}, name)
            else:
                self.assertEqual(doms, {doc["_meta"]["domain"]}, name)
        # every entry carries the curated fields
        for e in ents.values():
            for k in ("domain", "origin", "gaps"):
                self.assertIn(k, e, e["id"])

    def test_settings_patterns_are_split_by_the_module_that_contributed_them(self):
        self.make_target()
        settings = json.loads((self.target / ".claude/settings.json").read_text())
        settings["permissions"]["deny"].append("Bash(my-own-rule:*)")
        (self.target / ".claude/settings.json").write_text(json.dumps(settings))
        self.assertEqual(self.init().returncode, 0)
        ents = self.entries()
        merged = {i["module"]: i.get("merged", {}).get(".claude/settings.json", {})
                  for i in self.manifest()["installs"]}
        for module, kinds in merged.items():
            for kind, pats in kinds.items():
                if not pats:
                    continue
                dom = next(i["domain"] for i in self.manifest()["installs"] if i["module"] == module)
                e = ents["settings-%s-%s" % (kind, dom)]
                self.assertEqual(e["domain"], dom)
                self.assertTrue(set(pats) <= set(e["patterns"]), (module, kind))
        mine = [e for e in ents.values() if "Bash(my-own-rule:*)" in e.get("patterns", [])]
        self.assertEqual([e["domain"] for e in mine], ["TODO-curate"])
        self.assertEqual(self.diff().returncode, 0)

    def test_gate_hook_keeps_the_mechanism_its_module_declares(self):
        # the hook exits 2 in block mode, so the scanner proposes BLOCK; the module ships it as DETECT
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        e = self.entries()["hook-outcome-block-check"]
        self.assertEqual(e["mechanism"], "DETECT")
        self.assertIn("decision-gating-business", e["mechanism_override_reason"])
        self.assertEqual(self.diff().returncode, 0)

    def test_unreadable_settings_fails_the_audit_with_one_named_line(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        (self.target / ".claude/settings.json").write_text("{ not json")
        r = self.diff()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("settings-unreadable: settings.json", r.stdout)

    def test_init_refuses_when_settings_json_is_unreadable_and_writes_nothing(self):
        self.make_target()
        (self.target / ".claude/settings.json").write_text("{ not json")
        r = self.init()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        lines = [l for l in r.stderr.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertIn("settings-unreadable", lines[0])
        self.assertIn("settings.json", lines[0])
        self.assertEqual(self.registry(), {}, "no baseline may be seeded from an unreadable file")
        # the same through a saved scan, and also for settings.local.json
        scan = self.scan_file()
        r = self.init("--scan", str(scan))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("settings-unreadable", r.stderr)
        self.assertEqual(self.registry(), {})
        (self.target / ".claude/settings.json").write_text("{}")
        (self.target / ".claude/settings.local.json").write_text("[1,")
        r = self.init()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("settings.local.json", r.stderr)
        # a valid file seeds as before
        (self.target / ".claude/settings.local.json").write_text("{}")
        self.assertEqual(self.init().returncode, 0)

    def test_second_run_reports_zero_drift(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        r = self.diff()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("registry-vs-reality: CLEAN (0 diffs)", r.stdout)
        r = self.diff()  # and again: a fresh scan each time
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_planted_unwired_hook_is_reported(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        write(self.target / ".claude/hooks/planted.sh", "#!/bin/bash\n# Enforces: rules/git-hygiene.md\nexit 0\n",
              0o755)
        scan = self.scan_file()
        r = self.diff(scan)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("missing-from-registry: hook-planted", r.stdout)
        # the scanner's own gap list names it too
        finds = json.loads(scan.read_text())["findings"]
        self.assertIn({"check": "unwired-file", "artifact": "hooks/planted.sh"},
                      [{k: f[k] for k in ("check", "artifact")} for f in finds])

    def test_deleted_managed_rule_is_reported(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        (self.target / ".claude/rules/git-hygiene.md").unlink()
        r = self.diff()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("missing-from-reality: rule-git-hygiene", r.stdout)

    def test_wiring_a_hook_is_drift_until_the_registry_is_rebaselined(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        ents = self.entries()
        ents["rule-git-hygiene"]["domain"]  # sanity
        # hand-curate, then wire a hook
        f = self.target / REGISTRY / "git-multi-session.json"
        doc = json.loads(f.read_text())
        for e in doc["entries"]:
            if e["id"] == "rule-git-hygiene":
                e["origin"] = "my own note"
        f.write_text(json.dumps(doc))
        write(self.target / ".claude/settings.json", json.dumps(
            dict(json.loads((self.target / ".claude/settings.json").read_text()),
                 hooks={"PostToolUse": [{"matcher": "Bash", "hooks": [
                     {"type": "command", "command": "bash .claude/hooks/stash-leak-check.sh"}]}]})))
        r = self.diff()
        self.assertEqual(r.returncode, 1)
        self.assertIn("drift: hook-stash-leak-check field wired_at", r.stdout)
        # init refuses to overwrite, with a way forward and nothing written
        before = {n: (self.target / REGISTRY / n).read_bytes() for n in self.registry()}
        r = self.init()
        self.assertEqual(r.returncode, 2)
        self.assertIn("--force", r.stderr + r.stdout)
        self.assertEqual(before, {n: (self.target / REGISTRY / n).read_bytes() for n in self.registry()})
        # re-baseline keeps what the owner curated
        r = self.init("--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.entries()["rule-git-hygiene"]["origin"], "my own note")
        self.assertEqual(self.diff().returncode, 0)

    # ---- a full install: every module of the factory, module-only domains included
    def test_full_install_init_then_diff_is_clean_and_template_files_are_not_entries(self):
        self.make_target()
        names = {i["module"] for i in self.manifest()["installs"]}
        self.assertEqual(names, set(_devkit.discover_modules()))
        self.assertIn("skill-template", names)
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("template.json", self.registry())
        for e in self.entries().values():
            self.assertNotEqual(e["domain"], "template", e["id"])
        self.assertNotIn("skill-EXAMPLE-SKILL", self.entries())
        ignored = json.loads((self.target / REGISTRY / "_ignored.json").read_text())["ignored"]
        self.assertEqual([i["id"] for i in ignored], ["skill-EXAMPLE-SKILL"])
        self.assertIn("template", ignored[0]["reason"])
        r = self.diff()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("invalid-domain", r.stdout)
        self.assertIn("CLEAN", r.stdout)

    def test_an_ignored_template_skill_may_be_deleted_or_edited_without_a_finding(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        shutil.rmtree(str(self.target / ".claude/skills/EXAMPLE-SKILL"))
        self.assertEqual(self.diff().returncode, 0)

    def test_full_install_planted_hook_and_deleted_rule_are_still_reported(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        write(self.target / ".claude/hooks/planted.sh", "#!/bin/bash\nexit 0\n", 0o755)
        (self.target / ".claude/rules/plan-writing.md").unlink()
        r = self.diff()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("missing-from-registry: hook-planted", r.stdout)
        self.assertIn("missing-from-reality: rule-plan-writing", r.stdout)

    # ---- --force keeps what a person curated (and only that)
    def edit_entry(self, entry_id, **changes):
        for f in (self.target / REGISTRY).glob("*.json"):
            if f.name in ("_schema.json", "_ignored.json"):
                continue
            doc = json.loads(f.read_text())
            for e in doc["entries"]:
                if e["id"] == entry_id:
                    e.update(changes)
                    f.write_text(json.dumps(doc))
                    return
        self.fail("no entry " + entry_id)

    def test_force_keeps_a_curated_domain_and_origin_over_the_modules(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        self.edit_entry("rule-git-hygiene", domain="work-craft", origin="moved on purpose")
        self.edit_entry("rule-plan-writing", origin="written by hand")
        self.assertEqual(self.init("--force").returncode, 0)
        e = self.entries()
        self.assertEqual(e["rule-git-hygiene"]["domain"], "work-craft")
        self.assertEqual(e["rule-git-hygiene"]["origin"], "moved on purpose")
        self.assertEqual(e["rule-plan-writing"]["origin"], "written by hand")
        self.assertEqual(e["rule-plan-writing"]["domain"], "work-craft")
        self.assertIn("rule-git-hygiene", [x["id"] for x in self.registry()["work-craft.json"]["entries"]])
        self.assertEqual(self.diff().returncode, 0)

    def _outside_symlink(self, which):
        """Move .claude (or just its registry) outside the target, leave a symlink, add a decoy json."""
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        outside = self.root / "outside"
        outside.mkdir()
        link = self.target / (".claude" if which == "claude" else REGISTRY)
        shutil.move(str(link), str(outside / "moved"))
        os.symlink(str(outside / "moved"), str(link))
        decoy = outside / "moved" / ("harness-registry/x.json" if which == "claude" else "x.json")
        decoy.write_text("{}")
        return decoy

    def test_default_registry_through_a_symlink_is_refused_and_deletes_nothing(self):
        for which in ("registry", "claude"):
            for extra in (("--force",), ()):
                with self.subTest(which=which, extra=extra):
                    decoy = self._outside_symlink(which)
                    r = self.init(*extra)
                    self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                    self.assertIn("--registry", r.stderr + r.stdout)
                    self.assertTrue(decoy.is_file())

    def test_default_registry_symlink_loop_is_refused_not_a_traceback(self):
        self.make_target()
        reg = self.target / REGISTRY
        shutil.rmtree(str(reg))
        os.symlink(reg.name, str(reg))                         # a loop: harness-registry -> harness-registry
        r = self.init()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("--registry", r.stderr + r.stdout)

    def test_explicit_registry_outside_the_target_still_works(self):
        self.make_target()
        outside = self.root / "elsewhere"
        r = self.init("--registry", str(outside))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(outside.glob("*.json")))

    def test_force_recomputes_a_generated_origin_and_a_todo_domain(self):
        self.make_target()
        write(self.target / ".claude/rules/house-style.md", "# house style\n")
        self.assertEqual(self.init().returncode, 0)
        self.edit_entry("rule-house-style", domain="all", origin="our style guide")
        self.edit_entry("rule-git-hygiene", origin="installed by module git-multi-session 0.0.1")  # generated form
        self.assertEqual(self.init("--force").returncode, 0)
        e = self.entries()
        self.assertEqual((e["rule-house-style"]["domain"], e["rule-house-style"]["origin"]), ("all", "our style guide"))
        self.assertNotIn("0.0.1", e["rule-git-hygiene"]["origin"])

    def all_ids(self):
        ids = []
        for doc in self.registry().values():
            ids += [e["id"] for e in doc["entries"]]
        return ids

    def test_permission_groups_of_two_modules_in_one_domain_share_one_entry_with_a_unique_id(self):
        self.make_target()
        man = self.manifest()
        for inst in man["installs"]:
            if inst["module"] == "secrets-privacy":
                inst["domain"] = "system-safety"  # two modules now contribute patterns to one domain
        (self.target / ".devkit-manifest.json").write_text(json.dumps(man))
        self.assertEqual(self.init().returncode, 0)
        ids = self.all_ids()
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids: %s" % sorted(i for i in ids if ids.count(i) > 1))
        deny = self.entries()["settings-deny-system-safety"]
        mine = [i for i in man["installs"] if i["module"] in ("system-safety", "secrets-privacy")]
        want = {p for i in mine for p in (i.get("merged") or {}).get(".claude/settings.json", {}).get("deny", [])}
        self.assertTrue(want <= set(deny["patterns"]))
        # a curated origin on the shared entry survives --force
        self.edit_entry("settings-deny-system-safety", origin="curated by hand")
        self.assertEqual(self.init("--force").returncode, 0)
        ids = self.all_ids()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.entries()["settings-deny-system-safety"]["origin"], "curated by hand")
        self.assertEqual(self.diff().returncode, 0)

    def test_force_drops_the_generated_unwired_note_once_wired_and_keeps_a_written_note(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        hook = self.entries()["hook-stash-leak-check"]
        self.assertTrue(any("not wired" in g for g in hook["gaps"]), hook["gaps"])
        self.edit_entry("hook-stash-leak-check", gaps=hook["gaps"] + ["a note written by the owner"])
        write(self.target / ".claude/settings.json", json.dumps(
            dict(json.loads((self.target / ".claude/settings.json").read_text()),
                 hooks={"PostToolUse": [{"matcher": "Bash", "hooks": [
                     {"type": "command", "command": "bash .claude/hooks/stash-leak-check.sh"}]}]})))
        self.assertEqual(self.init("--force").returncode, 0)
        self.assertEqual(self.entries()["hook-stash-leak-check"]["gaps"], ["a note written by the owner"])
        self.assertEqual(self.diff().returncode, 0)

    def test_force_recomputes_a_generated_override_and_keeps_a_written_one(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        gate = self.entries()["hook-outcome-block-check"]
        self.assertEqual(gate["mechanism"], "DETECT")
        # a generated reason follows the module's current declaration
        man = self.manifest()
        for inst in man["installs"]:
            for f in inst["files"]:
                if f["path"].endswith("outcome-block-check.sh"):
                    f["mechanism"] = "VERIFY"
        (self.target / ".devkit-manifest.json").write_text(json.dumps(man))
        self.assertEqual(self.init("--force").returncode, 0)
        gate = self.entries()["hook-outcome-block-check"]
        self.assertEqual(gate["mechanism"], "VERIFY")
        self.assertIn("VERIFY", gate["mechanism_override_reason"])
        self.assertNotIn("DETECT", gate["mechanism_override_reason"])
        # a written reason (and the mechanism it justifies) is kept
        self.edit_entry("hook-outcome-block-check", mechanism="BLOCK",
                        mechanism_override_reason="we run it in block mode on every machine")
        self.assertEqual(self.init("--force").returncode, 0)
        gate = self.entries()["hook-outcome-block-check"]
        self.assertEqual((gate["mechanism"], gate["mechanism_override_reason"]),
                         ("BLOCK", "we run it in block mode on every machine"))
        self.assertEqual(self.diff().returncode, 0)

    def test_a_hand_curated_group_origin_stays_with_its_patterns_not_the_modules(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        settings = json.loads((self.target / ".claude/settings.json").read_text())
        settings["permissions"]["deny"].append("Bash(foo:*)")
        (self.target / ".claude/settings.json").write_text(json.dumps(settings))
        self.assertEqual(self.init("--force").returncode, 0)
        self.assertIn("Bash(foo:*)", self.entries()["settings-deny-todo-curate"]["patterns"])
        self.edit_entry("settings-deny-todo-curate", domain="system-safety", origin="hand curated")
        for _ in range(2):  # and again: the result is stable
            self.assertEqual(self.init("--force").returncode, 0)
            ids = self.all_ids()
            self.assertEqual(len(ids), len(set(ids)), "duplicate ids")
            ents = self.entries()
            module = ents["settings-deny-system-safety"]
            self.assertNotIn("Bash(foo:*)", module["patterns"])
            self.assertTrue(module["origin"].startswith("merged by module"), module["origin"])
            mine = [e for i, e in ents.items() if i.startswith("settings-deny-system-safety-curated")]
            self.assertEqual(len(mine), 1, sorted(ents))
            mine = mine[0]
            self.assertEqual(mine["patterns"], ["Bash(foo:*)"])
            self.assertEqual((mine["domain"], mine["origin"]), ("system-safety", "hand curated"))
            self.assertNotIn("settings-deny-todo-curate", ents)
            self.assertEqual(self.diff().returncode, 0)

    # ---- a hand-written origin AND gaps note stay with exactly the patterns they were written for
    def module_deny(self, module):
        inst = [i for i in self.manifest()["installs"] if i["module"] == module][0]
        return set((inst.get("merged") or {}).get(".claude/settings.json", {}).get("deny", []))

    def holders(self, pattern):
        """The deny entries (any domain) that list the pattern."""
        return [e for e in self.entries().values()
                if e.get("artifact_type") == "deny-rule" and pattern in (e.get("patterns") or [])]

    def move_group(self, entry_id, domain, origin=None, gaps=None):
        changes = {"domain": domain}
        if origin is not None:
            changes["origin"] = origin
        if gaps is not None:
            changes["gaps"] = gaps
        self.edit_entry(entry_id, **changes)

    def assert_clean_ids(self):
        ids = self.all_ids()
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids: %s" % sorted(i for i in ids if ids.count(i) > 1))

    def test_moving_a_group_to_another_domain_keeps_its_origin_and_its_gaps_note(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        want = self.module_deny("system-safety")
        self.assertTrue(want)
        self.move_group("settings-deny-system-safety", "all", "origin A", ["review quarterly"])
        for _ in range(2):  # a second --force run changes nothing
            self.assertEqual(self.init("--force").returncode, 0)
            self.assert_clean_ids()
            e = self.holders(sorted(want)[0])
            self.assertEqual(len(e), 1)
            self.assertEqual((e[0]["domain"], e[0]["origin"], e[0]["gaps"]), ("all", "origin A", ["review quarterly"]))
            self.assertTrue(want <= set(e[0]["patterns"]))
            self.assertEqual(self.diff().returncode, 0)

    def test_a_gaps_note_alone_survives_a_domain_move(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        pat = sorted(self.module_deny("system-safety"))[0]
        self.move_group("settings-deny-system-safety", "all", gaps=["only a note"])
        for _ in range(2):
            self.assertEqual(self.init("--force").returncode, 0)
            self.assert_clean_ids()
            e = self.holders(pat)
            self.assertEqual(len(e), 1)
            self.assertEqual((e[0]["domain"], e[0]["gaps"]), ("all", ["only a note"]))
            self.assertEqual(self.diff().returncode, 0)

    def test_two_hand_curated_groups_in_one_domain_each_keep_their_own_origin_and_gaps(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        a, b = self.module_deny("system-safety"), self.module_deny("secrets-privacy")
        self.assertTrue(a and b and not (a & b))
        self.move_group("settings-deny-system-safety", "all", "origin A", ["note A"])
        self.move_group("settings-deny-secrets-privacy", "all", "origin B", ["note B"])
        for _ in range(2):
            self.assertEqual(self.init("--force").returncode, 0)
            self.assert_clean_ids()
            ea, eb = self.holders(sorted(a)[0]), self.holders(sorted(b)[0])
            self.assertEqual((len(ea), len(eb)), (1, 1))
            ea, eb = ea[0], eb[0]
            self.assertNotEqual(ea["id"], eb["id"])
            self.assertEqual((ea["origin"], ea["gaps"], set(ea["patterns"])), ("origin A", ["note A"], a))
            self.assertEqual((eb["origin"], eb["gaps"], set(eb["patterns"])), ("origin B", ["note B"], b))
            self.assertEqual(self.diff().returncode, 0)

    def test_a_third_hand_group_and_module_patterns_stay_apart(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        a, b = self.module_deny("system-safety"), self.module_deny("secrets-privacy")
        settings = json.loads((self.target / ".claude/settings.json").read_text())
        settings["permissions"]["deny"].append("Bash(foo:*)")
        (self.target / ".claude/settings.json").write_text(json.dumps(settings))
        self.assertEqual(self.init("--force").returncode, 0)
        self.move_group("settings-deny-system-safety", "all", "origin A", ["note A"])
        self.move_group("settings-deny-secrets-privacy", "all", "origin B")
        self.move_group("settings-deny-todo-curate", "all", "origin C", ["note C"])
        for _ in range(2):
            self.assertEqual(self.init("--force").returncode, 0)
            self.assert_clean_ids()
            got = {}
            for key, pats in (("A", a), ("B", b), ("C", {"Bash(foo:*)"})):
                e = self.holders(sorted(pats)[0])
                self.assertEqual(len(e), 1, key)
                self.assertEqual(set(e[0]["patterns"]), pats, key)
                got[key] = (e[0]["origin"], e[0]["gaps"])
            self.assertEqual(got, {"A": ("origin A", ["note A"]), "B": ("origin B", []), "C": ("origin C", ["note C"])})
            self.assertEqual(self.diff().returncode, 0)

    def test_a_moved_group_with_no_hand_text_still_lands_in_one_curated_entry(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        a, b = self.module_deny("system-safety"), self.module_deny("secrets-privacy")
        self.move_group("settings-deny-system-safety", "all")
        self.move_group("settings-deny-secrets-privacy", "all")
        for _ in range(2):
            self.assertEqual(self.init("--force").returncode, 0)
            self.assert_clean_ids()
            e = self.holders(sorted(a)[0])
            self.assertEqual(len(e), 1)
            self.assertTrue((a | b) <= set(e[0]["patterns"]))
            self.assertEqual(self.diff().returncode, 0)

    def test_force_lets_a_generated_domain_follow_the_module_and_keeps_a_curated_one(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        self.assertEqual(self.entries()["rule-git-hygiene"]["domain_from_module"], "git-multi-session")
        self.edit_entry("rule-plan-writing", domain="all")  # a person moves this one
        man = self.manifest()
        for inst in man["installs"]:
            if inst["module"] == "git-multi-session":
                inst["domain"] = "cost-control"  # the module's domain changes upstream
            if inst["module"] == "work-craft":
                inst["domain"] = "skill-governance"
        (self.target / ".devkit-manifest.json").write_text(json.dumps(man))
        self.assertEqual(self.init("--force").returncode, 0)
        ents = self.entries()
        self.assertEqual(ents["rule-git-hygiene"]["domain"], "cost-control")  # generated: follows the module
        self.assertEqual(ents["rule-communication-style"]["domain"], "skill-governance")
        self.assertEqual(ents["rule-plan-writing"]["domain"], "all")  # curated: kept
        self.assertEqual(self.diff().returncode, 0)

    def test_the_differ_prints_what_it_ignored_and_refuses_a_non_template_ignore(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        r = self.diff()
        self.assertIn("ignored: 1 (skill-EXAMPLE-SKILL)", r.stdout)
        f = self.target / REGISTRY / "_ignored.json"
        doc = json.loads(f.read_text())
        doc["ignored"].append({"id": "rule-plan-writing", "name": "rules/plan-writing.md", "module": "work-craft",
                               "domain": "work-craft", "reason": "hiding it"})
        f.write_text(json.dumps(doc))
        r = self.diff()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("ignored-non-template: rule-plan-writing", r.stdout)
        self.assertNotIn("ignored: 2", r.stdout)  # the bad one is not skipped
        (self.target / ".claude/rules/plan-writing.md").unlink()
        self.assertIn("missing-from-reality: rule-plan-writing", self.diff().stdout)

    def test_differ_reports_a_duplicate_id_across_files(self):
        self.make_target(["audit", "harness-governance"])
        self.assertEqual(self.init().returncode, 0)
        f = self.target / REGISTRY / "all.json"
        doc = json.loads(f.read_text())
        extra = dict(doc["entries"][0])
        write(self.target / REGISTRY / "git-multi-session.json",
              json.dumps({"_meta": {"domain": "git-multi-session"}, "entries": [extra]}))
        r = self.diff()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("duplicate-id: %s" % extra["id"], r.stdout)

    # ---- a domain is a file name: a missing or non-plain one never escapes the target or breaks init
    def _no_file_outside_target(self, name):
        self.assertEqual([p for p in self.root.rglob(name) if self.target not in p.parents], [])

    def test_manifest_domain_with_path_segments_is_uncurated_and_writes_nothing_outside(self):
        self.make_target()
        man = self.manifest()
        for inst in man["installs"]:
            inst["domain"] = "../../../TRAVERSED"
        (self.target / ".devkit-manifest.json").write_text(json.dumps(man))
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self._no_file_outside_target("TRAVERSED.json")
        self.assertEqual({e["domain"] for e in self.entries().values()}, {"TODO-curate"})
        self.assertIn("_uncurated.json", self.registry())

    def test_install_record_without_a_domain_does_not_break_init_or_force(self):
        self.make_target()
        man = self.manifest()
        victim = next(i for i in man["installs"] if i["module"] == "harness-governance")
        victim.pop("domain", None)
        (self.target / ".devkit-manifest.json").write_text(json.dumps(man))
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.edit_entry("rule-git-hygiene", origin="curated by hand")
        before = self.entries()
        r = self.init("--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.entries()["rule-git-hygiene"]["origin"], "curated by hand")
        self.assertEqual(set(self.entries()), set(before))

    def test_hand_edited_prior_domain_with_path_segments_is_not_written_outside(self):
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        self.edit_entry("rule-git-hygiene", domain="../evil")
        r = self.init("--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self._no_file_outside_target("evil.json")
        self.assertRegex(self.entries()["rule-git-hygiene"]["domain"], r"^[A-Za-z0-9-]+$")

    def test_force_builds_everything_before_deleting_the_old_registry(self):
        import importlib.util
        from unittest import mock
        self.make_target()
        self.assertEqual(self.init().returncode, 0)
        before = {n: (self.target / REGISTRY / n).read_bytes() for n in self.registry()}
        spec = importlib.util.spec_from_file_location("registry_under_test", str(REG_SRC))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        scan = self.scan_file()
        with mock.patch.object(mod.json, "dumps", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                mod.main(["init", str(self.target), "--force", "--scan", str(scan)])
        after = {n: (self.target / REGISTRY / n).read_bytes() for n in self.registry()}
        self.assertEqual(before, after)

    def test_target_without_a_manifest_is_all_uncurated(self):
        self.make_target(["audit", "harness-governance"])
        (self.target / ".devkit-manifest.json").unlink()
        self.assertEqual(self.init().returncode, 0)
        self.assertEqual({e["domain"] for e in self.entries().values()}, {"TODO-curate"})
        self.assertEqual(self.diff().returncode, 0)

    def test_uncurated_entries_are_noted_but_are_not_drift(self):
        self.make_target()
        write(self.target / ".claude/rules/house-style.md", "# house style\n")
        self.assertEqual(self.init().returncode, 0)
        r = self.diff()
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"uncurated: \d+ entr")

    def test_malformed_registry_is_exit_2_not_a_traceback(self):
        self.make_target(["audit", "harness-governance"])
        self.assertEqual(self.init().returncode, 0)
        (self.target / REGISTRY / "all.json").write_text("{not json")
        r = self.diff()
        self.assertEqual(r.returncode, 2)
        self.assertIn("registry-error:", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_init_does_not_touch_anything_outside_the_registry_directory(self):
        self.make_target()
        snap = {p: p.read_bytes() for p in self.target.rglob("*")
                if p.is_file() and REGISTRY not in str(p)}
        self.assertEqual(self.init().returncode, 0)
        after = {p: p.read_bytes() for p in self.target.rglob("*") if p.is_file() and REGISTRY not in str(p)}
        self.assertEqual(snap, after)


# ------------------------------------------------------------------ schema-shared

@unittest.skipUnless(_devkit is not None, "needs PyYAML (the installer)")
class SchemaFileOverride(unittest.TestCase):
    """The registry schema is an optional input; DEVKIT_REGISTRY_SCHEMA points the installer at another file."""

    def schema_file(self, **env):
        code = "import sys; sys.path.insert(0, %r); import _devkit; print(_devkit.SCHEMA_FILE)" % str(REPO / "devkit/install")
        e = {k: v for k, v in os.environ.items() if k != "DEVKIT_REGISTRY_SCHEMA"}
        e.update(env, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    @unittest.skipUnless(_devkit is not None, "needs PyYAML (the installer)")
    def test_default_and_override(self):
        self.assertTrue(self.schema_file().endswith("harness-registry/_schema.yaml"))
        self.assertEqual(self.schema_file(DEVKIT_REGISTRY_SCHEMA="/somewhere/else.yaml"), "/somewhere/else.yaml")


class SchemaShared(unittest.TestCase):
    def shipped(self):
        return json.loads(SCHEMA.read_text())

    @unittest.skipUnless(SCHEMA_YAML.is_file(), "optional input absent: no registry schema file")
    def test_shipped_schema_is_generated_from_the_registry_schema(self):
        self.assertEqual(SCHEMA.read_text(), _devkit.schema_json())

    def test_shipped_schema_vocabulary_equals_what_the_installer_validates_against(self):
        domains, mechs = _devkit.load_vocab()
        s = self.shipped()
        self.assertEqual(list(s["domains"]) + list(s["module_only_domains"]), domains)
        self.assertEqual(list(s["mechanisms"]), mechs)

    def test_domains_check_covers_the_shipped_schema(self):
        if not SCHEMA_YAML.is_file():
            self.skipTest("optional input absent: no registry schema file")
        self.assertEqual(_devkit.domains(check=True), 0)
        tmp = Path(tempfile.mkdtemp(prefix="hg-schema-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        stale = tmp / "_schema.json"
        stale.write_text(SCHEMA.read_text().replace('"BLOCK"', '"BLOCKED"', 1))
        saved = _devkit.SCHEMA_PAYLOAD_FILE
        _devkit.SCHEMA_PAYLOAD_FILE = stale
        self.addCleanup(setattr, _devkit, "SCHEMA_PAYLOAD_FILE", saved)
        self.assertEqual(_devkit.domains(check=True), 1)

    def test_every_domain_has_a_public_description_that_the_schema_uses(self):
        domains, _ = _devkit.load_vocab()
        self.assertEqual(sorted(_devkit.PUBLIC_DOMAIN_PREVENTS), sorted(domains))
        s = self.shipped()
        for name, d in list(s["domains"].items()) + list(s["module_only_domains"].items()):
            self.assertEqual(d["prevents"], _devkit.PUBLIC_DOMAIN_PREVENTS[name], name)

    def test_domains_check_also_proves_the_public_descriptions(self):
        if not SCHEMA_YAML.is_file():
            self.skipTest("optional input absent: no registry schema file")
        saved = dict(_devkit.PUBLIC_DOMAIN_PREVENTS)
        self.addCleanup(_devkit.PUBLIC_DOMAIN_PREVENTS.update, saved)
        _devkit.PUBLIC_DOMAIN_PREVENTS["work-craft"] = "something else entirely"
        self.assertEqual(_devkit.domains(check=True), 1)

    def test_differ_rejects_a_domain_the_shipped_schema_does_not_know(self):
        tmp = Path(tempfile.mkdtemp(prefix="hg-dom-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        reg = tmp / "reg"
        reg.mkdir()
        shutil.copy(SCHEMA, reg / "_schema.json")
        ent = {"id": "rule-x", "name": "rules/x.md", "artifact_type": "rule-file", "mechanism": "INSTRUCT",
               "wired_at": "always-on (rules/)", "status": "active", "domain": "made-up", "origin": "t", "gaps": []}
        write(reg / "made-up.json", json.dumps({"_meta": {"domain": "made-up"}, "entries": [ent]}))
        scan = tmp / "scan.json"
        scan.write_text(json.dumps({"scan": {}, "entries": [{k: v for k, v in ent.items()
                                                              if k not in ("domain", "origin", "gaps")}],
                                    "findings": []}))
        r = py(DIFF_SRC, scan, reg, env=clean_env(tmp), cwd=tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("invalid-domain: rule-x", r.stdout)
        self.assertIn("made-up", r.stdout)


# ------------------------------------------------------------------ owner-neutral shipped text

def have_lexicon():
    return build is not None and (REPO / "publish/lexicon.yaml").is_file()


class OwnerNeutral(unittest.TestCase):
    @unittest.skipUnless(have_lexicon(), "optional input absent: no lexicon")
    def test_lexicon_gate_zero_hits_on_every_shipped_file(self):
        lex = build.parse_lexicon((REPO / "publish/lexicon.yaml").read_text())
        files = {str(p.relative_to(REPO)): (False, p.read_bytes()) for p in SHIPPED}
        build.scan_lexicon(lex, files)  # raises Refusal on any hit

    def test_no_personal_absolute_path_in_any_shipped_file(self):
        for f in SHIPPED:
            self.assertNotRegex(f.read_text(), r"/(Users|home)/[A-Za-z0-9._-]+", f.name)


class SkillAndRule(unittest.TestCase):
    TOKEN = re.compile(r"`([^`\n]+)`")

    def installed_dests(self):
        md = (MOD / "MODULE.md").read_text()
        return set(re.findall(r'dest:\s*"([^"]+)"', md))

    def test_skill_procedure_names_only_paths_under_the_targets_claude_directory(self):
        text = SKILL.read_text()
        paths = [t for t in self.TOKEN.findall(text) if "/" in t and not t.startswith(("-", "$"))]
        paths += re.findall(r"(?<![`\w.<\/-])((?:\.claude|scripts|hooks|rules|skills|harness-registry)/[\w./<>*-]+)", text)
        self.assertTrue(paths)
        for p in paths:
            first = p.split()[-1] if p.startswith(("bash ", "python3 ")) else p
            for tok in re.findall(r"[\w.<>*-]+/[\w./<>*-]*", first):
                self.assertTrue(tok.startswith((".claude/", "<target>/.claude/")),
                                "skill names a path outside the target's .claude: %r" % tok)

    def test_skill_calls_only_shipped_scripts(self):
        text = SKILL.read_text()
        dests = self.installed_dests()
        for name in ("scan.sh", "diff-registry.py", "registry.py"):
            self.assertIn(SCRIPTS + "/" + name, text)
            self.assertIn(SCRIPTS + "/" + name, dests)
        for tok in re.findall(r"\.claude/[\w./-]+", text):
            tok = tok.rstrip(".")
            ok = (tok.rstrip("/") in (".claude/hooks", ".claude/rules", ".claude/skills", ".claude/agents")
                  or tok in dests or any(d.startswith(tok.rstrip("/") + "/") for d in dests)
                  or tok.startswith(REGISTRY + "/") or tok in (".claude/settings.json", ".claude/settings.local.json", ".claude/CLAUDE.md"))
            self.assertTrue(ok, "skill names a file the module does not ship or create: " + tok)

    def test_skill_has_a_name_and_description(self):
        text = SKILL.read_text()
        self.assertTrue(text.startswith("---\nname: harness-audit\n"))
        self.assertIn("description:", text.split("---")[1])

    def test_skill_keeps_the_hard_rules(self):
        text = SKILL.read_text()
        for needle in ("DRAFT", "READ-ONLY", "enforcing line", "## Findings", "registry.py init",
                       "diff-registry.py", "Known"):
            self.assertIn(needle, text)

    def test_rule_is_the_governance_rule(self):
        text = RULE.read_text()
        self.assertTrue(text.startswith("# Harness Governance"))
        for needle in ("Never hand-edit computed registry fields", "mechanism is behavior", "never auto-fix",
                       "DRAFT", REGISTRY + "/_schema.json", SCRIPTS + "/scan.sh"):
            self.assertIn(needle.lower(), text.lower())

    @unittest.skipUnless(have_lexicon() and (REPO / "publish/allowlist.yaml").is_file(),
                         "optional input absent: no build selection to resolve against")
    def test_pointers_resolve_inside_this_tree(self):
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard"], capture_output=True, check=True).stdout
        entries = {}
        for raw in out.split(b"\0"):
            if raw:
                rel = raw.decode()
                f = REPO / rel
                if f.is_file() and not f.is_symlink():
                    entries[rel] = ("file", f.stat().st_mode & 0o111 != 0, f.read_bytes())
        allow = build.parse_allowlist((REPO / "publish/allowlist.yaml").read_text())
        chosen, mapped = build.select(allow, entries)
        files = dict(chosen)
        files.update(mapped)
        mine = [p for p in files if p.startswith("devkit/modules/harness-governance/")]
        self.assertTrue(mine, "harness-governance files are missing from this tree")
        try:
            build.check_references(files)
        except build.Refusal as e:
            hits = [l for l in str(e).splitlines() if "harness-governance" in l]
            self.assertEqual(hits, [], "dangling pointer in a harness-governance file")


if __name__ == "__main__":
    unittest.main()
