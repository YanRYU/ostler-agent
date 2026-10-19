"""Tests for the MODULE.md contract v2: domain, per-file mechanism, settings fragments,
the manifest projection, the `board` card renderer and the generated README table.

Run from the repo root:
    bash devkit/tests/test-contract-v2.sh

Every test works on a throwaway copy of the factory (devkit/ and install/) inside a temp
directory, so nothing outside it is touched. Synthetic modules are added to that copy.
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
# Tests that need an optional parser skip when it is absent.
sys.path.insert(0, str(REPO / "publish"))
try:
    import build  # noqa: E402  (optional stdlib-only build module)
except ImportError:  # pragma: no cover
    build = None

ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def module_md(name, domain="system-safety", entries=(), extra="", sections=True, version="1.0.0"):
    """entries: [(src, dest, mode, mechanism)]"""
    lines = ["---", "name: %s" % name, 'version: "%s"' % version]
    if domain is not None:
        lines.append("domain: %s" % domain)
    lines.append("depends-on: []")
    lines.append("payload:")
    for src, dest, mode, mech in entries:
        parts = ['src: "%s"' % src, 'dest: "%s"' % dest, "mode: %s" % mode]
        if mech is not None:
            parts.append("mechanism: %s" % mech)
        lines.append("  - { %s }" % ", ".join(parts))
    lines.append("---")
    lines.append("# %s" % name)
    if sections:
        lines += ["", "## Prevents", "", "Prevents the %s kind of mistake." % name,
                  "", "## Procedure", "", "Run the thing, read the result.",
                  "", "## State", "", "Keeps no state outside its files."]
    lines.append(extra)
    return "\n".join(lines) + "\n"


class Factory:
    """A copy of the factory in a temp dir; `install()` runs the copy's installer."""

    def __init__(self, tc):
        self.root = Path(tempfile.mkdtemp(prefix="devkit-v2-"))
        tc.addCleanup(shutil.rmtree, self.root, True)
        shutil.copytree(REPO / "devkit", self.root / "fac" / "devkit",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        shutil.copytree(REPO / "install", self.root / "fac" / "install",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        self.fac = self.root / "fac"

    def add_module(self, name, md, payload):
        d = self.fac / "devkit" / "modules" / name
        (d / "payload").mkdir(parents=True)
        (d / "MODULE.md").write_text(md)
        for rel, text in payload.items():
            p = d / "payload" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def run(self, *args):
        return subprocess.run(
            ["bash", str(self.fac / "devkit" / "install" / "devkit-install.sh"), *map(str, args)],
            capture_output=True, text=True, env=ENV)

    def install(self, target, *mods, extra=()):
        return self.run("install", target, *mods, "--allow-dirty", *extra)

    def target(self, name="t"):
        t = self.root / name
        t.mkdir(exist_ok=True)
        return t


def settings(deny=(), ask=(), allow=()):
    return json.dumps({"permissions": {"deny": list(deny), "ask": list(ask), "allow": list(allow)}})


def manifest(target):
    return json.loads((Path(target) / ".devkit-manifest.json").read_text())


def two_fragment_factory(tc):
    f = Factory(tc)
    for name, dom, deny, ask in (
            ("frag-a", "system-safety", ["Bash(rm -rf:*)", "Shared(x)"], ["Bash(sudo:*)"]),
            ("frag-b", "secrets-privacy", ["Read(./.env)", "Shared(x)"], ["Bash(curl:*)"])):
        f.add_module(name, module_md(name, dom, [("s.json", ".claude/settings.json", "merge", "BLOCK")]),
                     {"s.json": settings(deny, ask)})
    return f


class V2ParsesBoth(unittest.TestCase):
    def test_v2_module_installs(self):
        f = Factory(self)
        f.add_module("good", module_md("good", "work-craft", [("r.md", ".claude/rules/r.md", "overwrite", "INSTRUCT")]),
                     {"r.md": "rule\n"})
        t = f.target()
        r = f.install(t, "good")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue((t / ".claude/rules/r.md").is_file())

    def test_v2_module_passes_build_parser(self):
        if build is None:
            self.skipTest("optional input absent: no build tooling")
        md = module_md("good", "work-craft", [("r.md", ".claude/rules/r.md", "overwrite", "INSTRUCT")])
        domains, mechs = build.load_vocab(
            (REPO / "devkit" / "domains.json").read_bytes())
        self.assertEqual(build.parse_payload("m/MODULE.md", md.encode(), domains, mechs),
                         [("r.md", ".claude/rules/r.md")])

    def _bad_installer(self, md, needle):
        f = Factory(self)
        f.add_module("bad", md, {"r.md": "rule\n"})
        t = f.root / "never-created"
        r = f.install(t, "bad")
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertIn(needle, r.stderr)
        self.assertFalse(t.exists(), "refusal must write nothing, not even the target dir")

    def _bad_build(self, md, needle):
        if build is None:
            return
        domains, mechs = build.load_vocab((REPO / "devkit" / "domains.json").read_bytes())
        with self.assertRaises(build.Refusal) as cm:
            build.parse_payload("m/MODULE.md", md.encode(), domains, mechs)
        self.assertIn(needle, str(cm.exception))

    def test_unknown_domain_fails_both(self):
        md = module_md("bad", "no-such-domain", [("r.md", ".claude/r.md", "overwrite", "INSTRUCT")])
        self._bad_installer(md, "unknown domain 'no-such-domain'")
        self._bad_build(md, "unknown domain 'no-such-domain'")

    def test_unknown_mechanism_fails_both(self):
        md = module_md("bad", "work-craft", [("r.md", ".claude/r.md", "overwrite", "SHOUT")])
        self._bad_installer(md, "unknown mechanism 'SHOUT'")
        self._bad_build(md, "unknown mechanism 'SHOUT'")

    def test_missing_mechanism_fails_both(self):
        md = module_md("bad", "work-craft", [("r.md", ".claude/r.md", "overwrite", None)])
        self._bad_installer(md, "mechanism")
        self._bad_build(md, "mechanism")

    def test_missing_domain_and_leftover_layer_fail_installer(self):
        self._bad_installer(module_md("bad", None, [("r.md", ".claude/r.md", "overwrite", "INSTRUCT")]), "domain")
        md = module_md("bad", "work-craft", [("r.md", ".claude/r.md", "overwrite", "INSTRUCT")]).replace(
            "depends-on: []", "layer: INSTRUCT\ndepends-on: []")
        self._bad_installer(md, "layer")

    def test_missing_body_section_fails_installer(self):
        md = module_md("bad", "work-craft", [("r.md", ".claude/r.md", "overwrite", "INSTRUCT")], sections=False)
        self._bad_installer(md, "## Prevents")

    def test_build_parser_still_rejects_unknown_key(self):
        md = module_md("bad", "work-craft", [("r.md", ".claude/r.md", "overwrite", "INSTRUCT")]).replace(
            "mechanism: INSTRUCT", "mechanism: INSTRUCT, bogus: x")
        self._bad_build(md, "bogus")


class SettingsFragments(unittest.TestCase):
    def test_two_fragments_install_and_manifest_records_patterns(self):
        f = two_fragment_factory(self)
        t = f.target()
        (t / ".claude").mkdir()
        (t / ".claude/settings.json").write_text(settings(deny=["Mine(deny)"], allow=["Mine(allow)"]))
        r = f.install(t, "frag-a", "frag-b")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        s = json.loads((t / ".claude/settings.json").read_text())["permissions"]
        self.assertEqual(s["allow"], ["Mine(allow)"])
        for p in ("Mine(deny)", "Bash(rm -rf:*)", "Read(./.env)", "Shared(x)"):
            self.assertIn(p, s["deny"])
        m = {i["module"]: i for i in manifest(t)["installs"]}
        self.assertEqual(m["frag-a"]["merged"][".claude/settings.json"],
                         {"deny": ["Bash(rm -rf:*)", "Shared(x)"], "ask": ["Bash(sudo:*)"]})
        self.assertEqual(m["frag-b"]["merged"][".claude/settings.json"],
                         {"deny": ["Read(./.env)", "Shared(x)"], "ask": ["Bash(curl:*)"]})
        # the user's own deny entry is never recorded as a contribution
        self.assertNotIn("Mine(deny)", json.dumps(m))
        # one merged file, so every record of it carries the final sha and the drift check is clean
        shas = {fr["sha256"] for i in m.values() for fr in i["files"] if fr["path"] == ".claude/settings.json"}
        self.assertEqual(len(shas), 1)
        chk = f.run("check", t)
        self.assertEqual(chk.returncode, 0, chk.stdout + chk.stderr)

    def test_remove_module_unmerges_only_its_patterns(self):
        f = two_fragment_factory(self)
        t = f.target()
        (t / ".claude").mkdir()
        (t / ".claude/settings.json").write_text(settings(deny=["Mine(deny)"], allow=["Mine(allow)"]))
        self.assertEqual(f.install(t, "frag-a", "frag-b").returncode, 0)
        r = f.run("remove-module", t, "frag-a")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        s = json.loads((t / ".claude/settings.json").read_text())["permissions"]
        self.assertEqual(s["allow"], ["Mine(allow)"])
        self.assertIn("Mine(deny)", s["deny"])
        self.assertIn("Read(./.env)", s["deny"])
        self.assertIn("Shared(x)", s["deny"], "a pattern both modules contributed stays until both are removed")
        self.assertIn("Bash(curl:*)", s["ask"])
        self.assertNotIn("Bash(rm -rf:*)", s["deny"])
        self.assertNotIn("Bash(sudo:*)", s["ask"])
        self.assertEqual(f.run("check", t).returncode, 0)
        r = f.run("remove-module", t, "frag-b")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        s = json.loads((t / ".claude/settings.json").read_text())["permissions"]
        self.assertEqual(s["deny"], ["Mine(deny)"])
        self.assertEqual(s["ask"], [])
        self.assertEqual(s["allow"], ["Mine(allow)"])

    def test_overwrite_second_claim_still_rejected(self):
        f = Factory(self)
        for n in ("ow-a", "ow-b"):
            f.add_module(n, module_md(n, "work-craft", [("r.md", ".claude/rules/x.md", "overwrite", "INSTRUCT")]),
                         {"r.md": n})
        t = f.root / "t2"
        r = f.install(t, "ow-a", "ow-b")
        self.assertEqual(r.returncode, 8)
        self.assertIn("already claimed", r.stderr)
        self.assertFalse(t.exists())

    def test_merge_then_overwrite_claim_rejected(self):
        f = Factory(self)
        f.add_module("mg", module_md("mg", "system-safety", [("s.json", ".claude/settings.json", "merge", "BLOCK")]),
                     {"s.json": settings(["A(1)"])})
        f.add_module("ow", module_md("ow", "work-craft", [("s.json", ".claude/settings.json", "overwrite", "INSTRUCT")]),
                     {"s.json": "{}"})
        r = f.install(f.root / "t3", "mg", "ow")
        self.assertEqual(r.returncode, 8)
        self.assertIn("already claimed", r.stderr)

    def test_fragments_installed_in_separate_runs(self):
        f = two_fragment_factory(self)
        t = f.target()
        self.assertEqual(f.install(t, "frag-a").returncode, 0)
        self.assertEqual(f.install(t, "frag-b").returncode, 0)
        self.assertEqual(f.run("check", t).returncode, 0)
        self.assertEqual(f.run("remove-module", t, "frag-a").returncode, 0)
        s = json.loads((t / ".claude/settings.json").read_text())["permissions"]
        self.assertIn("Shared(x)", s["deny"])
        self.assertNotIn("Bash(rm -rf:*)", s["deny"])


class ManifestProjectionAndBoard(unittest.TestCase):
    def test_manifest_projection_and_board_with_factory_deleted(self):
        f = Factory(self)
        f.add_module("gate", module_md(
            "gate", "decision-gating-business",
            [("hooks/g.sh", ".claude/hooks/g.sh", "overwrite", "DETECT"),
             ("rules/g.md", ".claude/rules/g.md", "overwrite", "INSTRUCT")]),
            {"hooks/g.sh": "#!/bin/sh\n# Enforces: rules/g.md\nexit 0\n", "rules/g.md": "rule\n"})
        f.add_module("fragx", module_md("fragx", "system-safety", [("s.json", ".claude/settings.json", "merge", "BLOCK")]),
                     {"s.json": settings(["Q(1)"])})
        t = f.target()
        self.assertEqual(f.install(t, "audit", "gate", "fragx").returncode, 0)
        m = {i["module"]: i for i in manifest(t)["installs"]}
        self.assertEqual(m["gate"]["domain"], "decision-gating-business")
        self.assertEqual({fr["path"]: fr["mechanism"] for fr in m["gate"]["files"]},
                         {".claude/hooks/g.sh": "DETECT", ".claude/rules/g.md": "INSTRUCT"})
        self.assertEqual(set(m["gate"]["sections"]), {"prevents", "procedure", "state"})
        self.assertIn("Prevents the gate kind", m["gate"]["sections"]["prevents"])
        # wire the hook, delete the factory, render the board from the installed copy only
        (t / ".claude/settings.json").write_text(json.dumps({
            "permissions": {"deny": ["Q(1)"], "ask": [], "allow": []},
            "hooks": {"PreToolUse": [{"matcher": "ExitPlanMode",
                                      "hooks": [{"type": "command", "command": "bash .claude/hooks/g.sh"}]}]}}))
        shutil.rmtree(f.fac)
        r = subprocess.run([sys.executable, str(t / ".claude/devkit-audit.py"), "board", str(t)],
                           capture_output=True, text=True, env=ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        cards = [c for c in re.split(r"(?m)^(?==== )", r.stdout) if c.startswith("=== ")]
        self.assertEqual(len(cards), 3, r.stdout)
        for c in cards:
            for face in ("purpose:", "surfaces:", "principles:", "procedure:", "state:"):
                m_ = re.search(r"(?m)^%s\s*(.*(?:\n  .*)*)" % re.escape(face), c)
                self.assertTrue(m_ and m_.group(1).strip(), "empty face %s in card:\n%s" % (face, c))
        gate = next(c for c in cards if c.startswith("=== gate"))
        self.assertIn("decision-gating-business", gate)
        self.assertIn("PreToolUse:ExitPlanMode", gate)
        self.assertIn(".claude/hooks/g.sh [DETECT]", gate)
        self.assertIn(".claude/rules/g.md", gate.split("principles:")[1].split("procedure:")[0])

    def test_board_on_empty_target_is_a_clean_message(self):
        t = Path(tempfile.mkdtemp(prefix="devkit-v2-empty-"))
        self.addCleanup(shutil.rmtree, t, True)
        r = subprocess.run([sys.executable,
                            str(REPO / "devkit/modules/audit/payload/devkit-audit.py"), "board", str(t)],
                           capture_output=True, text=True, env=ENV)
        self.assertEqual(r.returncode, 0)
        self.assertIn("no .devkit-manifest.json", r.stdout)


class ReadmeGenerated(unittest.TestCase):
    def test_readme_table_matches_generator(self):
        r = subprocess.run([sys.executable, str(REPO / "devkit/install/_devkit.py"), "readme", "--check"],
                           capture_output=True, text=True, env=ENV)
        self.assertEqual(r.returncode, 0,
                         "devkit/README.md module table differs from `_devkit.py readme`; "
                         "run: python3 devkit/install/_devkit.py readme\n" + r.stdout + r.stderr)

    def test_hand_edit_is_detected_and_regenerated(self):
        f = Factory(self)
        readme = f.fac / "devkit" / "README.md"
        text = readme.read_text()
        self.assertIn("<!-- devkit:modules:begin -->", text)
        readme.write_text(text.replace("| audit |", "| audit-HANDEDIT |", 1))
        py = f.fac / "devkit/install/_devkit.py"
        bad = subprocess.run([sys.executable, str(py), "readme", "--check"], capture_output=True, text=True, env=ENV)
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        fix = subprocess.run([sys.executable, str(py), "readme"], capture_output=True, text=True, env=ENV)
        self.assertEqual(fix.returncode, 0, fix.stderr)
        self.assertNotIn("HANDEDIT", readme.read_text())
        ok = subprocess.run([sys.executable, str(py), "readme", "--check"], capture_output=True, text=True, env=ENV)
        self.assertEqual(ok.returncode, 0)


class VocabularySingleSource(unittest.TestCase):
    def test_domains_json_equals_schema(self):
        # An optional schema file input; the test skips when it
        # is absent.
        schema = REPO / "harness-registry" / "_schema.yaml"
        if not schema.is_file():
            self.skipTest("optional input absent: no harness-registry")
        import yaml
        doc = yaml.safe_load(schema.read_text())
        data = json.loads((REPO / "devkit" / "domains.json").read_text())
        want = list(doc["domains"]) + list(doc["module_only_domains"])
        self.assertEqual(data["domains"], want)
        self.assertEqual(data["mechanisms"], list(doc["mechanisms"]))
        self.assertIn("template", data["domains"])
        self.assertNotIn("template", doc["domains"], "template is module-only, never a registry entry")
        self.assertNotIn("template", doc["field_definitions"]["domain"]["values"])

    def test_generator_check_passes(self):
        schema = REPO / "harness-registry" / "_schema.yaml"
        if not schema.is_file():
            self.skipTest("optional input absent: no harness-registry")
        r = subprocess.run([sys.executable, str(REPO / "devkit/install/_devkit.py"), "domains", "--check"],
                           capture_output=True, text=True, env=ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class SevenModulesMigrated(unittest.TestCase):
    EXPECT = {"audit": "all", "claude-md-kernel": "all", "git-multi-session": "git-multi-session",
              "observe": "observe", "secrets-privacy": "secrets-privacy", "system-safety": "system-safety",
              "work-craft": "work-craft", "skill-template": "template",
              "decision-gating-business": "decision-gating-business",
              "decision-gating-technical": "decision-gating-technical", "harness-governance": "all"}

    def test_all_install_with_expected_domains(self):
        f = Factory(self)
        t = f.target()
        r = f.install(t)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        m = {i["module"]: i for i in manifest(t)["installs"]}
        in_tree = {p.name for p in (REPO / "devkit/modules").iterdir() if p.is_dir()}
        self.assertEqual(set(m), in_tree, "installing everything installs every module in this tree")
        present = {k: v for k, v in self.EXPECT.items() if k in in_tree}
        self.assertEqual({k: v["domain"] for k, v in m.items() if k in self.EXPECT}, present)
        for i in m.values():
            for fr in i["files"]:
                self.assertIn(fr["mechanism"], ("BLOCK", "ASK", "DETECT", "INSTRUCT", "ROUTE", "VERIFY"))
        self.assertEqual(f.run("check", t).returncode, 0)


def frag_factory(tc, fa_deny, fa_ask=()):
    f = Factory(tc)
    f.add_module("fa", module_md("fa", "system-safety", [("s.json", ".claude/settings.json", "merge", "BLOCK")]),
                 {"s.json": settings(fa_deny, fa_ask)})
    return f


def user_target(f, deny=("Mine",), allow=("MyAllow",)):
    t = f.target()
    (t / ".claude").mkdir(exist_ok=True)
    (t / ".claude/settings.json").write_text(settings(deny=deny, allow=allow))
    return t


def perms(t):
    return json.loads((Path(t) / ".claude/settings.json").read_text())["permissions"]


class RepairReconcileAndRemove(unittest.TestCase):
    def test_f1_changed_fragment_reconciles_on_reinstall(self):
        f = frag_factory(self, ["P", "Q"])
        t = user_target(f)
        self.assertEqual(f.install(t, "fa").returncode, 0)
        (f.fac / "devkit/modules/fa/payload/s.json").write_text(settings(["P", "R"]))
        self.assertEqual(f.install(t, "fa").returncode, 0)
        self.assertNotIn("Q", perms(t)["deny"], "a pattern the module no longer ships is un-merged")
        self.assertEqual(manifest(t)["installs"][0]["merged"][".claude/settings.json"]["deny"], ["P", "R"])
        self.assertEqual(f.run("remove-module", t, "fa").returncode, 0)
        self.assertEqual(perms(t)["deny"], ["Mine"])
        self.assertEqual(perms(t)["allow"], ["MyAllow"])

    def test_f1_dropped_pattern_kept_when_another_module_owns_it(self):
        f = two_fragment_factory(self)          # frag-a and frag-b both ship Shared(x)
        t = f.target()
        self.assertEqual(f.install(t, "frag-a", "frag-b").returncode, 0)
        (f.fac / "devkit/modules/frag-a/payload/s.json").write_text(settings(["Bash(rm -rf:*)"], ["Bash(sudo:*)"]))
        self.assertEqual(f.install(t, "frag-a").returncode, 0)
        self.assertIn("Shared(x)", perms(t)["deny"])

    def test_f1_two_merge_entries_on_one_dest_in_one_module_refused(self):
        f = Factory(self)
        f.add_module("fa", module_md("fa", "system-safety", [("s1.json", ".claude/settings.json", "merge", "BLOCK"),
                                                             ("s2.json", ".claude/settings.json", "merge", "ASK")]),
                     {"s1.json": settings(["P1"]), "s2.json": settings(["P2"])})
        t = f.root / "never"
        r = f.install(t, "fa")
        self.assertEqual(r.returncode, 8)
        self.assertIn("already claimed by fa", r.stderr)
        self.assertFalse(t.exists())

    def test_f1_dropped_merge_entry_unmerges_like_remove(self):
        f = two_fragment_factory(self)
        t = user_target(f)
        self.assertEqual(f.install(t, "frag-a", "frag-b").returncode, 0)
        (f.fac / "devkit/modules/frag-a/MODULE.md").write_text(
            module_md("frag-a", "system-safety", [("r.md", ".claude/rules/r.md", "overwrite", "INSTRUCT")]))
        (f.fac / "devkit/modules/frag-a/payload/r.md").write_text("r\n")
        r = f.install(t, "frag-a")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        d = perms(t)["deny"]
        self.assertNotIn("Bash(rm -rf:*)", d)
        self.assertIn("Shared(x)", d)
        self.assertIn("Read(./.env)", d)
        self.assertIn("Mine", d)
        m = {i["module"]: i for i in manifest(t)["installs"]}
        self.assertNotIn("merged", m["frag-a"])
        self.assertEqual(f.run("check", t).returncode, 0)

    def test_f2_pre_v2_record_leaves_present_fragment_patterns_unclaimed(self):
        f = frag_factory(self, ["P", "Q"], ["K"])
        t = user_target(f)
        self.assertEqual(f.install(t, "fa").returncode, 0)
        m = manifest(t)                      # simulate a record written by the 1.x installer
        for i in m["installs"]:
            for k in ("merged", "domain", "sections"):
                i.pop(k, None)
        (t / ".devkit-manifest.json").write_text(json.dumps(m))
        r = f.install(t, "fa")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        rec = manifest(t)["installs"][0]
        # patterns already in the file with no recorded owner may be the user's: unclaimed, never owned
        self.assertEqual(rec["merged"][".claude/settings.json"], {"deny": [], "ask": []})
        self.assertEqual(rec["legacy_unclaimed"][".claude/settings.json"], {"deny": ["P", "Q"], "ask": ["K"]})
        rr = f.run("remove-module", t, "fa")
        self.assertEqual(rr.returncode, 0)
        self.assertIn("predate ownership tracking", rr.stdout)
        self.assertEqual(perms(t)["deny"], ["Mine", "P", "Q"])
        self.assertEqual(perms(t)["ask"], ["K"])

    def test_f2_settings_baseline_state_wording(self):
        text = (REPO / "devkit/modules/system-safety/MODULE.md").read_text()
        self.assertIn("cannot be told from your own", text)

    def test_f3_fenced_heading_does_not_end_a_section(self):
        f = Factory(self)
        md = module_md("fenced", "work-craft", [("r.md", ".claude/r.md", "overwrite", "INSTRUCT")], sections=False) + (
            "\n## Prevents\n\nx\n\n## Procedure\n\n```bash\n# a comment\n## not a heading\nrun it\n```\n"
            "~~~\n# tilde comment\n~~~\nafter the fences\n\n## State\n\nz\n")
        f.add_module("fenced", md, {"r.md": "x\n"})
        t = f.target()
        self.assertEqual(f.install(t, "fenced").returncode, 0)
        proc = {i["module"]: i for i in manifest(t)["installs"]}["fenced"]["sections"]["procedure"]
        for needle in ("# a comment", "## not a heading", "# tilde comment", "after the fences"):
            self.assertIn(needle, proc)

    def test_f5_force_keeps_unparseable_file_and_drops_record(self):
        f = frag_factory(self, ["P"])
        t = user_target(f)
        self.assertEqual(f.install(t, "fa").returncode, 0)
        (t / ".claude/settings.json").write_text("{ not json")
        r = f.run("remove-module", t, "fa")
        self.assertEqual(r.returncode, 3)
        self.assertEqual((t / ".claude/settings.json").read_text(), "{ not json")
        self.assertTrue(any(i["module"] == "fa" for i in manifest(t)["installs"]))
        r = f.run("remove-module", t, "fa", "--force")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("merge-file-kept", r.stdout)
        self.assertEqual((t / ".claude/settings.json").read_text(), "{ not json")
        self.assertFalse(any(i["module"] == "fa" for i in manifest(t)["installs"]))


class MergeFileDrift(unittest.TestCase):
    """A `mode: merge` file (settings.json) is shared with the repo owner: adding hooks or `allow` entries is
    not drift; losing a pattern a module contributed, or breaking the JSON, is."""

    def _installed(self):
        f = two_fragment_factory(self)
        t = f.target()
        self.assertEqual(f.install(t, "frag-a", "frag-b").returncode, 0)
        return f, t

    def _edit(self, t, fn):
        p = t / ".claude/settings.json"
        doc = json.loads(p.read_text())
        fn(doc)
        p.write_text(json.dumps(doc, indent=2))

    def test_wiring_hooks_and_adding_allow_entries_is_not_drift(self):
        f, t = self._installed()
        self._edit(t, lambda d: (d.setdefault("hooks", {}).update(
            {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "bash x.sh"}]}]}),
            d["permissions"]["allow"].append("Bash(ls:*)"), d["permissions"]["deny"].append("Mine(deny)")))
        r = f.run("--check", t)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("modified-downstream", r.stdout)

    def test_removing_a_contributed_pattern_is_drift(self):
        f, t = self._installed()
        self._edit(t, lambda d: d["permissions"]["deny"].remove("Read(./.env)"))
        r = f.run("--check", t)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("modified-downstream", r.stdout)

    def test_removing_an_ask_pattern_is_drift(self):
        f, t = self._installed()
        self._edit(t, lambda d: d["permissions"]["ask"].remove("Bash(sudo:*)"))
        self.assertEqual(f.run("--check", t).returncode, 2)

    def test_a_permission_bypassing_default_mode_is_drift_with_a_reason_line(self):
        f, t = self._installed()
        self._edit(t, lambda d: d["permissions"].update(defaultMode="bypassPermissions"))
        r = f.run("--check", t)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        rows = [l for l in r.stdout.splitlines() if "settings.json" in l]
        self.assertTrue(rows and all("modified-downstream" in l for l in rows), r.stdout)
        self.assertTrue(all("defaultMode" in l and "bypassPermissions" in l for l in rows), r.stdout)
        # the audit script a target carries (run standalone, as an installed copy runs) says the same
        r2 = subprocess.run([sys.executable, str(REPO / "devkit/modules/audit/payload/devkit-audit.py"), str(t)],
                            capture_output=True, text=True, env=ENV)
        self.assertEqual(r2.returncode, 2, r2.stdout)
        self.assertIn("bypassPermissions", r2.stdout)

    def test_other_default_modes_and_wired_hooks_stay_clean(self):
        f, t = self._installed()
        self._edit(t, lambda d: (d["permissions"].update(defaultMode="acceptEdits"),
                                 d.setdefault("hooks", {}).update(
                                     {"PreToolUse": [{"matcher": "Bash", "hooks": [
                                         {"type": "command", "command": "bash x.sh"}]}]})))
        r = f.run("--check", t)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_turning_an_ask_pattern_into_an_allow_is_still_drift(self):
        f, t = self._installed()
        def swap(d):
            d["permissions"]["ask"].remove("Bash(sudo:*)")
            d["permissions"]["allow"].append("Bash(sudo:*)")
        self._edit(t, swap)
        self.assertEqual(f.run("--check", t).returncode, 2)

    def test_broken_json_is_drift(self):
        f, t = self._installed()
        (t / ".claude/settings.json").write_text("{ not json")
        r = f.run("--check", t)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("modified-downstream", r.stdout)

    def test_a_record_without_merged_patterns_falls_back_to_the_sha(self):
        f, t = self._installed()
        m = manifest(t)
        for inst in m["installs"]:
            inst.pop("merged", None)
        (t / ".devkit-manifest.json").write_text(json.dumps(m))
        self.assertEqual(f.run("--check", t).returncode, 0)
        self._edit(t, lambda d: d.setdefault("hooks", {}))
        self.assertEqual(f.run("--check", t).returncode, 2)


