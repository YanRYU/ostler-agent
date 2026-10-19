"""Observable contract for the Codex installer entry point."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLAUDE = ROOT / "devkit" / "install" / "devkit-install.sh"
CODEX = ROOT / "devkit" / "install" / "codex-install.sh"
AUDIT = ROOT / "devkit" / "modules" / "audit" / "payload" / "devkit-audit.py"


def run_installer(wrapper: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(wrapper), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


class PlatformSelectionTests(unittest.TestCase):
    def test_codex_wrapper_rejects_claude_module_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            result = run_installer(CODEX, "install", str(target), "rules-core")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("unknown module", result.stderr)
            self.assertFalse(target.exists())

    def test_codex_outdated_reports_only_codex_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".devkit-manifest.json").write_text(json.dumps({
                "schema_version": 2,
                "installs": [
                    {"module": "rules-core", "fingerprint": "old", "files": []},
                    {"module": "codex-removed", "fingerprint": "old", "files": []},
                ],
            }))
            codex = run_installer(CODEX, "outdated", str(target))
            claude = run_installer(CLAUDE, "outdated", str(target))
            self.assertEqual(codex.returncode, 0, codex.stderr)
            self.assertIn("codex-removed outdated", codex.stdout)
            self.assertNotIn("rules-core outdated", codex.stdout)
            self.assertEqual(claude.returncode, 0, claude.stderr)
            self.assertNotIn("codex-removed outdated", claude.stdout)


class CodexPayloadTests(unittest.TestCase):
    MODULES = ("codex-instructions", "codex-command-rules", "codex-skill-template", "codex-audit")

    def install(self, target: Path) -> subprocess.CompletedProcess[str]:
        return run_installer(CODEX, "install", str(target), *self.MODULES, "--allow-dirty")

    def test_installs_native_files_and_self_contained_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            result = self.install(target)
            self.assertEqual(result.returncode, 0, result.stderr)
            for rel in (
                "AGENTS.md", ".agents/ostler/rules/git-hygiene.md",
                ".agents/ostler/rules/plan-writing.md",
                ".agents/ostler/rules/privacy.md",
                ".agents/ostler/rules/self-contained-agents.md",
                ".agents/skills/EXAMPLE-SKILL/SKILL.md",
                ".codex/rules/ostler.rules", ".codex/devkit-audit.py",
                ".devkit-manifest.json",
            ):
                self.assertTrue((target / rel).is_file(), rel)
            installed_audit = subprocess.run(
                ["python3", "-S", str(target / ".codex" / "devkit-audit.py"), str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(installed_audit.returncode, 0, installed_audit.stderr)
            self.assertIn("drift check", installed_audit.stdout)
            factory_check = run_installer(CODEX, "check", str(target))
            self.assertEqual(factory_check.returncode, 0, factory_check.stderr)
            repeated = self.install(target)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn("up to date", repeated.stdout)

    def test_unmanaged_agents_file_refuses_without_partial_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            target.mkdir()
            (target / "AGENTS.md").write_text("my instructions\n")
            result = self.install(target)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertEqual((target / "AGENTS.md").read_text(), "my instructions\n")
            self.assertFalse((target / ".devkit-manifest.json").exists())
            self.assertFalse((target / ".agents").exists())

    def test_installed_command_rules_prompt_before_git_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            result = self.install(target)
            self.assertEqual(result.returncode, 0, result.stderr)
            rule = target / ".codex" / "rules" / "ostler.rules"
            checked = subprocess.run(
                ["codex", "execpolicy", "check", "--rules", str(rule), "git", "push", "origin", "main"],
                capture_output=True, text=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(json.loads(checked.stdout)["decision"], "prompt")

    def test_audit_reports_codex_hook_without_its_instruction_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            hooks = target / ".codex" / "hooks"
            hooks.mkdir(parents=True)
            (hooks / "path_guard.py").write_text(
                "# Enforces: .agents/ostler/rules/privacy.md\n"
            )
            result = subprocess.run(
                ["python3", "-S", str(AUDIT), str(target)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("privacy.md", result.stdout)
            self.assertIn("MISSING", result.stdout)


if __name__ == "__main__":
    unittest.main()
