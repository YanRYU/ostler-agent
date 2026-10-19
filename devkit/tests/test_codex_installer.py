"""Observable contract for the Codex installer entry point."""
from __future__ import annotations

import json
import hashlib
import io
import os
import runpy
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLAUDE = ROOT / "devkit" / "install" / "devkit-install.sh"
CODEX = ROOT / "devkit" / "install" / "codex-install.sh"
AUDIT = ROOT / "devkit" / "modules" / "audit" / "payload" / "devkit-audit.py"
HOOKS = ROOT / "devkit" / "codex-modules" / "codex-hooks-core" / "payload" / "hooks"


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
                    {"module": "codex-instructions", "fingerprint": "old", "files": []},
                ],
            }))
            codex = run_installer(CODEX, "outdated", str(target))
            claude = run_installer(CLAUDE, "outdated", str(target))
            self.assertEqual(codex.returncode, 0, codex.stderr)
            self.assertIn("codex-removed outdated", codex.stdout)
            self.assertNotIn("rules-core outdated", codex.stdout)
            self.assertIn("codex-install.sh install", codex.stdout)
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
                ".agents/ostler/templates/EXAMPLE-SKILL/SKILL.md",
                ".codex/rules/ostler.rules", ".codex/devkit-audit.py",
                ".devkit-manifest.json",
            ):
                self.assertTrue((target / rel).is_file(), rel)
            self.assertFalse((target / ".agents" / "skills" / "EXAMPLE-SKILL").exists())
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
            clean_dry_run = subprocess.run(
                ["codex", "execpolicy", "check", "--rules", str(rule), "git", "clean", "-n"],
                capture_output=True, text=True,
            )
            self.assertEqual(clean_dry_run.returncode, 0, clean_dry_run.stderr)
            self.assertEqual(json.loads(clean_dry_run.stdout)["decision"], "prompt")
            clean_force = subprocess.run(
                ["codex", "execpolicy", "check", "--rules", str(rule), "git", "clean", "-fd"],
                capture_output=True, text=True,
            )
            self.assertEqual(clean_force.returncode, 0, clean_force.stderr)
            self.assertEqual(json.loads(clean_force.stdout)["decision"], "forbidden")

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

    def test_audit_does_not_scan_unrelated_root_hooks_or_change_claude_gap_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            claude_hooks = target / ".claude" / "hooks"
            claude_hooks.mkdir(parents=True)
            (claude_hooks / "check.py").write_text("# Enforces: rules/privacy.md\n")
            root_hooks = target / "hooks"
            root_hooks.mkdir()
            (root_hooks / "unrelated.py").write_text("print('hello')\n")
            result = subprocess.run(["python3", "-S", str(AUDIT), str(target)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("unrelated.py", result.stdout)
            self.assertNotIn("privacy.md but that rule file is MISSING", result.stdout)

    def test_claude_and_codex_audit_payloads_match(self) -> None:
        codex_audit = ROOT / "devkit" / "codex-modules" / "codex-audit" / "payload" / "devkit-audit.py"
        self.assertEqual(AUDIT.read_bytes(), codex_audit.read_bytes())

    def test_audit_reports_missing_codex_hook_script_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            codex = target / ".codex"
            codex.mkdir()
            (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{
                "matcher": "Bash", "hooks": [{"type": "command", "command": "python3 .codex/hooks/missing.py"}]
            }]}}))
            result = subprocess.run(["python3", "-S", str(AUDIT), str(target)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("missing.py", result.stdout)
            self.assertIn("MISSING", result.stdout)


class CodexHookTests(unittest.TestCase):
    def make_repo(self, target: Path) -> None:
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "Tester"], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "tester@example.com"], check=True)

    def run_hook(self, script: str, target: Path, payload: dict, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(HOOKS / script)], cwd=target, input=json.dumps(payload),
            capture_output=True, text=True, env={**os.environ, **env},
        )

    def test_hooks_ignore_non_object_tool_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for script in ("branch_similarity.py", "stash_leak.py", "personal_path_guard.py"):
                result = self.run_hook(script, Path(tmp), {"tool_name": "Bash", "tool_input": None})
                self.assertEqual(result.returncode, 0, (script, result.stderr))
                self.assertEqual(result.stdout, "", script)

    def test_branch_hook_surfaces_existing_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.make_repo(target)
            (target / "readme.txt").write_text("initial\n")
            subprocess.run(["git", "add", "readme.txt"], cwd=target, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=target, check=True)
            subprocess.run(["git", "switch", "-q", "-c", "feature/work-a"], cwd=target, check=True)
            result = self.run_hook("branch_similarity.py", target, {
                "hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "git switch -c feature/work-a"},
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already exists", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])
            long_option = self.run_hook("branch_similarity.py", target, {
                "hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "git switch --create feature/work-a"},
            })
            self.assertIn("already exists", json.loads(long_option.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_branch_hook_surfaces_shared_domain_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.make_repo(target)
            (target / "readme.txt").write_text("initial\n")
            subprocess.run(["git", "add", "readme.txt"], cwd=target, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=target, check=True)
            subprocess.run(["git", "branch", "feature/codex-installer-current"], cwd=target, check=True)
            result = self.run_hook("branch_similarity.py", target, {
                "hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "git switch -c feature/codex-installer-next"},
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("feature/codex-installer-current", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_stash_hook_surfaces_remaining_stash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.make_repo(target)
            (target / "a.txt").write_text("one\n")
            subprocess.run(["git", "add", "a.txt"], cwd=target, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=target, check=True)
            (target / "a.txt").write_text("two\n")
            subprocess.run(["git", "stash", "push", "-qm", "test stash"], cwd=target, check=True)
            result = self.run_hook("stash_leak.py", target, {
                "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "git commit -m done"},
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("test stash", json.loads(result.stdout)["systemMessage"])
            indented = self.run_hook("stash_leak.py", target, {
                "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "  git push origin main"},
            })
            self.assertIn("test stash", json.loads(indented.stdout)["systemMessage"])

    def test_path_hook_warns_for_shared_patch_but_skips_ignored_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.make_repo(target)
            patch = "*** Begin Patch\n*** Add File: shared.txt\n+/Users/alice/secret\n*** End Patch\n"
            payload = {"hook_event_name": "PreToolUse", "tool_name": "apply_patch",
                       "tool_input": {"command": patch}, "cwd": str(target)}
            warning = self.run_hook("personal_path_guard.py", target, payload)
            self.assertEqual(warning.returncode, 0, warning.stderr)
            self.assertIn("personal absolute path", json.loads(warning.stdout)["hookSpecificOutput"]["additionalContext"])
            (target / ".gitignore").write_text("private.txt\n")
            payload["tool_input"]["command"] = patch.replace("shared.txt", "private.txt")
            ignored = self.run_hook("personal_path_guard.py", target, payload)
            self.assertEqual(ignored.returncode, 0, ignored.stderr)
            self.assertEqual(ignored.stdout, "")

    def test_path_hook_checks_repository_codex_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.make_repo(target)
            payload = {
                "hook_event_name": "PreToolUse", "tool_name": "apply_patch",
                "tool_input": {"command": "*** Begin Patch\n*** Add File: .codex/config.toml\n+path = '/Users/alice/secret'\n*** End Patch\n"},
                "cwd": str(target),
            }
            result = self.run_hook("personal_path_guard.py", target, payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(".codex/config.toml", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_path_hook_checks_patch_move_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.make_repo(target)
            (target / ".gitignore").write_text("secret.txt\n")
            payload = {
                "hook_event_name": "PreToolUse", "tool_name": "apply_patch",
                "tool_input": {"command": "*** Begin Patch\n*** Update File: secret.txt\n*** Move to: shared.txt\n@@\n+path=/Users/alice/project\n*** End Patch\n"},
                "cwd": str(target),
            }
            result = self.run_hook("personal_path_guard.py", target, payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("shared.txt", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_hooks_install_with_valid_codex_config_and_activation_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            self.make_repo(target)
            result = run_installer(CODEX, "install", str(target), "codex-instructions", "codex-hooks-core", "--allow-dirty")
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads((target / ".codex" / "hooks.json").read_text())
            self.assertIn("PreToolUse", config["hooks"])
            self.assertIn("PostToolUse", config["hooks"])
            self.assertIn("/hooks", result.stdout)
            self.assertIn("trust", result.stdout.lower())

    def test_hooks_refuse_non_git_target_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "plain"
            result = run_installer(CODEX, "install", str(target), "codex-hooks-core", "--allow-dirty")
            self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
            self.assertIn("Git repository root", result.stderr)
            self.assertFalse(target.exists())

    def test_hooks_accept_target_with_different_path_case_on_case_insensitive_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "CaseRepo"
            self.make_repo(target)
            alternate = target.with_name("caserepo")
            if not alternate.exists() or not os.path.samefile(target, alternate):
                self.skipTest("requires a case-insensitive filesystem")
            result = run_installer(CODEX, "install", str(alternate), "codex-hooks-core", "--allow-dirty")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_audit_reports_non_object_hook_config_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            codex = target / ".codex"
            codex.mkdir()
            (codex / "hooks.json").write_text("[]\n")
            output = io.StringIO()
            with redirect_stdout(output):
                runpy.run_path(str(AUDIT))["check_enforcement_gaps"](target)
            self.assertIn("config is not an object", output.getvalue())

    def test_hook_command_exits_cleanly_if_git_root_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads((HOOKS.parent / "hooks.json").read_text())
            command = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            result = subprocess.run(["sh", "-c", command], cwd=tmp,
                                    input='{"tool_name":"Bash","tool_input":{"command":"git switch -c work"}}',
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_nested_hook_config_is_rejected_by_preflight(self) -> None:
        installer = runpy.run_path(str(ROOT / "devkit" / "install" / "_devkit.py"))
        validate = installer["validate_codex_hooks"]
        config = {"hooks": {"PreToolUse": [{"matcher": "[", "hooks": [{"type": "command", "command": "echo ok"}]}],
                            "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo ok"}]}]}}
        with self.assertRaises(ValueError):
            validate(json.dumps(config).encode())
        config["hooks"]["PreToolUse"][0]["matcher"] = "Bash"
        config["hooks"]["PostToolUse"][0]["hooks"][0]["command"] = ""
        with self.assertRaises(ValueError):
            validate(json.dumps(config).encode())
        config["hooks"].pop("PostToolUse")
        validate(json.dumps(config).encode())


class MixedInstallTests(unittest.TestCase):
    def test_codex_module_updates_from_new_factory_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            factory = base / "factory"
            shutil.copytree(ROOT / "devkit" / "install", factory / "devkit" / "install")
            shutil.copytree(ROOT / "install" / "lib", factory / "install" / "lib")
            shutil.copy(ROOT / "devkit" / "domains.json", factory / "devkit" / "domains.json")
            module = factory / "devkit" / "codex-modules" / "codex-instructions"
            shutil.copytree(ROOT / "devkit" / "codex-modules" / "codex-instructions", module)
            subprocess.run(["git", "init", "-q", str(factory)], check=True)
            for key, value in (("user.name", "Tester"), ("user.email", "tester@example.com")):
                subprocess.run(["git", "-C", str(factory), "config", key, value], check=True)
            subprocess.run(["git", "-C", str(factory), "add", "."], check=True)
            subprocess.run(["git", "-C", str(factory), "commit", "-qm", "initial"], check=True)
            target = base / "target"
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            wrapper = factory / "devkit" / "install" / "codex-install.sh"
            first = run_installer(wrapper, "install", str(target), "codex-instructions")
            self.assertEqual(first.returncode, 0, first.stderr)
            source = module / "payload" / "AGENTS.md"
            source.write_text(source.read_text() + "\nUpdated guidance.\n")
            subprocess.run(["git", "-C", str(factory), "add", "."], check=True)
            subprocess.run(["git", "-C", str(factory), "commit", "-qm", "update"], check=True)
            second = run_installer(wrapper, "install", str(target), "codex-instructions")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("Updated guidance.", (target / "AGENTS.md").read_text())

    def test_module_cannot_take_over_another_modules_managed_path(self) -> None:
        installer = runpy.run_path(str(ROOT / "devkit" / "install" / "_devkit.py"))
        installer["build_install_plan"].__globals__["PLATFORM"] = "codex"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "repo"
            target.mkdir()
            dest = target / "shared.txt"
            dest.write_text("old\n")
            module = base / "codex-collision"
            payload = module / "payload"
            payload.mkdir(parents=True)
            (payload / "new.txt").write_text("new\n")
            prior = {"installs": [{"module": "rules-core", "files": [{
                "path": "shared.txt", "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
                "mode": "overwrite",
            }]}]}
            mods = {"codex-collision": {"_dir": module, "payload": [
                {"src": "new.txt", "dest": "shared.txt", "mode": "overwrite"}
            ]}}
            plan, _ = installer["build_install_plan"](target, ["codex-collision"], mods, prior, False)
            self.assertTrue(any("managed-by-other-module" in issue for issue in plan.conflicts), plan.conflicts)
            forced, _ = installer["build_install_plan"](target, ["codex-collision"], mods, prior, True)
            self.assertTrue(any("managed-by-other-module" in issue for issue in forced.conflicts), forced.conflicts)

    def test_codex_wrapper_cannot_remove_claude_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            first = run_installer(CLAUDE, "install", str(target), "git-multi-session", "--allow-dirty")
            self.assertEqual(first.returncode, 0, first.stderr)
            before = (target / ".devkit-manifest.json").read_bytes()
            result = run_installer(CODEX, "remove-module", str(target), "git-multi-session")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((target / ".devkit-manifest.json").read_bytes(), before)
            self.assertTrue((target / ".claude" / "rules" / "git-hygiene.md").is_file())

    def test_mixed_target_updates_only_each_platform_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            for wrapper, module in ((CLAUDE, "git-multi-session"),
                                    (CODEX, "codex-command-rules")):
                result = run_installer(wrapper, "install", str(target), module, "--allow-dirty")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for wrapper in (CODEX, CLAUDE):
                result = run_installer(wrapper, "update", str(target), "--allow-dirty")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            names = {entry["module"] for entry in json.loads(
                (target / ".devkit-manifest.json").read_text())["installs"]}
            self.assertEqual(names, {"git-multi-session", "codex-command-rules"})

    def test_end_to_end_preserves_claude_during_codex_install_and_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            claude = run_installer(CLAUDE, "install", str(target), "git-multi-session", "--allow-dirty")
            self.assertEqual(claude.returncode, 0, claude.stderr)
            codex = run_installer(CODEX, "install", str(target), "--allow-dirty")
            self.assertEqual(codex.returncode, 0, codex.stderr)
            manifest = json.loads((target / ".devkit-manifest.json").read_text())
            names = {entry["module"] for entry in manifest["installs"]}
            self.assertIn("git-multi-session", names)
            self.assertIn("codex-hooks-core", names)
            self.assertTrue((target / ".claude" / "rules").is_dir())
            self.assertTrue((target / ".codex" / "hooks.json").is_file())

            checked = run_installer(CODEX, "check", str(target))
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            installed_audit = subprocess.run(
                ["python3", "-S", str(target / ".codex" / "devkit-audit.py"), str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(installed_audit.returncode, 0, installed_audit.stdout + installed_audit.stderr)
            repeated = run_installer(CODEX, "install", str(target), "--allow-dirty")
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn("up to date", repeated.stdout)
            recovered = run_installer(CODEX, "recover", str(target))
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn("no interrupted transaction", recovered.stdout)

            removed = run_installer(CODEX, "remove-module", str(target), "codex-hooks-core")
            self.assertEqual(removed.returncode, 0, removed.stderr)
            after = json.loads((target / ".devkit-manifest.json").read_text())
            names_after = {entry["module"] for entry in after["installs"]}
            self.assertIn("git-multi-session", names_after)
            self.assertNotIn("codex-hooks-core", names_after)
            self.assertFalse((target / ".codex" / "hooks.json").exists())
            self.assertTrue((target / ".claude" / "rules").is_dir())

    def test_modified_hook_config_refuses_without_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            first = run_installer(CODEX, "install", str(target), "--allow-dirty")
            self.assertEqual(first.returncode, 0, first.stderr)
            hooks = target / ".codex" / "hooks.json"
            hooks.write_text("{}\n")
            before = (target / ".devkit-manifest.json").read_bytes()
            second = run_installer(CODEX, "install", str(target), "--allow-dirty")
            self.assertEqual(second.returncode, 3, second.stdout + second.stderr)
            self.assertEqual(hooks.read_text(), "{}\n")
            self.assertEqual((target / ".devkit-manifest.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
