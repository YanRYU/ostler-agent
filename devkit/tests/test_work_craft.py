"""Tests for the two rules the work-craft module adds: output-quality-standards and
communication-style.

Run from the repo root:
    bash devkit/tests/test-work-craft.sh

Check, craft-owner-neutral: the lexicon gate has 0 hits on both rules,
every pointer in them resolves inside this tree, and the twelve numbered standards are
all present. These tests need two optional inputs (a lexicon file and a build script);
without them they skip.
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WC = REPO / "devkit/modules/work-craft"
QUALITY = WC / "payload/rules/output-quality-standards.md"
STYLE = WC / "payload/rules/communication-style.md"
RULES = [QUALITY, STYLE]

sys.path.insert(0, str(REPO / "publish"))
try:
    import build  # noqa: E402
except ImportError:  # pragma: no cover
    build = None


def have_lexicon():
    return build is not None and (REPO / "publish/lexicon.yaml").is_file()


def git_tracked_tree():
    """{path: (kind, exec, bytes)} of the working tree's tracked and new files, like read_archive."""
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"], capture_output=True, check=True).stdout
    entries = {}
    for raw in out.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode()
        f = REPO / rel
        if f.is_file() and not f.is_symlink():
            entries[rel] = ("file", f.stat().st_mode & 0o111 != 0, f.read_bytes())
    return entries


class OwnerNeutral(unittest.TestCase):
    @unittest.skipUnless(have_lexicon(), "optional input absent: no lexicon")
    def test_lexicon_gate_zero_hits(self):
        lex = build.parse_lexicon((REPO / "publish/lexicon.yaml").read_text())
        files = {str(p.relative_to(REPO)): (False, p.read_bytes())
                 for p in RULES + [WC / "MODULE.md", Path(__file__)]}
        build.scan_lexicon(lex, files)  # raises Refusal on any hit

    def test_rules_exist_and_are_declared_in_module(self):
        md = (WC / "MODULE.md").read_text()
        for rule in RULES:
            self.assertTrue(rule.is_file(), str(rule))
            self.assertIn("rules/" + rule.name, md)

    @unittest.skipUnless(have_lexicon() and (REPO / "publish/allowlist.yaml").is_file(),
                         "optional input absent: no build selection to resolve against")
    def test_pointers_resolve_inside_this_tree(self):
        # the build's own selection and its own dangling-pointer gate, on the working tree
        allow = build.parse_allowlist((REPO / "publish/allowlist.yaml").read_text())
        chosen, mapped = build.select(allow, git_tracked_tree())
        files = dict(chosen)
        files.update(mapped)
        for rule in RULES:
            rel = str(rule.relative_to(REPO))
            self.assertIn(rel, files, "work-craft rule is not in this tree: " + rel)
        try:
            build.check_references(files)
        except build.Refusal as e:
            mine = [l for l in str(e).splitlines() if any(r.name in l for r in RULES)]
            self.assertEqual(mine, [], "dangling pointer in a work-craft rule")
            raise

    @unittest.skipIf(build is None, "optional input absent: no build")
    def test_pointer_check_catches_a_dangling_pointer(self):
        # the gate above is live: a pointer to a file this tree lacks is refused
        files = {"devkit/modules/work-craft/payload/rules/x.md":
                 (False, b"see `rules/not-shipped-anywhere.md` for more\n")}
        with self.assertRaises(build.Refusal):
            build.check_references(files)

    def test_rules_name_each_other_by_their_installed_path(self):
        self.assertIn(".claude/rules/output-quality-standards.md", STYLE.read_text())


class Substance(unittest.TestCase):
    def test_all_twelve_numbered_standards_present(self):
        got = re.findall(r"(?m)^(\d+)\. \*\*", QUALITY.read_text())
        self.assertEqual(got, [str(i) for i in range(1, 13)])

    def test_key_clauses_of_the_standards_survive(self):
        text = QUALITY.read_text()
        for needle in ("Commit before claim", "Smoke is not e2e", "Day 1", "unattended failure",
                       "problem, who is affected, options with tradeoffs",
                       "draft", "30 minutes", "## Core Facts", "rollback",
                       "Config and governance files"):
            self.assertIn(needle, text)

    def test_style_rule_is_answer_first(self):
        text = STYLE.read_text()
        self.assertTrue(text.startswith("# Communication Style"))
        self.assertRegex(text, r"(?i)sentence one is the answer")
        for needle in ("Minto", "Density", "caveat", "undefined jargon", "Self-check", "facts-and-sources"):
            self.assertIn(needle.lower(), text.lower())

    def test_module_md_covers_both_rules(self):
        md = (WC / "MODULE.md").read_text()
        for needle in ("output-quality-standards", "communication-style"):
            self.assertGreaterEqual(md.count(needle), 2, needle)  # payload entry + body prose
        procedure = md.split("## Procedure", 1)[1].split("## State", 1)[0]
        self.assertIn("communication-style.md", procedure)
        self.assertIn("output-quality-standards.md", procedure)
        self.assertNotIn("Generic skeletons only", md)


if __name__ == "__main__":
    unittest.main()
