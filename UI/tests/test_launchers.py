from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class _Root:
    def __init__(self) -> None:
        self.mainloop_called = False

    def mainloop(self) -> None:
        self.mainloop_called = True


class LauncherTests(unittest.TestCase):
    def _assert_launcher(self, module_name: str, ai, verbose: bool) -> None:
        module = __import__(module_name, fromlist=["main"])
        root = _Root()
        with mock.patch.object(module.tk, "Tk", return_value=root), mock.patch.object(
            module, "AutoR3DUI"
        ) as ui_class:
            self.assertEqual(module.main([]), 0)
        kwargs = ui_class.call_args.kwargs
        self.assertIs(kwargs["ai_assistant"], ai)
        self.assertEqual(kwargs["console_verbose"], verbose)
        self.assertTrue(root.mainloop_called)

    def test_autocrys_follows_config_and_is_compact(self):
        self._assert_launcher("UI.ui", None, False)

    def test_autocrys_ai_forces_ai_and_is_compact(self):
        self._assert_launcher("UI.ui_ai", True, False)

    def test_autocrys_base_forces_no_ai_and_is_compact(self):
        self._assert_launcher("UI.ui_base", False, False)

    def test_autocrys_main_follows_config_and_is_verbose(self):
        self._assert_launcher("UI.ui_main", None, True)

    def test_package_data_excludes_machine_local_ai_config(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("include-package-data = false", pyproject)
        self.assertIn(
            'config = ["ai_assistant.example.json", "environment-autocrys.yml"]',
            pyproject,
        )
        self.assertNotIn('config = ["*.json", "*.yml"]', pyproject)

    def test_package_data_excludes_unrelated_autosolve_binaries(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn('"lib/*"', pyproject)
        self.assertNotIn('"tools/*"', pyproject)
        self.assertIn('"lib/shelxt_space_groups.json"', pyproject)
        self.assertNotIn('"tools/shelxl"', pyproject)
        self.assertNotIn('"tools/shelxt.bz2"', pyproject)

    def test_source_distribution_excludes_local_state(self):
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("exclude config/ai_assistant.json", manifest)
        self.assertIn("prune Data", manifest)
        self.assertIn("prune log", manifest)
        self.assertIn("recursive-exclude AutoSolve/tools *", manifest)
        self.assertNotIn("include AutoSolve/tools/shelxl", manifest)
        self.assertNotIn("include AutoSolve/tools/shelxt.bz2", manifest)

    def test_deployment_runtime_is_python_312(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        environment = (ROOT / "config" / "environment-autocrys.yml").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.12"', pyproject)
        self.assertIn("python=3.12", environment)

    def test_existing_deployment_self_check_is_packaged(self):
        from scripts import update_self_check

        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertEqual(update_self_check.PROJECT_ROOT, ROOT)
        self.assertEqual(update_self_check.shell_profile(Path("/usr/bin/bash2")).name, ".bashrc")
        self.assertIn('autocrys-self-check = "scripts.update_self_check:main"', pyproject)
        self.assertIn('"scripts*"', pyproject)
        self.assertIn("include scripts/update_self_check.py", manifest)


if __name__ == "__main__":
    unittest.main()
