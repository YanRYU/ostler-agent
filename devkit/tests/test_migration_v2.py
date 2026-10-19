"""Tests for the 2.0 regrouping: the original 1.x modules regrouped by domain, with 1.x name aliases and a
one-transaction migration of an installed 1.x manifest.

Run from the repo root:
    bash devkit/tests/test-migration-v2.sh

What is checked: install-comparison, alias-all-verbs, migrate-existing.
The 1.x side of the comparison is the devkit tree as it was just before the 2.0 contract change
(found in git history), materialized with
`git archive` inside the test (nothing is committed). Without the 1.x commit in
history (for example a shallow copy) those tests skip.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def base_1x():
    """The commit just before the 2.0 contract change: the parent of the commit that first added the
    contract test. Needs git history."""
    r = subprocess.run(["git", "-C", str(REPO), "log", "--diff-filter=A", "--format=%H", "--",
                        "devkit/tests/test_contract_v2.py"], capture_output=True, text=True)
    first = r.stdout.split()[-1] if r.returncode == 0 and r.stdout.split() else None
    if first is None:
        raise AssertionError("cannot find the commit that introduced the 2.0 contract: %s" % r.stderr)
    return first + "^"

ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

OLD_MODULES = ["audit", "claude-md-kernel", "hooks-core", "rules-core", "settings-baseline", "skill-template"]
NEW_MODULES = ["audit", "claude-md-kernel", "git-multi-session", "observe", "secrets-privacy",
               "skill-template", "system-safety", "work-craft"]
ALIASES = {"settings-baseline": ["system-safety", "secrets-privacy"],
           "hooks-core": ["git-multi-session", "secrets-privacy", "observe"],
           "rules-core": ["git-multi-session", "secrets-privacy", "work-craft"]}

# Where each 1.x payload file lives in 2.0 (module, relative payload path); bytes must be identical.
MOVED = {
    ("hooks-core", "hooks/stash-leak-check.sh"): "git-multi-session",
    ("hooks-core", "hooks/branch-similarity-check.sh"): "git-multi-session",
    ("hooks-core", "hooks/personal-path-guard.py"): "secrets-privacy",
    ("hooks-core", "hooks/plan-exit-print-location.sh"): "observe",
    ("rules-core", "rules/git-hygiene.md"): "git-multi-session",
    ("rules-core", "rules/privacy.md"): "secrets-privacy",
    ("rules-core", "rules/plan-writing.md"): "work-craft",
    ("rules-core", "rules/self-contained-agents.md"): "work-craft",
    ("claude-md-kernel", "CLAUDE.md"): "claude-md-kernel",
    ("skill-template", "SKILL.md"): "skill-template",
}

# The enumerated, intentional differences between a 1.x and a 2.0 install into a clean target.
# Nothing else may differ.
TARGET_DELTAS = {
    ".devkit-manifest.json": "manifest records: 2.0 module names, domain, per-file mechanism, body sections, kit_version",
    ".claude/devkit-audit.py": "the audit script gained the `board` command and sanitizing with the module contract v2 change (not a regrouping change)",
    ".claude/rules/git-hygiene.md": "the rule's header names its 2.0 module instead of the retired hooks-core (a follow-up wording fix)",
    "CLAUDE.md": "two hard-rule and pointer bullets became optional placeholders (a follow-up wording fix)",
    ".claude/skills/EXAMPLE-SKILL/SKILL.md": "the template says its method-only convention is the template's own (a follow-up wording fix)",
}

# Moved files whose bytes changed on purpose. The left side of each pair is the retired 1.x wording, kept
# verbatim so the byte comparison can reproduce it; it is not current guidance.
MOVED_EDITS = {("rules-core", "rules/git-hygiene.md"): [("`hooks-core` hooks", "git-multi-session hooks"),
                                                       ("**Always-on. Applies in every repo. Surfaced by the",
                                                        "**Always-on in the repo where this module is installed. Surfaced by the")],
               ("claude-md-kernel", "CLAUDE.md"): [
                   ("- Ask before sudo, curl/wget, git push, or publishing packages — enforced by `settings.json` ask gates",
                    "- {Optional: commands that need your confirmation first, e.g. sudo or network downloads; else delete "
                    "this bullet} — enforced by the `ask` list in `settings.json`, if you configure one"),
                   ("- Agents ship self-contained; copy-on-build → `.claude/rules/self-contained-agents.md`",
                    "- {Optional: if this repo builds other agents, how they are packaged (e.g. copied in rather than "
                    "referenced at runtime); else delete this bullet} → `.claude/rules/self-contained-agents.md`"),
                   ("- Plan format + complexity-scaled gates → `.claude/rules/plan-writing.md`",
                    "- {Optional: how plans are written, if you install the plan-writing rule; else delete this bullet} → "
                    "`.claude/rules/plan-writing.md`"),
                   ("its rules, so each rule is written down once (see `.claude/rules/self-contained-agents.md`).",
                    "its rules, so each rule is written down once and this file only points to it.")],
               ("skill-template", "SKILL.md"): [
                   ("{One paragraph: the capability this skill provides — knowledge/method, not execution.}",
                    "{One paragraph: the capability this skill provides (under this template's convention: "
                    "knowledge and method, with execution in scripts it calls; adapt to your repo).}"),
                   ("{anti-pattern — e.g. do not put orchestration logic here; a skill describes a method and does not run anything}",
                    "{anti-pattern — e.g. under this template's convention, do not put orchestration logic here; "
                    "keep it in scripts or tools the skill calls}")]}

# Files a 2.x install adds that the 1.x kit never had (work-craft 2.1.0 added two rules).
# They are additions, not rewrites: the byte-identity checks leave them out and the update test
# asserts they land.
ADDED_FILES = {
    ".claude/rules/output-quality-standards.md": "work-craft 2.1.0",
    ".claude/rules/communication-style.md": "work-craft 2.1.0",
}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def tree(root):
    root = Path(root)
    skip = {".devkit-install.lock"}
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p.name not in skip and ".devkit-txn" not in p.parts}


def run(factory, *args, env=None):
    e = dict(ENV, **(env or {}))
    return subprocess.run(["bash", str(Path(factory) / "devkit/install/devkit-install.sh"), *map(str, args)],
                          capture_output=True, text=True, env=e)


def manifest(t):
    return json.loads((Path(t) / ".devkit-manifest.json").read_text())


def names(t):
    return [i["module"] for i in manifest(t)["installs"]]


def deprecations(r):
    return [ln for ln in (r.stdout + r.stderr).splitlines() if "DEPRECATED" in ln]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="devkit-migration-"))
        cls.new = cls.tmp / "new"
        for d in ("devkit", "install"):
            shutil.copytree(REPO / d, cls.new / d, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def target(self, name=None, user_settings=True):
        type(self).n += 1
        t = self.tmp / (name or "t%d" % self.n)
        (t / ".claude").mkdir(parents=True)
        if user_settings:
            (t / ".claude/settings.json").write_text(json.dumps({"permissions": {"allow": ["Mine(allow)"]}}))
        return t

    def install_new(self, t, *mods):
        r = run(self.new, "install", t, *mods, "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r


class Old1x(Base):
    """A factory copy of the 1.x devkit tree, for the tests that need a 1.x-installed target."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.old = cls.tmp / "old"
        cls.old.mkdir()
        try:
            BASE_1X = base_1x()
        except AssertionError:
            raise unittest.SkipTest("no git history: the 1.x kit commit is not reachable")
        reach = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", BASE_1X + "^{commit}"],
                               capture_output=True)
        if reach.returncode != 0:
            raise unittest.SkipTest("no git history: the 1.x kit commit %s is not reachable" % BASE_1X[:8])
        ar = subprocess.run(["git", "-C", str(REPO), "archive", BASE_1X, "devkit", "install"],
                            capture_output=True)
        if ar.returncode != 0:
            raise AssertionError("cannot materialize the 1.x kit at %s: %s" % (BASE_1X, ar.stderr.decode()))
        subprocess.run(["tar", "-x", "-C", str(cls.old)], input=ar.stdout, check=True)

    def install_old(self, t, *mods):
        r = run(self.old, "install", t, *(mods or OLD_MODULES), "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class InstallComparison(Old1x):
    def test_every_1x_payload_file_moved_with_identical_bytes(self):
        for (mod, rel), new_mod in MOVED.items():
            old = self.old / "devkit/modules" / mod / "payload" / rel
            new = self.new / "devkit/modules" / new_mod / "payload" / rel
            self.assertTrue(new.is_file(), "%s not found in %s" % (rel, new_mod))
            if (mod, rel) in MOVED_EDITS:
                text = old.read_text()
                for a, b in MOVED_EDITS[(mod, rel)]:
                    text = text.replace(a, b)
                self.assertEqual(text, new.read_text(), "%s changed beyond its edit" % rel)
                continue
            self.assertEqual(sha(old), sha(new), "%s changed while moving" % rel)
            self.assertEqual(old.stat().st_mode & 0o111, new.stat().st_mode & 0o111, "%s exec bit" % rel)

    def test_each_1x_permission_pattern_lands_in_exactly_one_fragment(self):
        old = json.loads((self.old / "devkit/modules/settings-baseline/payload/settings-baseline.partial.json")
                         .read_text())["permissions"]
        frags = []
        for m in ("system-safety", "secrets-privacy"):
            f = next((self.new / "devkit/modules" / m / "payload").glob("*.partial.json"))
            frags.append(json.loads(f.read_text())["permissions"])
        for kind in ("deny", "ask"):
            got = [p for f in frags for p in f.get(kind, [])]
            self.assertEqual(sorted(got), sorted(old[kind]), kind)
            self.assertEqual(len(got), len(set(got)), "%s: a pattern is in two fragments" % kind)

    def test_install_comparison_all_modules_into_clean_targets(self):
        t_old, t_new = self.target("cmp-old"), self.target("cmp-new")
        self.install_old(t_old)
        self.install_new(t_new, *NEW_MODULES)
        a, b = tree(t_old), tree(t_new)
        b = {p: h for p, h in b.items() if p not in ADDED_FILES}
        self.assertEqual(sorted(a), sorted(b), "the set of installed destinations differs")
        # settings.json is compared as permission sets below (its list order may legitimately differ)
        differing = {p for p in a if a[p] != b[p]} - {".claude/settings.json"}
        self.assertEqual(differing, set(TARGET_DELTAS), "differences beyond the enumerated list: %s"
                         % sorted(differing - set(TARGET_DELTAS)))
        sa = json.loads((t_old / ".claude/settings.json").read_text())["permissions"]
        sb = json.loads((t_new / ".claude/settings.json").read_text())["permissions"]
        self.assertEqual(sorted(sa["deny"]), sorted(sb["deny"]))
        self.assertEqual(sorted(sa["ask"]), sorted(sb["ask"]))
        self.assertEqual(sb["allow"], ["Mine(allow)"])
        self.assertEqual(sa["allow"], ["Mine(allow)"])
        self.assertEqual({k: v for k, v in sa.items() if k not in ("deny", "ask")},
                         {k: v for k, v in sb.items() if k not in ("deny", "ask")})
        old_md = (t_old / "CLAUDE.md").read_text()
        for a, b in MOVED_EDITS[("claude-md-kernel", "CLAUDE.md")]:
            old_md = old_md.replace(a, b)
        self.assertEqual(old_md, (t_new / "CLAUDE.md").read_text())
        self.assertEqual(sorted(names(t_new)), NEW_MODULES)
        self.assertEqual(manifest(t_new)["kit_version"], "2.0.0")
        chk = run(self.new, "check", t_new)
        self.assertEqual(chk.returncode, 0, chk.stdout + chk.stderr)

    def test_factory_side_deltas_are_only_module_docs_and_the_split_fragments(self):
        # Modules outside the comparison (added or kept apart from the regrouping) are ignored; the 1.x
        # side must still hold every 1.x module and the 2.0 side every regrouped one.
        mods_old = {p.name for p in (self.old / "devkit/modules").iterdir() if p.is_dir()}
        self.assertLessEqual(set(OLD_MODULES), mods_old)
        mods_new = {p.name for p in (self.new / "devkit/modules").iterdir() if p.is_dir()}
        self.assertLessEqual(set(NEW_MODULES), mods_new)
        for m in ("system-safety", "secrets-privacy", "git-multi-session", "work-craft", "observe"):
            md = (self.new / "devkit/modules" / m / "MODULE.md").read_text()
            self.assertIn("domain: %s\n" % m, md)
            for sec in ("## Prevents", "## Procedure", "## State"):
                self.assertIn(sec, md)
            self.assertNotIn("layer:", md)


class AliasAllVerbs(Base):
    def test_install_with_a_1x_name(self):
        t = self.target()
        r = run(self.new, "install", t, "rules-core", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(deprecations(r)), 1, r.stdout + r.stderr)
        self.assertIn("rules-core", deprecations(r)[0])
        self.assertIn("2.1.0", deprecations(r)[0], "the removal version is part of the line")
        self.assertEqual(sorted(names(t)), sorted(ALIASES["rules-core"]))

    def test_install_two_aliases_one_line_each_and_dedupe(self):
        t = self.target()
        r = run(self.new, "install", t, "hooks-core", "rules-core", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(deprecations(r)), 2)
        self.assertEqual(sorted(names(t)), sorted(set(ALIASES["hooks-core"]) | set(ALIASES["rules-core"])))

    def test_install_settings_baseline_alias(self):
        t = self.target()
        r = run(self.new, "install", t, "settings-baseline", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(deprecations(r)), 1)
        self.assertEqual(sorted(names(t)), sorted(ALIASES["settings-baseline"]))

    def test_new_names_print_no_deprecation(self):
        t = self.target()
        r = self.install_new(t, "work-craft")
        self.assertEqual(deprecations(r), [])

    def test_update_verb_with_a_1x_name(self):
        t = self.target()
        self.install_new(t, *NEW_MODULES)
        r = run(self.new, "update", t, "rules-core", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(deprecations(r)), 1)

    def test_outdated_with_a_1x_name(self):
        t = self.target()
        self.install_new(t, *NEW_MODULES)
        shared = self.tmp / "outdated-factory"
        shutil.copytree(self.new, shared)
        rule = shared / "devkit/modules/work-craft/payload/rules/plan-writing.md"
        rule.write_text(rule.read_text() + "\nA new line.\n")
        r = run(shared, "outdated", t, "rules-core")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(deprecations(r)), 1, r.stdout + r.stderr)
        lines = [ln for ln in r.stdout.splitlines() if "outdated" in ln and "DEPRECATED" not in ln]
        self.assertEqual(len(lines), 1)
        self.assertIn("work-craft", lines[0])
        # the same question without a filter and without aliases prints no deprecation line
        r2 = run(shared, "outdated", t)
        self.assertEqual(deprecations(r2), [])
        self.assertIn("work-craft", r2.stdout)

    def test_outdated_with_an_unknown_name_is_refused(self):
        t = self.target()
        self.install_new(t, "work-craft")
        r = run(self.new, "outdated", t, "no-such-module")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("unknown module", r.stderr)

    def test_remove_module_with_a_1x_name(self):
        t = self.target()
        self.install_new(t, *NEW_MODULES)
        r = run(self.new, "remove-module", t, "rules-core")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(deprecations(r)), 1)
        left = names(t)
        for gone in ALIASES["rules-core"]:
            self.assertNotIn(gone, left)
        self.assertIn("system-safety", left)
        self.assertEqual(run(self.new, "check", t).returncode, 0)

    def test_remove_module_alias_skips_mapped_modules_that_are_not_installed(self):
        t = self.target()
        self.install_new(t, "work-craft")
        r = run(self.new, "remove-module", t, "rules-core")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(names(t), [])

    def test_unknown_name_still_refused(self):
        t = self.target()
        r = run(self.new, "install", t, "no-such-module", "--allow-dirty")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown module", r.stderr)


