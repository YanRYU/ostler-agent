"""Tests for the two decision-gating modules and the warn|block mode of the outcome gate hook.

Run from the repo root:
    bash devkit/tests/test-decision-gating.sh

What is checked:
  warn-and-block  the fixture plans against the packaged hook, in warn and block mode, plus
                  the install-and-wire path
  owner-neutral   the two rules pass the lexicon gate, name the four pressure-test lenses;
                  the technical card reads "no BLOCK surface"

That test needs the optional lexicon input; without it, it skips.
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
FIX = REPO / "devkit/tests/fixtures"
BIZ = REPO / "devkit/modules/decision-gating-business"
TECH = REPO / "devkit/modules/decision-gating-technical"
PACKAGED_HOOK = BIZ / "payload/hooks/outcome-block-check.sh"
BIZ_RULE = BIZ / "payload/rules/biz-outcome-gate.md"
TECH_RULE = TECH / "payload/rules/technical-gate.md"
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

sys.path.insert(0, str(REPO / "publish"))
try:
    import build  # noqa: E402
except ImportError:  # pragma: no cover
    build = None

HAVE_JQ = shutil.which("jq") is not None


def hook_input(plan_text, tool="ExitPlanMode"):
    return json.dumps({"tool_name": tool, "tool_input": {"plan": plan_text}})


def run_hook(script, plan_text, mode=None, extra_env=None):
    env = dict(ENV)
    env.pop("OUTCOME_CHECK_MODE", None)
    env.pop("OUTCOME_CHECK_DISABLED", None)
    if mode is not None:
        env["OUTCOME_CHECK_MODE"] = mode
    env.update(extra_env or {})
    return subprocess.run(["bash", str(script)], input=hook_input(plan_text), capture_output=True,
                          text=True, env=env)


def run_installer(*args):
    return subprocess.run(["bash", str(REPO / "devkit/install/devkit-install.sh"), *map(str, args)],
                          capture_output=True, text=True, env=ENV)


TAIL = "\nRollback: revert the commit.\nVerify: run the end-to-end test.\n"


NO = (FIX / "plan-no-outcome.md").read_text()
YES = (FIX / "plan-with-outcome.md").read_text()
# Field words present, no `## Outcome` heading: passes in warn mode, must fail in block mode.
FIELDS_ONLY = ("# Plan\nThe KPI is refund rate; expected lift is half a point, measured on a dashboard "
               "by the named consumer.\nRollback: revert. Verify: end-to-end test.\n")


@unittest.skipUnless(HAVE_JQ, "jq is not installed (the hook needs it)")
class HookModes(unittest.TestCase):
    """Hook behavior in warn and block mode on the packaged copy."""

    COPIES = [("packaged", PACKAGED_HOOK)]

    def each(self):
        for label, path in self.COPIES:
            if not path.is_file():
                self.fail("missing %s hook %s" % (label, path))
            with self.subTest(copy=label):
                yield path

    def test_block_mode_blocks_the_plan_without_outcome(self):
        for h in self.each():
            r = run_hook(h, NO, mode="block")
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("## Outcome", r.stderr)

    def test_block_mode_passes_the_plan_with_outcome(self):
        for h in self.each():
            r = run_hook(h, YES, mode="block")
            self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_warn_mode_nudges_and_exits_zero(self):
        for h in self.each():
            r = run_hook(h, NO, mode="warn")
            self.assertEqual(r.returncode, 0)
            self.assertIn("## Outcome", r.stderr)
            self.assertIn("[outcome-block-check]", r.stderr)

    def test_default_mode_is_warn(self):
        for h in self.each():
            r = run_hook(h, NO)
            self.assertEqual(r.returncode, 0)
            self.assertIn("[outcome-block-check]", r.stderr)

    def test_unknown_mode_behaves_as_warn(self):
        for h in self.each():
            r = run_hook(h, NO, mode="loud")
            self.assertEqual(r.returncode, 0)

    def test_block_mode_requires_the_literal_heading(self):
        for h in self.each():
            warn = run_hook(h, FIELDS_ONLY, mode="warn")
            self.assertEqual((warn.returncode, warn.stderr), (0, ""), "field words pass in warn mode")
            blk = run_hook(h, FIELDS_ONLY, mode="block")
            self.assertEqual(blk.returncode, 2)
            self.assertIn("## Outcome", blk.stderr)

    def test_block_mode_still_blocks_a_missing_rollback_or_verification(self):
        plan = "## Outcome\nKPI: x. Lift: y. Measured: z. Consumer: w.\n1. Change the thing.\n"
        for h in self.each():
            r = run_hook(h, plan, mode="block")
            self.assertEqual(r.returncode, 2)
            self.assertIn("rollback", r.stderr.lower())

    def test_disabled_switch_wins_over_block(self):
        for h in self.each():
            r = run_hook(h, NO, mode="block", extra_env={"OUTCOME_CHECK_DISABLED": "1"})
            self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_other_tools_pass_in_block_mode(self):
        for h in self.each():
            env = dict(ENV, OUTCOME_CHECK_MODE="block")
            r = subprocess.run(["bash", str(h)], input=hook_input(NO, tool="Write"), capture_output=True,
                               text=True, env=env)
            self.assertEqual((r.returncode, r.stderr), (0, ""))

    # ---- block mode never exits 0 silently on input it cannot use
    def raw(self, h, raw, mode="block"):
        env = dict(ENV)
        env.pop("OUTCOME_CHECK_MODE", None)
        if mode:
            env["OUTCOME_CHECK_MODE"] = mode
        return subprocess.run(["bash", str(h)], input=raw, capture_output=True, text=True, env=env)

    @staticmethod
    def tool_line(name, path):
        """One transcript line shaped like Claude Code's: an assistant message holding one tool_use."""
        return json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": {"file_path": str(path)}}]}})

    def transcript_for(self, plan_text, name="plan.md", extra_lines=()):
        """A fixture transcript whose last write is to a plan file; returns (transcript, plan_file)."""
        d = Path(tempfile.mkdtemp(prefix="plan-src-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        (d / ".claude/plans").mkdir(parents=True)
        pf = d / ".claude/plans" / name
        pf.write_text(plan_text)
        tr = d / "transcript.jsonl"
        lines = [self.tool_line("Write", d / ".claude/plans/old.md")]
        lines += list(extra_lines)
        lines.append(self.tool_line("Write", pf))
        tr.write_text("\n".join(lines) + "\n")
        return tr, pf

    def exit_plan(self, transcript=None, plan=None):
        ti = {} if plan is None else {"plan": plan}
        d = {"tool_name": "ExitPlanMode", "tool_input": ti}
        if transcript is not None:
            d["transcript_path"] = str(transcript)
        return json.dumps(d)

    def test_block_mode_reads_the_plan_file_named_in_the_transcript(self):
        for h in self.each():
            tr, _ = self.transcript_for(NO)
            r = self.raw(h, self.exit_plan(transcript=tr))
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("## Outcome", r.stderr)
            tr, _ = self.transcript_for(YES)
            r = self.raw(h, self.exit_plan(transcript=tr))
            self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_a_read_of_another_plan_never_masks_the_plan_the_session_wrote(self):
        for h in self.each():
            for writer in ("Write", "Edit"):
                # wrote plan A (no Outcome), then only READ plan B (has Outcome): still blocked
                tr, pf = self.transcript_for(NO, name="a.md")
                other = pf.parent / "b.md"
                other.write_text(YES)
                tr.write_text(tr.read_text().replace('"Write"', '"%s"' % writer) + self.tool_line("Read", other) + "\n")
                r = self.raw(h, self.exit_plan(transcript=tr))
                self.assertEqual(r.returncode, 2, (writer, r.stderr))
                self.assertIn("## Outcome", r.stderr)
                # the reverse: wrote plan A (has Outcome), then READ plan B (none): passes
                tr, pf = self.transcript_for(YES, name="a.md")
                other = pf.parent / "b.md"
                other.write_text(NO)
                tr.write_text(tr.read_text().replace('"Write"', '"%s"' % writer) + self.tool_line("Read", other) + "\n")
                r = self.raw(h, self.exit_plan(transcript=tr))
                self.assertEqual((r.returncode, r.stderr), (0, ""), writer)

    def test_warn_mode_also_ignores_a_read_and_stays_silent_when_only_reads_remain(self):
        for h in self.each():
            tr, pf = self.transcript_for(YES, name="a.md")
            other = pf.parent / "b.md"
            other.write_text(NO)
            tr.write_text(tr.read_text() + self.tool_line("Read", other) + "\n")
            r = self.raw(h, self.exit_plan(transcript=tr), mode="warn")
            self.assertEqual((r.returncode, r.stderr), (0, ""))
            reads_only = pf.parent.parent.parent / "reads.jsonl"
            reads_only.write_text(self.tool_line("Read", other) + "\n")
            r = self.raw(h, self.exit_plan(transcript=reads_only), mode="warn")
            self.assertEqual((r.returncode, r.stderr), (0, ""))
            r = self.raw(h, self.exit_plan(transcript=reads_only), mode="block")
            self.assertEqual(r.returncode, 2)

    def test_block_mode_prefers_a_nonempty_tool_input_plan(self):
        for h in self.each():
            tr, _ = self.transcript_for(NO)
            r = self.raw(h, self.exit_plan(transcript=tr, plan=YES))
            self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_block_mode_unparseable_input_fails_closed(self):
        for h in self.each():
            for raw in ("not json", "{", "5"):
                r = self.raw(h, raw)
                self.assertEqual(r.returncode, 2, raw)
                self.assertEqual(r.stderr.strip(), "[outcome-block-check] cannot read hook input")

    def test_block_mode_empty_stdin_fails_closed(self):
        for h in self.each():
            r = self.raw(h, "")
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stderr.strip(), "[outcome-block-check] cannot read hook input")

    def test_block_mode_exit_plan_with_no_plan_source_fails_closed(self):
        msg = "[outcome-block-check] no plan found in tool input or plan file"
        for h in self.each():
            gone = Path(tempfile.mkdtemp(prefix="gone-")) / "transcript.jsonl"
            self.addCleanup(shutil.rmtree, str(gone.parent), True)
            tr_nofile, pf = self.transcript_for(NO)
            pf.unlink()  # the transcript names a plan file that no longer exists
            for raw in (self.exit_plan(), self.exit_plan(plan=""), self.exit_plan(transcript=gone),
                        self.exit_plan(transcript=tr_nofile),
                        json.dumps({"tool_name": "ExitPlanMode"})):
                r = self.raw(h, raw)
                self.assertEqual(r.returncode, 2, raw)
                self.assertEqual(r.stderr.strip(), msg, raw)

    def test_block_mode_empty_plan_file_fails_closed(self):
        for h in self.each():
            tr, _ = self.transcript_for("")
            r = self.raw(h, self.exit_plan(transcript=tr))
            self.assertEqual(r.returncode, 2)

    def test_warn_mode_nudges_from_the_plan_file_named_in_the_transcript(self):
        for h in self.each():
            tr, _ = self.transcript_for(NO)
            for mode in (None, "warn"):
                r = self.raw(h, self.exit_plan(transcript=tr), mode=mode)
                self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
                self.assertIn("[outcome-block-check]", r.stderr)
                self.assertIn("## Outcome", r.stderr)
                self.assertIn("Advisory only", r.stderr)
                # the same text the tool input would have carried gives the same nudge
                same = self.raw(h, self.exit_plan(plan=NO), mode=mode)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (same.returncode, same.stdout, same.stderr))

    def test_warn_mode_is_silent_when_the_plan_file_is_complete(self):
        for h in self.each():
            tr, _ = self.transcript_for(YES)
            for mode in (None, "warn"):
                r = self.raw(h, self.exit_plan(transcript=tr), mode=mode)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_warn_mode_uses_the_last_plan_file_and_prefers_the_tool_input_plan(self):
        for h in self.each():
            tr, _ = self.transcript_for(NO)  # the last write in this transcript is the plan with no outcome
            r = self.raw(h, self.exit_plan(transcript=tr, plan=YES), mode="warn")
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            r = self.raw(h, self.exit_plan(transcript=tr, plan=NO), mode="warn")
            self.assertIn("[outcome-block-check]", r.stderr)
            tr, _ = self.transcript_for(YES)
            r = self.raw(h, self.exit_plan(transcript=tr, plan=NO), mode="warn")
            self.assertIn("[outcome-block-check]", r.stderr)  # a non-empty tool input plan wins over the file

    def test_warn_mode_with_no_plan_source_is_silent_and_exits_zero(self):
        for h in self.each():
            gone = Path(tempfile.mkdtemp(prefix="gone-")) / "transcript.jsonl"
            self.addCleanup(shutil.rmtree, str(gone.parent), True)
            tr_nofile, pf = self.transcript_for(NO)
            pf.unlink()  # the transcript names a plan file that no longer exists
            tr_empty, _ = self.transcript_for("")
            for mode in (None, "warn"):
                for raw in (self.exit_plan(), self.exit_plan(plan=""), self.exit_plan(transcript=gone),
                            self.exit_plan(transcript=tr_nofile), self.exit_plan(transcript=tr_empty),
                            json.dumps({"tool_name": "ExitPlanMode"}), "not json", "{", "5", ""):
                    r = self.raw(h, raw, mode=mode)
                    self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), raw)

    def test_warn_mode_ignores_a_transcript_for_other_tools(self):
        for h in self.each():
            tr, _ = self.transcript_for(NO)
            raw = json.dumps({"tool_name": "Write", "tool_input": {}, "transcript_path": str(tr)})
            r = self.raw(h, raw, mode="warn")
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_block_mode_fence_needs_a_matching_closer(self):
        cases = {
            "nested longer fence": "````\n```\n## Outcome\n```\n````\n",
            "opener with info string, heading inside": "```python\n## Outcome\n```\n",
            "shorter closer does not close": "````\n## Outcome\n```\n",
            "other char does not close": "```\n## Outcome\n~~~\n",
            "closer with text does not close": "```\n## Outcome\n``` x\n",
            "unclosed fence": "```\n## Outcome\n",
        }
        for h in self.each():
            for label, body in cases.items():
                r = run_hook(h, "# Plan\n" + body + TAIL, mode="block")
                self.assertEqual(r.returncode, 2, "%s: %s" % (label, r.stderr))
        ok = {"longer closer": "```\ncode\n`````\n## Outcome\nKPI x\n",
              "info-string fence then heading": "```python\nx = 1\n```\n## Outcome\nKPI x\n",
              "nested then heading": "````\n```\ninner\n```\n````\n## Outcome\nKPI x\n"}
        for h in self.each():
            for label, body in ok.items():
                r = run_hook(h, "# Plan\n" + body + TAIL, mode="block")
                self.assertEqual((r.returncode, r.stderr), (0, ""), label)

    # ---- block mode accepts only an exact `## Outcome` line outside code
    def test_block_mode_rejects_every_near_miss_heading(self):
        cases = {
            "single hash": "# Outcome\nKPI x\n",
            "three hashes, extra word": "### Outcomes tbd\nKPI x\n",
            "no space after hash": "#outcome\nKPI x\n",
            "inside a backtick fence": "```\n## Outcome\nKPI x\n```\n",
            "inside a tilde fence": "~~~\n## Outcome\nKPI x\n~~~\n",
            "indented heading": "    # outcome\nKPI x\n",
            "indented exact heading": "    ## Outcome\nKPI x\n",
            "tab indented exact heading": "\t## Outcome\nKPI x\n",
            "lowercase": "## outcome\nKPI x\n",
            "suffix word": "## Outcome tbd\nKPI x\n",
        }
        for h in self.each():
            for label, body in cases.items():
                r = run_hook(h, "# Plan\n" + body + TAIL, mode="block")
                self.assertEqual(r.returncode, 2, "%s: %s" % (label, r.stderr))
                self.assertIn("## Outcome", r.stderr)

    def test_block_mode_still_passes_exact_headings(self):
        cases = {
            "plain": "## Outcome\nKPI x\n",
            "trailing spaces": "## Outcome   \nKPI x\n",
            "after a closed fence": "```\ncode\n```\n## Outcome\nKPI x\n",
            "after a closed tilde fence": "~~~\ncode\n~~~\n## Outcome\nKPI x\n",
            "crlf line ends": "## Outcome\r\nKPI x\r\n",
        }
        for h in self.each():
            for label, body in cases.items():
                r = run_hook(h, "# Plan\n" + body + TAIL, mode="block")
                self.assertEqual((r.returncode, r.stderr), (0, ""), label)

    def test_warn_mode_detection_is_unchanged_for_near_misses(self):
        for h in self.each():
            for body in ("# Outcome\nx\n", "#outcome\nx\n", "    # outcome\nx\n", "```\n## Outcome\n```\n"):
                r = run_hook(h, "# Plan\n" + body + TAIL, mode="warn")
                self.assertEqual((r.returncode, r.stderr), (0, ""), body)

    def bin_without_jq(self):
        """A PATH directory holding the ordinary tools the hook uses, but no jq."""
        d = tempfile.mkdtemp(prefix="no-jq-")
        self.addCleanup(shutil.rmtree, d, True)
        for tool in ("cat", "tr", "grep", "awk", "sed", "echo", "printf"):
            src = shutil.which(tool)
            if src:
                os.symlink(src, os.path.join(d, tool))
        self.assertIsNone(shutil.which("jq", path=d))
        return d

    # ---- block mode fails closed without jq
    def test_block_mode_without_jq_fails_closed(self):
        empty = self.bin_without_jq()
        for h in self.each():
            env = dict(ENV, PATH=empty, OUTCOME_CHECK_MODE="block")
            r = subprocess.run(["/bin/bash", str(h)], input=hook_input(YES), capture_output=True, text=True,
                               env=env)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stderr.strip(),
                             "[outcome-block-check] jq not found; block mode cannot check plans")

    def test_warn_mode_without_jq_keeps_its_behavior(self):
        empty = self.bin_without_jq()
        for h in self.each():
            for mode in (None, "warn"):
                env = dict(ENV, PATH=empty)
                env.pop("OUTCOME_CHECK_MODE", None)
                if mode:
                    env["OUTCOME_CHECK_MODE"] = mode
                r = subprocess.run(["/bin/bash", str(h)], input=hook_input(NO), capture_output=True,
                                   text=True, env=env)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
                tr, _ = self.transcript_for(NO)
                r = subprocess.run(["/bin/bash", str(h)], input=self.exit_plan(transcript=tr),
                                   capture_output=True, text=True, env=env)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    # ---- the mode value is lowercased and trimmed
    def test_mode_value_is_case_and_space_insensitive(self):
        for h in self.each():
            for val in ("BLOCK", "Block", " block ", "block\t"):
                r = run_hook(h, NO, mode=val)
                self.assertEqual(r.returncode, 2, repr(val))
            for val in ("WARN", " Warn "):
                r = run_hook(h, NO, mode=val)
                self.assertEqual(r.returncode, 0, repr(val))