class RepairBoard(unittest.TestCase):
    def _board(self, f, t):
        r = subprocess.run([sys.executable, str(t / ".claude/devkit-audit.py"), "board", str(t)],
                           capture_output=True, text=True, env=ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_f6_control_chars_are_neutralized_and_lines_kept(self):
        f = Factory(self)
        md = module_md("evil", "work-craft", [("hooks/guard.sh", ".claude/hooks/guard.sh", "overwrite", "DETECT")])
        md = md.replace("Run the thing, read the result.", "line one\n\x1b[31mred\x07 bell\nline three")
        f.add_module("evil", md, {"hooks/guard.sh": "#!/bin/sh\nexit 0\n"})
        t = f.target()
        self.assertEqual(f.install(t, "audit", "evil").returncode, 0)
        (t / ".claude/settings.json").write_text(json.dumps({"hooks": {
            "Pre\x1bTool": [{"matcher": "Ba\x1b]0;x\x07sh",
                             "hooks": [{"type": "command", "command": "bash .claude/hooks/my-guard.sh"}]}]}}))
        out = self._board(f, t)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("  line one\n", out)
        self.assertIn("  line three", out)
        self.assertIn("not wired", out, "guard.sh must not match my-guard.sh by substring")

    def test_f6_hostile_event_names_do_not_inject(self):
        f = Factory(self)
        f.add_module("hk", module_md("hk", "work-craft", [("hooks/guard.sh", ".claude/hooks/guard.sh", "overwrite", "DETECT")]),
                     {"hooks/guard.sh": "#!/bin/sh\n"})
        t = f.target()
        self.assertEqual(f.install(t, "audit", "hk").returncode, 0)
        (t / ".claude/settings.json").write_text(json.dumps({"hooks": {
            "Pre\x1b[2JTool": [{"matcher": "M\x1b[2J", "hooks": [{"type": "command", "command": "bash .claude/hooks/guard.sh"}]}]}}))
        out = self._board(f, t)
        self.assertNotIn("\x1b", out)
        self.assertIn("wired: Pre", out)


if __name__ == "__main__":
    unittest.main()