class MigrateExisting(Old1x):
    def test_update_rewrites_1x_records_to_2_0_in_one_transaction(self):
        t = self.target()
        self.install_old(t)
        before = tree(t)
        old_names = names(t)
        for n in ("hooks-core", "rules-core", "settings-baseline"):
            self.assertIn(n, old_names)
        r = run(self.new, "update", t, "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = tree(t)
        for added in ADDED_FILES:
            self.assertIn(added, after)
        after = {p: h for p, h in after.items() if p not in ADDED_FILES}
        m = manifest(t)
        self.assertEqual(sorted(i["module"] for i in m["installs"]), sorted(set(NEW_MODULES)))
        self.assertEqual(m["kit_version"], "2.0.0")
        for i in m["installs"]:
            self.assertTrue(i["domain"] and i["sections"]["prevents"])
        self.assertEqual({p: h for p, h in after.items() if p not in TARGET_DELTAS},
                         {p: h for p, h in before.items() if p not in TARGET_DELTAS},
                         "migration must not rewrite any installed file")
        chk = run(self.new, "check", t)
        self.assertEqual(chk.returncode, 0, chk.stdout + chk.stderr)
        # a 1.x record cannot say which patterns were the user's: removing system-safety deletes none of
        # them (strict), and names what it kept
        r = run(self.new, "remove-module", t, "system-safety")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("predate ownership tracking", r.stdout)
        perms = json.loads((t / ".claude/settings.json").read_text())["permissions"]
        self.assertIn("Bash(rm :*)", perms["deny"])
        self.assertIn("Read(./.env)", perms["deny"])
        self.assertEqual(perms["allow"], ["Mine(allow)"])
        self.assertEqual(run(self.new, "check", t).returncode, 0)

    def test_partial_update_keeps_the_rest_of_the_1x_records_working(self):
        t = self.target()
        self.install_old(t)
        r = run(self.new, "install", t, "rules-core", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        left = names(t)
        self.assertNotIn("rules-core", left)
        self.assertIn("hooks-core", left)
        self.assertIn("settings-baseline", left)
        self.assertEqual(run(self.new, "check", t).returncode, 0)
        # finish the migration
        r = run(self.new, "update", t, "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(names(t)), sorted(NEW_MODULES))

    def test_a_target_that_never_updates_keeps_working(self):
        t = self.target()
        self.install_old(t)
        r = run(self.new, "check", t)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run(self.new, "outdated", t)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("update", r.stdout, "outdated points a 1.x record at the update command")

    def test_injected_failure_leaves_the_1x_manifest_untouched(self):
        for point in ("manifest_temp", "journal_written", "after_op:0"):
            for mode in ("kill", "raise"):
                with self.subTest(point=point, mode=mode):
                    t = self.target()
                    self.install_old(t)
                    mbytes = (t / ".devkit-manifest.json").read_bytes()
                    before = tree(t)
                    env = {"DEVKIT_INSTALL_FAULT": point}
                    if mode == "raise":
                        env["DEVKIT_INSTALL_FAULT_MODE"] = "raise"
                    r = run(self.new, "update", t, "--allow-dirty", env=env)
                    if r.returncode == 0:
                        if point == "manifest_temp":
                            self.fail("the manifest write must always pass through manifest_temp")
                        continue                      # this plan has no such point (no file ops)
                    rec = run(self.new, "recover", t)
                    self.assertEqual(rec.returncode, 0, rec.stdout + rec.stderr)
                    self.assertEqual((t / ".devkit-manifest.json").read_bytes(), mbytes)
                    self.assertEqual(tree(t), before, "no partial files may remain")
                    ok = run(self.new, "update", t, "--allow-dirty")
                    self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
                    self.assertEqual(sorted(names(t)), sorted(NEW_MODULES))

    # ---- a retained 1.x merge record keeps providing its patterns -------------------------
    def _counts(self, t):
        perms = json.loads((t / ".claude/settings.json").read_text())["permissions"]
        return len(perms.get("deny", [])), len(perms.get("ask", [])), perms["allow"]

    def test_f1_partial_migration_then_remove_keeps_every_protection(self):
        for step in ("system-safety", "secrets-privacy", "rules-core"):
            with self.subTest(step=step):
                t = self.target()
                self.install_old(t)
                before = self._counts(t)
                self.assertEqual(before[:2], (27, 5))
                r = run(self.new, "install", t, step, "--allow-dirty")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self._counts(t), before, "installing a successor must not change the file")
                r = run(self.new, "remove-module", t, step)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self._counts(t), before, "removing a successor stripped protections the 1.x record still provides")
                self.assertIn("settings-baseline", names(t), "the 1.x record is still installed")
                chk = run(self.new, "check", t)
                self.assertEqual(chk.returncode, 0, chk.stdout + chk.stderr)

    def test_f1_both_successors_one_at_a_time_then_remove_each(self):
        t = self.target()
        self.install_old(t)
        before = self._counts(t)
        for m in ("system-safety", "secrets-privacy"):
            self.assertEqual(run(self.new, "install", t, m, "--allow-dirty").returncode, 0)
        self.assertNotIn("settings-baseline", names(t), "both successors installed: the 1.x record is retired")
        self.assertEqual(self._counts(t), before)
        self.assertEqual(run(self.new, "remove-module", t, "system-safety").returncode, 0)
        self.assertEqual(self._counts(t), before, "1.x-era patterns may be the user's: none is removed")
        self.assertEqual(run(self.new, "remove-module", t, "secrets-privacy").returncode, 0)
        self.assertEqual(self._counts(t), before)

    def test_f1_full_update_then_remove_both_fragments_keeps_every_legacy_pattern(self):
        t = self.target()
        self.install_old(t)
        self.assertEqual(run(self.new, "update", t, "--allow-dirty").returncode, 0)
        before = self._counts(t)
        for m in ("system-safety", "secrets-privacy"):
            self.assertEqual(run(self.new, "remove-module", t, m).returncode, 0)
        self.assertEqual(self._counts(t), before, "every pattern predates ownership tracking: all kept")

    # ---- legacy attribution: a pattern that may be the user's is never deleted ------------
    def _legacy_target(self):
        t = self.target()
        self.install_old(t)
        sp = t / ".claude/settings.json"
        d = json.loads(sp.read_text())
        d["permissions"]["deny"].remove("Bash(rm :*)")      # a pattern only the 2.0 module will add
        sp.write_text(json.dumps(d, indent=2) + "\n")
        return t

    def test_legacy_coinciding_pattern_survives_remove_module_and_is_named(self):
        t = self._legacy_target()
        r = run(self.new, "update", t, "--allow-dirty")      # full migration retires the 1.x record
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("predate ownership tracking", r.stdout)
        rec = next(i for i in manifest(t)["installs"] if i["module"] == "system-safety")
        self.assertIn("Bash(rm :*)", rec["merged"][".claude/settings.json"]["deny"])
        unc = rec["legacy_unclaimed"][".claude/settings.json"]
        self.assertTrue(unc["deny"])
        self.assertNotIn("Bash(rm :*)", unc["deny"])
        for k, v in unc.items():
            self.assertFalse(set(v) & set(rec["merged"][".claude/settings.json"].get(k, [])))
        r = run(self.new, "remove-module", t, "system-safety")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        perms = json.loads((t / ".claude/settings.json").read_text())["permissions"]
        self.assertNotIn("Bash(rm :*)", perms["deny"], "the module-only pattern is removed")
        for p in unc["deny"]:
            self.assertIn(p, perms["deny"], "an ambiguous pattern must survive remove-module")
        kept = [ln for ln in r.stdout.splitlines() if ln.startswith("  kept   ")]
        self.assertEqual(len(kept), 1, r.stdout)
        self.assertIn("predate ownership tracking (they may be yours; delete by hand if unwanted): ", kept[0])
        self.assertIn(unc["deny"][0], kept[0])

    def test_legacy_unclaimed_survives_reinstall_and_a_fresh_2_0_install_has_none(self):
        t = self._legacy_target()
        self.assertEqual(run(self.new, "update", t, "--allow-dirty").returncode, 0)
        first = next(i for i in manifest(t)["installs"] if i["module"] == "system-safety")["legacy_unclaimed"]
        r = run(self.new, "install", t, "system-safety", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        again = next(i for i in manifest(t)["installs"] if i["module"] == "system-safety")
        self.assertEqual(again["legacy_unclaimed"], first)
        self.assertNotIn("legacy_unclaimed", again["merged"])
        c = self.target()
        self.install_new(c, "system-safety")
        self.assertNotIn("legacy_unclaimed", next(i for i in manifest(c)["installs"]))

    def _deny(self, t):
        return json.loads((t / ".claude/settings.json").read_text())["permissions"]["deny"]

    def test_partial_migration_newly_added_pattern_is_removed_with_its_module(self):
        t = self._legacy_target()                  # "Bash(rm :*)" is absent: a 2.0 install adds it
        x = "Bash(rm :*)"
        self.assertNotIn(x, self._deny(t))
        r = run(self.new, "install", t, "system-safety", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(x, self._deny(t))
        self.assertIn("settings-baseline", names(t), "partial migration: the 1.x record is retained")
        recs = {i["module"]: i for i in manifest(t)["installs"]}
        self.assertIn(x, recs["system-safety"]["merged"][".claude/settings.json"]["deny"])
        self.assertNotIn(x, recs["settings-baseline"]["merged"][".claude/settings.json"]["deny"],
                         "the stand-in must not claim a pattern a 2.0 install newly added")
        shipped_before = [p for p in recs["system-safety"]["merged"][".claude/settings.json"]["deny"] if p != x]
        r = run(self.new, "remove-module", t, "system-safety")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        deny = self._deny(t)
        self.assertNotIn(x, deny, "a newly added pattern is removed with its module")
        # a pattern in the file before migration (shipped by 1.x and the successor) is kept
        kept = [p for p in json.loads((t / ".claude/settings.json").read_text())["permissions"]["deny"]]
        self.assertTrue(kept)
        for p in shipped_before:
            self.assertIn(p, deny)
        self.assertEqual(run(self.new, "check", t).returncode, 0)

    def test_retained_stand_in_drops_a_pattern_a_later_install_newly_added(self):
        t = self._legacy_target()
        gone, kept_p = "Bash(rm -r :*)", "Bash(rm -f :*)"
        self.assertEqual(run(self.new, "install", t, "system-safety", "--allow-dirty").returncode, 0)
        self.assertIn("settings-baseline", names(t), "partial migration: the 1.x record is retained")
        base = next(i for i in manifest(t)["installs"] if i["module"] == "settings-baseline")
        self.assertIn(gone, base["merged"][".claude/settings.json"]["deny"], "the stand-in claims it first")
        sp = t / ".claude/settings.json"
        d = json.loads(sp.read_text())
        d["permissions"]["deny"].remove(gone)           # hand-deleted before the re-install
        sp.write_text(json.dumps(d, indent=2) + "\n")
        r = run(self.new, "install", t, "system-safety", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(gone, self._deny(t))
        recs = {i["module"]: i for i in manifest(t)["installs"]}
        self.assertIn(gone, recs["system-safety"]["merged"][".claude/settings.json"]["deny"])
        self.assertNotIn(gone, recs["settings-baseline"]["merged"][".claude/settings.json"]["deny"],
                         "the stale stand-in must release a pattern a real record now owns")
        self.assertIn(kept_p, recs["settings-baseline"]["merged"][".claude/settings.json"]["deny"])
        r = run(self.new, "remove-module", t, "system-safety")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        deny = self._deny(t)
        self.assertNotIn(gone, deny, "a pattern a 2.0 install newly added is removed with its module")
        self.assertIn(kept_p, deny, "a pre-migration pattern the user still has stays")

    def test_partial_migration_reinstall_after_remove_leaves_no_unclaimed_pattern(self):
        t = self._legacy_target()
        x = "Bash(rm :*)"
        self.assertEqual(run(self.new, "install", t, "system-safety", "--allow-dirty").returncode, 0)
        self.assertEqual(run(self.new, "remove-module", t, "system-safety").returncode, 0)
        r = run(self.new, "install", t, "system-safety", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = next(i for i in manifest(t)["installs"] if i["module"] == "system-safety")
        self.assertIn(x, rec["merged"][".claude/settings.json"]["deny"])
        unc = rec.get("legacy_unclaimed", {}).get(".claude/settings.json", {})
        self.assertNotIn(x, unc.get("deny", []))
        self.assertEqual(run(self.new, "remove-module", t, "system-safety").returncode, 0)
        self.assertNotIn(x, self._deny(t))

    def test_retained_record_with_three_successors_does_not_claim_a_later_newly_added_pattern(self):
        t = self.target()
        pre, y = "Read(./.env)", "Write(./.env.*)"
        sp = t / ".claude/settings.json"
        sp.write_text(json.dumps({"permissions": {"allow": ["Mine(allow)"], "deny": [pre]}}, indent=2) + "\n")
        self.install_old(t, "hooks-core")
        mp = t / ".devkit-manifest.json"
        m = json.loads(mp.read_text())
        rec = next(i for i in m["installs"] if i["module"] == "hooks-core")
        rec["files"].append({"mode": "merge", "path": ".claude/settings.json", "sha256": sha(sp)})
        mp.write_text(json.dumps(m, indent=2) + "\n")
        self.assertEqual(run(self.new, "install", t, "git-multi-session", "--allow-dirty").returncode, 0)
        self.assertIn("hooks-core", names(t), "run 1: the 1.x record is retained")
        self.assertNotIn(y, self._deny(t))
        r = run(self.new, "install", t, "secrets-privacy", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(y, self._deny(t))
        recs = {i["module"]: i for i in manifest(t)["installs"]}
        self.assertIn(y, recs["secrets-privacy"]["merged"][".claude/settings.json"]["deny"])
        r = run(self.new, "remove-module", t, "secrets-privacy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        deny = self._deny(t)
        self.assertNotIn(y, deny, "a pattern a 2.0 install newly added is removed with its module")
        self.assertIn(pre, deny, "a pattern in the file before migration is kept")
        r = run(self.new, "install", t, "secrets-privacy", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = next(i for i in manifest(t)["installs"] if i["module"] == "secrets-privacy")
        self.assertNotIn(y, rec.get("legacy_unclaimed", {}).get(".claude/settings.json", {}).get("deny", []))

    def test_legacy_retained_1x_record_removal_deletes_nothing(self):
        t = self.target()
        self.install_old(t)
        before = self._counts(t)
        self.assertEqual(run(self.new, "install", t, "system-safety", "--allow-dirty").returncode, 0)
        self.assertIn("settings-baseline", names(t))
        r = run(self.new, "remove-module", t, "settings-baseline")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._counts(t), before, "a retained 1.x record's stand-in ownership never deletes")

    # ---- update with nothing installed installs nothing -----------------------------------
    def test_f2_update_with_nothing_installed_installs_nothing(self):
        t = self.target()
        self.install_new(t, "work-craft")
        self.assertEqual(run(self.new, "remove-module", t, "work-craft").returncode, 0)
        before = tree(t)
        r = run(self.new, "update", t, "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing is installed", r.stdout)
        self.assertEqual(names(t), [])
        self.assertEqual(tree(t), before, "update must not install the whole kit")

    # ---- the documented footprint of migrating a rules-core-only target -------------------
    def test_f3_update_of_a_rules_core_only_target_also_adds_the_secrets_footprint(self):
        t = self.target()
        r = run(self.old, "install", t, "rules-core", "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._counts(t)[:2], (0, 0))
        r = run(self.new, "update", t, "--allow-dirty")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(names(t)), sorted(ALIASES["rules-core"]))
        perms = json.loads((t / ".claude/settings.json").read_text())["permissions"]
        self.assertEqual(sorted(perms["deny"]), sorted(["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)",
                                                         "Write(./.env)", "Write(./.env.*)"]))
        for hook in ("stash-leak-check.sh", "branch-similarity-check.sh", "personal-path-guard.py"):
            self.assertTrue((t / ".claude/hooks" / hook).is_file(), hook)
        readme = (REPO / "devkit/README.md").read_text()
        for phrase in ("also installs the `secrets-privacy` settings fragment", "three hook scripts",
                       "`remove-module rules-core` removes those denies"):
            self.assertIn(phrase, readme, "devkit/README.md must document this footprint")
        r = run(self.new, "remove-module", t, "rules-core")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._counts(t)[:2], (0, 0))


if __name__ == "__main__":
    unittest.main()