class ModuleShape(unittest.TestCase):
    def meta(self, mod):
        sys.path.insert(0, str(REPO / "devkit/install"))
        import importlib
        dk = importlib.import_module("_devkit")
        return dk, dk.discover_modules()

    def test_both_modules_are_valid_v2_modules_named_after_their_domain(self):
        dk, mods = self.meta(None)
        for name in ("decision-gating-business", "decision-gating-technical"):
            self.assertIn(name, mods)
            self.assertEqual(mods[name]["_errors"], [], name)
            self.assertEqual(mods[name]["domain"], name)
            # pinned versions: bump the patch version whenever the shipped text of a module changes
            self.assertEqual(str(mods[name]["version"]), {"decision-gating-business": "1.0.17", "decision-gating-technical": "1.0.4"}[name])

    def test_declared_mechanisms(self):
        dk, mods = self.meta(None)
        biz = {e["dest"]: e["mechanism"] for e in mods["decision-gating-business"]["payload"]}
        self.assertEqual(biz[".claude/hooks/outcome-block-check.sh"], "DETECT")
        self.assertEqual(biz[".claude/rules/biz-outcome-gate.md"], "INSTRUCT")
        tech = {e["dest"]: e["mechanism"] for e in mods["decision-gating-technical"]["payload"]}
        self.assertEqual(tech, {".claude/rules/technical-gate.md": "INSTRUCT"})

    def test_business_module_documents_the_wiring_and_both_modes(self):
        text = (BIZ / "MODULE.md").read_text()
        self.assertIn("ExitPlanMode", text)
        self.assertIn(".claude/hooks/outcome-block-check.sh", text)
        self.assertIn("OUTCOME_CHECK_MODE=block", text)
        self.assertRegex(text, r"(?s)## Prevents.*BLOCK.*## Procedure")

    def test_business_module_documents_the_jq_requirement_and_symptom(self):
        text = (BIZ / "MODULE.md").read_text()
        self.assertIn("jq not found; block mode cannot check plans", text)
        self.assertRegex(text, r"(?i)needs `?jq`?")
        self.assertIn("OUTCOME_CHECK_MODE=block", text)

    def test_business_module_documents_plan_sources_and_the_twin_rule(self):
        text = (BIZ / "MODULE.md").read_text()
        self.assertIn("transcript", text)
        self.assertIn("cannot read hook input", text)
        self.assertIn("no plan found in tool input or plan file", text)
        self.assertRegex(text, r"(?s)decision-gating-technical.*(MISSING|missing)")

    def test_business_module_says_both_modes_read_the_same_plan_sources(self):
        text = (BIZ / "MODULE.md").read_text()
        self.assertRegex(text, r"(?s)Where the hook reads the plan from.*(warn|Warn) mode.*same")
        self.assertNotRegex(text, r"(?i)warn mode only reads the tool input")

    def test_technical_card_claim_is_conditional_on_the_business_hook(self):
        text = (TECH / "MODULE.md").read_text()
        prev = re.search(r"(?s)## Prevents(.*?)## Procedure", text).group(1)
        self.assertIn("no BLOCK surface", prev)
        self.assertRegex(prev, r"(?s)only when .*installed.*wired")
        self.assertNotIn("checked by decision-gating-business's hook", prev)

    def test_readme_table_is_current(self):
        r = subprocess.run([sys.executable, str(REPO / "devkit/install/_devkit.py"), "readme", "--check"],
                           capture_output=True, text=True, env=ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class InstallAndBoard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = Path(tempfile.mkdtemp(prefix="devkit-dg-"))
        (cls.t / ".claude").mkdir()
        cls.install = run_installer("install", cls.t, "decision-gating-business",
                                    "decision-gating-technical", "audit", "--allow-dirty")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.t, ignore_errors=True)

    def test_install_succeeds_and_copies_all_payload_files(self):
        self.assertEqual(self.install.returncode, 0, self.install.stdout + self.install.stderr)
        for rel in (".claude/hooks/outcome-block-check.sh", ".claude/rules/biz-outcome-gate.md",
                    ".claude/rules/technical-gate.md"):
            self.assertTrue((self.t / rel).is_file(), rel)

    def board(self):
        r = subprocess.run([sys.executable, str(self.t / ".claude/devkit-audit.py"), "board", str(self.t)],
                           capture_output=True, text=True, env=ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def card(self, name):
        out = self.board()
        parts = re.split(r"(?m)^(?==== )", out)
        for p in parts:
            if p.startswith("=== %s " % name):
                return p
        self.fail("no card for %s in:\n%s" % (name, out))

    def test_technical_card_reads_no_block_surface(self):
        card = self.card("decision-gating-technical")
        self.assertIn("no BLOCK surface", card)
        self.assertIn("decision-gating-business", card)
        self.assertNotIn("[BLOCK]", card)

    def test_audit_registers_both_rules_the_hook_enforces(self):
        r = subprocess.run([sys.executable, str(self.t / ".claude/devkit-audit.py"), str(self.t)],
                           capture_output=True, text=True, env=ENV)
        gap = r.stdout
        self.assertNotIn("technical-gate.md: no hook declares it", gap)
        self.assertNotIn("biz-outcome-gate.md: no hook declares it", gap)
        self.assertNotIn("outcome-block-check.sh: Enforces", gap)

    @unittest.skipUnless(HAVE_JQ, "jq is not installed")
    def test_wired_hook_blocks_and_passes_through_its_settings_entry(self):
        settings = {"hooks": {"PreToolUse": [{"matcher": "ExitPlanMode", "hooks": [
            {"type": "command", "command": "bash .claude/hooks/outcome-block-check.sh"}]}]}}
        sp = self.t / ".claude/settings.json"
        sp.write_text(json.dumps(settings))
        self.assertIn("PreToolUse:ExitPlanMode", self.card("decision-gating-business"))
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        env = dict(ENV, OUTCOME_CHECK_MODE="block")
        no = subprocess.run(cmd, shell=True, cwd=self.t, input=hook_input(NO), capture_output=True,
                            text=True, env=env)
        yes = subprocess.run(cmd, shell=True, cwd=self.t, input=hook_input(YES), capture_output=True,
                             text=True, env=env)
        self.assertEqual(no.returncode, 2, no.stderr)
        self.assertIn("## Outcome", no.stderr)
        self.assertEqual(yes.returncode, 0, yes.stderr)
        warn = subprocess.run(cmd, shell=True, cwd=self.t, input=hook_input(NO), capture_output=True,
                              text=True, env=dict(ENV, OUTCOME_CHECK_MODE="warn"))
        self.assertEqual(warn.returncode, 0)
        self.assertIn("[outcome-block-check]", warn.stderr)


class OwnerNeutral(unittest.TestCase):
    FILES = [BIZ_RULE, TECH_RULE, PACKAGED_HOOK, BIZ / "MODULE.md", TECH / "MODULE.md",
             FIX / "plan-no-outcome.md", FIX / "plan-with-outcome.md"]

    @unittest.skipIf(build is None or not (REPO / "publish/lexicon.yaml").is_file(),
                     "optional input absent: no lexicon")
    def test_lexicon_gate_zero_hits(self):
        lex = build.parse_lexicon((REPO / "publish/lexicon.yaml").read_text())
        files = {str(p.relative_to(REPO)): (False, p.read_bytes()) for p in self.FILES}
        files[str(Path(__file__).relative_to(REPO))] = (False, Path(__file__).read_bytes())
        build.scan_lexicon(lex, files)  # raises Refusal on any hit

    def test_business_rule_names_the_four_lenses_in_a_pressure_test_section(self):
        text = BIZ_RULE.read_text()
        m = re.search(r"(?s)## Pressure-test.*?(?=\n## |\Z)", text)
        self.assertTrue(m, "no ## Pressure-test section")
        sec = m.group(0).lower()
        for lens in ("strategy", "finance", "market", "product"):
            self.assertIn("**%s**" % lens, sec)

    def test_technical_rule_uses_generic_units_of_analysis(self):
        text = TECH_RULE.read_text()
        self.assertIn("customer, product, region, time period", text)

    def test_rules_keep_the_gate_content(self):
        biz = BIZ_RULE.read_text()
        for needle in ("## Outcome", "Short horizon", "Consumer test", "Lever sizing"):
            self.assertIn(needle, biz)
        # the horizon stays a hard rule; only its length is the reader's to set
        self.assertIn("**Short horizon (hard; default 2 weeks", biz)

    def test_the_reminder_and_the_module_text_ask_for_what_the_rule_asks_for(self):
        """The rule's first question is a business KPI, not an internal proxy; the reminder and the module's
        Prevents text must say so (and ask for the lift, when and how it is measured, the consumer and the
        decision)."""
        hook = PACKAGED_HOOK.read_text()
        m = re.search(r"(?s)<<'F'\n(.*?)\nF\n", hook)
        self.assertTrue(m, "no business reminder block in the hook")
        text = " ".join(m.group(1).split())
        for needle in ("business KPI", "not an internal proxy", "expected lift", "measured-on", "how you will check",
                       "consumer", "decision"):
            self.assertIn(needle, text)
        prev = re.search(r"(?s)## Prevents(.*?)## Procedure", (BIZ / "MODULE.md").read_text()).group(1)
        prev = " ".join(prev.split())
        for needle in ("business KPI", "not an internal proxy", "how much", "how and when", "consumer", "decision"):
            self.assertIn(needle, prev)
        tech = TECH_RULE.read_text()
        for needle in ("Blast radius", "Rollback", "Verification", "Order"):
            self.assertIn(needle, tech)


if __name__ == "__main__":
    unittest.main()
