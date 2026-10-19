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


if __name__ == "__main__":
    unittest.main()
