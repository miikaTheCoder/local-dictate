"""Separate manager installation, using a temporary HOME without activation."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]


class ModelInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dictate-model-install-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.source = self.root / "checkout with spaces"
        for folder in ("scripts", "bin"):
            (self.source / folder).mkdir(parents=True, exist_ok=True)
        for filename in ("model_manager.py", "asr.example.toml", "scripts/install-model-manager.sh", "bin/local-dictate-model"):
            shutil.copy2(SOURCE / filename, self.source / filename)
        self.script = self.source / "scripts/install-model-manager.sh"
        self.destination = self.home / ".local/bin/local-dictate-model"
        self.config = self.home / ".config/local-dictate/asr.toml"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("readlink", "dirname"):
            (self.tools / name).symlink_to(shutil.which(name))
        self.log = self.root / "forbidden-action"
        for name in ("systemctl", "parec", "wtype", "wl-copy", "notify-send", "curl", "pip"):
            target = self.tools / name
            target.write_text('#!/bin/sh\nprintf called >> "$ACTION_LOG"\nexit 99\n')
            target.chmod(0o700)
        self.environment = {"HOME": str(self.home), "PATH": str(self.tools),
                            "LOCAL_DICTATE_PYTHON": sys.executable, "ACTION_LOG": str(self.log)}

    def run_setup(self, script=None):
        result = subprocess.run(["/bin/bash", str(script or self.script)], cwd="/tmp",
                                env=self.environment, capture_output=True, text=True, timeout=10)
        self.assertFalse(self.log.exists(), "Installer performed a forbidden action")
        return result

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_install_defaults_and_repeat_preserve_permissions(self):
        self.assert_success(self.run_setup())
        self.assertEqual(self.destination.readlink(), self.source / "bin/local-dictate-model")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn('active_model = "parakeet-v3-int8"', self.config.read_text())
        self.config.write_text("# user edit\n")
        self.config.chmod(0o640)
        inode = self.config.stat().st_ino
        self.assert_success(self.run_setup())
        self.assertEqual(self.config.read_text(), "# user edit\n")
        self.assertEqual(self.config.stat().st_ino, inode)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.home / ".config/systemd").exists())
        self.assertFalse((self.home / ".local/state").exists())

    def test_preserves_dangling_config_symlink(self):
        self.config.parent.mkdir(parents=True)
        target = self.root / "missing"
        self.config.symlink_to(target)
        self.assert_success(self.run_setup())
        self.assertEqual(self.config.readlink(), target)
        self.assertFalse(target.exists())

    def test_command_conflicts_are_preserved_before_config_creation(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text("existing command")
        self.assertNotEqual(self.run_setup().returncode, 0)
        self.assertEqual(self.destination.read_text(), "existing command")
        self.assertFalse(self.config.exists())
        self.destination.unlink()
        self.destination.symlink_to(self.root / "missing-command")
        self.assertNotEqual(self.run_setup().returncode, 0)
        self.assertTrue(self.destination.is_symlink())
        self.assertFalse(self.config.exists())

    def test_symlink_install_directory_is_refused(self):
        other = self.root / "other"
        other.mkdir()
        (self.home / ".local").symlink_to(other)
        self.assertNotEqual(self.run_setup().returncode, 0)
        self.assertEqual(list(other.iterdir()), [])
        self.assertFalse(self.config.exists())

    def test_installer_alias_and_xdg_config_home(self):
        alias = self.root / "installer alias"
        alias.symlink_to(self.script)
        self.environment["XDG_CONFIG_HOME"] = str(self.home / "config elsewhere")
        self.assert_success(self.run_setup(alias))
        self.assertTrue((self.home / "config elsewhere/local-dictate/asr.toml").exists())
        self.assertFalse(self.config.exists())

    def test_empty_xdg_variables_use_home_defaults(self):
        self.environment["XDG_CONFIG_HOME"] = ""
        self.environment["XDG_STATE_HOME"] = ""
        self.assert_success(self.run_setup())
        self.assertTrue(self.config.exists())


if __name__ == "__main__":
    unittest.main()
