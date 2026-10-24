"""Installer checks with a temporary HOME and nonfunctional desktop tool stubs."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "scripts/install-client.sh"


class InstallClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="local-dictate-setup-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.checkout = self.root / "checkout with spaces"
        (self.checkout / "scripts").mkdir(parents=True)
        (self.checkout / "bin").mkdir()
        self.script = self.checkout / "scripts/install-client.sh"
        shutil.copy2(INSTALLER, self.script)
        self.launcher = self.checkout / "bin/local-dictate"
        self.launcher.write_text("#!/bin/sh\nexit 99\n")
        self.launcher.chmod(0o755)
        (self.checkout / "config.example.toml").write_text("# fixture config\n")
        self.mockbin = self.root / "commands"
        self.mockbin.mkdir()
        for name in ("readlink", "dirname", "stat", "id", "mkdir", "ln", "cat"):
            (self.mockbin / name).symlink_to(shutil.which(name))
        (self.mockbin / "python3").symlink_to(sys.executable)
        modules = self.root / "modules"
        package = modules / "websockets"
        (package / "asyncio").mkdir(parents=True)
        (package / "__init__.py").write_text('__version__ = "17.0.1"\n')
        (package / "asyncio/__init__.py").write_text("")
        (package / "asyncio/client.py").write_text("def connect(*, proxy=True):\n    pass\n")
        self.tool_log = self.root / "tool-called"
        for name in ("parec", "wtype", "wl-copy", "hyprctl", "notify-send"):
            tool = self.mockbin / name
            tool.write_text('#!/bin/sh\nprintf "called" >> "$SETUP_TOOL_LOG"\nexit 99\n')
            tool.chmod(0o755)
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.mockbin),
            "SETUP_TOOL_LOG": str(self.tool_log),
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(modules),
            "LOCAL_DICTATE_PYTHON": str(self.mockbin / "python3"),
        }
        self.destination = self.home / ".local/bin/local-dictate"
        self.config = self.home / ".config/local-dictate/config.toml"

    def run_setup(self, *args, script=None):
        result = subprocess.run(
            ["/bin/bash", str(script or self.script), *args],
            cwd="/tmp",
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertFalse(self.tool_log.exists(), "Installer executed a desktop tool")
        return result

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_install_resolves_source_and_creates_private_config(self):
        self.assert_success(self.run_setup())
        self.assertTrue(self.destination.is_symlink())
        self.assertEqual(self.destination.readlink(), self.launcher)
        self.assertEqual(self.config.read_text(), "# fixture config\n")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.parent.stat().st_mode & 0o777, 0o700)

    def test_reinstall_preserves_edited_config_and_command_link(self):
        self.assert_success(self.run_setup())
        self.config.write_text("# user's settings\n")
        link_inode = self.destination.lstat().st_ino
        config_inode = self.config.stat().st_ino
        self.assert_success(self.run_setup())
        self.assertEqual(self.config.read_text(), "# user's settings\n")
        self.assertEqual(self.config.stat().st_ino, config_inode)
        self.assertEqual(self.destination.lstat().st_ino, link_inode)

    def test_invocation_through_installer_symlink(self):
        alias = self.root / "installer alias"
        alias.symlink_to(self.script)
        self.assert_success(self.run_setup(script=alias))
        self.assertEqual(self.destination.readlink(), self.launcher)

    def test_existing_config_and_other_user_files_are_preserved(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text("# existing\n")
        hypr = self.home / ".config/hypr/bindings.lua"
        hypr.parent.mkdir()
        hypr.write_text("-- existing bindings\n")
        service = self.home / ".config/systemd/user/sherpa-onnx-asr.service"
        service.parent.mkdir(parents=True)
        service.write_text("# shared ASR\n")
        self.assert_success(self.run_setup())
        self.assertEqual(self.config.read_text(), "# existing\n")
        self.assertEqual(hypr.read_text(), "-- existing bindings\n")
        self.assertEqual(service.read_text(), "# shared ASR\n")

    def test_config_symlink_is_never_followed_for_writing(self):
        self.config.parent.mkdir(parents=True)
        target = self.root / "unrelated.toml"
        target.write_text("# unrelated\n")
        self.config.symlink_to(target)
        self.assert_success(self.run_setup())
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(target.read_text(), "# unrelated\n")

    def test_dangling_config_symlink_is_preserved(self):
        self.config.parent.mkdir(parents=True)
        target = self.root / "does-not-exist"
        self.config.symlink_to(target)
        self.assert_success(self.run_setup())
        self.assertTrue(self.config.is_symlink())
        self.assertFalse(target.exists())

    def test_unrelated_command_file_symlink_or_directory_is_refused(self):
        self.destination.parent.mkdir(parents=True)
        for kind in ("file", "symlink", "directory"):
            with self.subTest(kind=kind):
                if kind == "file":
                    self.destination.write_text("unrelated command")
                elif kind == "symlink":
                    self.destination.symlink_to(self.root / "other-command")
                else:
                    self.destination.mkdir()
                result = self.run_setup()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing to replace", result.stderr)
                self.assertFalse(self.config.exists())
                if kind == "file":
                    self.assertEqual(self.destination.read_text(), "unrelated command")
                elif kind == "symlink":
                    self.assertEqual(self.destination.readlink(), self.root / "other-command")
                if kind == "directory":
                    self.destination.rmdir()
                else:
                    self.destination.unlink()

    def test_failed_python_preflight_makes_no_install_files(self):
        python = self.mockbin / "python3"
        python.unlink()
        python.write_text("#!/bin/sh\nexit 1\n")
        python.chmod(0o755)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("websockets", result.stderr)
        self.assertFalse((self.home / ".local").exists())
        self.assertFalse((self.home / ".config").exists())

    def test_missing_external_tool_makes_no_install_files(self):
        (self.mockbin / "parec").unlink()
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing parec", result.stderr)
        self.assertFalse((self.home / ".local").exists())

    def test_default_interpreter_is_system_python(self):
        del self.env["LOCAL_DICTATE_PYTHON"]
        (self.mockbin / "python3").unlink()
        self.assert_success(self.run_setup())

    def test_checkout_venv_is_selected_and_failure_is_not_hidden(self):
        del self.env["LOCAL_DICTATE_PYTHON"]
        venv_python = self.checkout / ".venv/bin/python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\nexit 42\n")
        venv_python.chmod(0o755)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("websockets", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_explicit_interpreter_takes_precedence_over_venv(self):
        venv_python = self.checkout / ".venv/bin/python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\nexit 42\n")
        venv_python.chmod(0o755)
        self.assert_success(self.run_setup())

    def test_missing_explicit_interpreter_is_rejected(self):
        self.env["LOCAL_DICTATE_PYTHON"] = str(self.root / "missing-python")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing Python interpreter", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_websockets_without_proxy_support_is_rejected(self):
        module = Path(self.env["PYTHONPATH"]) / "websockets/asyncio/client.py"
        module.write_text("def connect():\n    pass\n")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("proxy=None support", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_missing_notifications_warns_without_running_any_tool(self):
        (self.mockbin / "notify-send").unlink()
        result = self.run_setup()
        self.assert_success(result)
        self.assertIn("notify-send", result.stderr)

    def test_xdg_config_home_is_used(self):
        xdg = self.root / "xdg config"
        self.env["XDG_CONFIG_HOME"] = str(xdg)
        self.assert_success(self.run_setup())
        self.assertTrue((xdg / "local-dictate/config.toml").is_file())
        self.assertFalse(self.config.exists())

    def test_empty_xdg_config_home_uses_home_default(self):
        self.env["XDG_CONFIG_HOME"] = ""
        self.assert_success(self.run_setup())
        self.assertTrue(self.config.is_file())

    def test_relative_xdg_config_home_is_rejected(self):
        self.env["XDG_CONFIG_HOME"] = "relative-config"
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absolute", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_missing_source_files_are_rejected(self):
        self.launcher.unlink()
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing executable", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_config_directory_is_refused(self):
        self.config.mkdir(parents=True)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Config path is a directory", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_unexpected_arguments_are_rejected(self):
        result = self.run_setup("--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
