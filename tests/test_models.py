"""Model selection tests use private temporary homes and mocked services only."""

import contextlib
import dataclasses
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

import model_manager as models


class FakeService:
    def __init__(self, argv, running=True):
        self.argv = argv if running else None
        self.running = running
        self.calls = []
        self.health_failures = 0
        self.restart_failures = 0
        self.interrupt = False

    def inspect(self, timeout=10):
        return {"running": self.running, "argv": self.argv, "pid": 321 if self.running else 0}

    def command(self, *args):
        self.calls.append(args)
        if args[0] == "restart":
            self.running = True
            if self.interrupt:
                self.interrupt = False
                raise KeyboardInterrupt
            if self.restart_failures:
                self.restart_failures -= 1
                raise models.ModelError("mock restart failed")
        if args[0] == "stop":
            self.running = False
            self.argv = None

    def health(self, argv, timeout):
        self.calls.append(("health", tuple(argv)))
        if self.health_failures:
            self.health_failures -= 1
            raise models.ModelError("mock health failed")
        self.argv = argv


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ModelsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dictate-models-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_RUNTIME_DIR": str(self.runtime)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = models.Paths.from_environment()
        models.directory(self.paths.config.parent, create=True)
        self.binary = self.root / 'server with spaces % $HOME'
        self.binary.write_text("#!/bin/sh\nexit 99\n")
        self.binary.chmod(0o700)
        self.weights = self.root / 'weights % with spaces "quote" \\slash'
        self.weights.mkdir(mode=0o700)
        self.filenames = ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")
        for filename in self.filenames:
            (self.weights / filename).write_bytes(b"fixture, not a model")
        self.config_text = (f'active_model = "v3"\n[server]\nbinary = {json.dumps(str(self.binary))}\n'
                            '[models.v3]\nvalidated = true\n'
                            f'directory = {json.dumps(str(self.weights))}\n'
                            + "".join(f'{key} = {json.dumps(value)}\n' for key, value in zip(models.FILES, self.filenames))
                            + '[models.ultra]\nvalidated = false\ndirectory = "/not/staged"\n'
                            + "".join(f'{key} = "set_{key}"\n' for key in models.FILES))
        self.write_config(self.config_text)
        self.settings = models.load_config(self.paths.config)
        self.baseline = [str(self.binary), "--original-baseline"]
        self.service = FakeService(self.baseline)
        flags = [arg.split("=", 1)[0] for arg in self.settings.argv("v3")[1:]] + ["--feat-dim", "--model-type"]
        self.help_text = "\n".join(f"  {flag:25s} : fixture" for flag in flags).encode()
        help_patch = mock.patch.object(models.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, self.help_text, b""))
        self.run = help_patch.start()
        self.addCleanup(help_patch.stop)

    def write_config(self, text):
        self.paths.config.write_text(text)
        self.paths.config.chmod(0o600)

    def assert_no_activation(self):
        self.assertEqual(self.service.calls, [])
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.dropin.exists())

    def put_dropin(self, content):
        models.directory(self.paths.dropin.parent, create=True)
        self.paths.dropin.write_bytes(content)
        self.paths.dropin.chmod(0o600)

    def test_initial_profile_preserves_concurrency_and_uses_cpu_autodetect(self):
        argv = models.validate(self.settings, "v3")
        for flag in ("--num-work-threads=4", "--num-threads=8", "--provider=cpu", "--port=6006",
                     "--log-file=/dev/null", "--print-args=false"):
            self.assertIn(flag, argv)
        self.assertFalse(any(arg.startswith("--model-type") for arg in argv))
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0], [str(self.binary), "--help"])
        self.assert_no_activation()

    def test_invalid_config_and_bounded_settings(self):
        for bad in ('provider = "cuda"', 'port = 7000', 'port = 6006.0',
                    'threads = true', 'threads = 33', 'health_timeout = 181',
                    'io_threads = 0', 'work_threads = 17', 'max_batch_size = 33',
                    'extra = true'):
            with self.subTest(bad=bad):
                self.write_config(self.config_text.replace('[server]\n', f'[server]\n{bad}\n'))
                with self.assertRaises(models.ModelError):
                    models.load_config(self.paths.config)
        self.assert_no_activation()

    def test_invalid_effective_values(self):
        for before, after in (('binary = ', 'wrong_binary = '),
                              ('validated = true', 'validated = "true"'),
                              ('encoder.int8.onnx', '../encoder.onnx'),
                              ('active_model = "v3"', 'active_model = "absent"')):
            with self.subTest(after=after):
                self.write_config(self.config_text.replace(before, after, 1))
                with self.assertRaises(models.ModelError):
                    models.load_config(self.paths.config)

    def test_optional_native_profile_overrides_are_bounded_and_checked(self):
        text = self.config_text.replace('[models.v3]\n',
            '[models.v3]\nfeat_dim = 128\nmodel_type = "nemo_transducer"\n')
        self.write_config(text)
        configured = models.load_config(self.paths.config)
        argv = models.validate(configured, "v3")
        self.assertIn("--feat-dim=128", argv)
        self.assertIn("--model-type=nemo_transducer", argv)
        self.run.return_value.stdout = self.help_text.replace(b"--feat-dim", b"--not-feat")
        with self.assertRaisesRegex(models.ModelError, "required flags"):
            models.validate(configured, "v3")
        for key, value in (("feat_dim", "0"), ("feat_dim", "257"), ("feat_dim", "true"),
                           ("model_type", '"whisper"'), ("model_type", '"nemo_transducer\\nextra"')):
            self.write_config(self.config_text.replace('[models.v3]\n', f'[models.v3]\n{key} = {value}\n'))
            with self.assertRaises(models.ModelError):
                models.load_config(self.paths.config)
        self.assert_no_activation()

    def test_unstaged_ultra_and_missing_or_empty_weights_never_restart(self):
        with self.assertRaisesRegex(models.ModelError, "not externally validated"):
            models.apply(self.paths, dataclasses.replace(self.settings, active_model="ultra"), self.service)
        (self.weights / self.filenames[0]).unlink()
        with self.assertRaisesRegex(models.ModelError, "encoder"):
            models.apply(self.paths, self.settings, self.service)
        (self.weights / self.filenames[0]).write_bytes(b"")
        with self.assertRaisesRegex(models.ModelError, "encoder"):
            models.apply(self.paths, self.settings, self.service)
        self.assert_no_activation()
        self.run.assert_not_called()

    def test_invalid_binary_and_unsupported_help_flags(self):
        with self.assertRaisesRegex(models.ModelError, "quotes or backslashes"):
            models.validate(dataclasses.replace(self.settings, binary=Path('/invalid"binary')), "v3")
        self.binary.chmod(0o600)
        with self.assertRaisesRegex(models.ModelError, "executable"):
            models.apply(self.paths, self.settings, self.service)
        self.binary.chmod(0o700)
        self.run.return_value.stdout = b"  --port : only one flag"
        with self.assertRaisesRegex(models.ModelError, "required flags"):
            models.apply(self.paths, self.settings, self.service)
        self.assert_no_activation()

    def test_help_timeout_is_bounded_and_does_not_echo_binary_output(self):
        self.run.side_effect = subprocess.TimeoutExpired(["private text"], 5)
        with self.assertRaisesRegex(models.ModelError, "timed out") as error:
            models.apply(self.paths, self.settings, self.service)
        self.assertNotIn("private text", str(error.exception))
        self.assert_no_activation()

    def test_systemd_quoting_percent_spaces_dollars_and_control_rejection(self):
        rendered = models.render("v3", self.settings.argv("v3")).decode()
        self.assertIn('ExecStart=\nExecStart=":', rendered)
        self.assertIn("spaces %% $HOME", rendered)
        self.assertIn('\\"quote\\"', rendered)
        self.assertIn("\\\\slash", rendered)
        self.assertNotIn("$$HOME", rendered)
        for value in ("a\nb", "a\rb", "a\0b"):
            with self.assertRaises(models.ModelError):
                models.systemd_arg(value)

    def test_check_and_list_are_read_only_and_show_desired_vs_actual(self):
        self.service.argv = self.settings.argv("v3")
        with mock.patch.object(models, "Service", return_value=self.service), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(models.main(["check"]), 0)
            self.assertEqual(models.main(["list"]), 0)
        self.assertIn("Desired: v3", output.getvalue())
        self.assertIn("Actual: v3", output.getvalue())
        self.assertIn("ultra: not externally validated", output.getvalue())
        self.assert_no_activation()

    def test_success_idempotence_and_manual_rollback_preserve_unrelated_files(self):
        models.directory(self.paths.dropin.parent, create=True)
        foreign = self.paths.dropin.parent / "99-unrelated.conf"
        foreign.write_bytes(b"[Service]\nEnvironment=KEEP=1\n")
        original = foreign.read_bytes()
        self.assertIn("Applied v3", models.apply(self.paths, self.settings, self.service))
        recovery = self.paths.state / "recovery.json"
        snapshot = recovery.read_bytes()
        self.assertEqual(models.read_snapshot(recovery)["argv"], self.baseline)
        calls = list(self.service.calls)
        self.assertIn("Already applied", models.apply(self.paths, self.settings, self.service))
        self.assertEqual(recovery.read_bytes(), snapshot)
        self.assertEqual(self.service.calls[:-1], calls)
        self.assertEqual(self.service.calls[-1][0], "health")
        models.rollback(self.paths, self.service)
        self.assertFalse(self.paths.dropin.exists())
        self.assertTrue(self.service.running)
        self.assertEqual(self.service.argv, self.baseline)
        self.assertEqual(foreign.read_bytes(), original)
        self.assertEqual(self.paths.config.read_text(), self.config_text)
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o600)
        models.rollback(self.paths, self.service)
        self.assertFalse(self.paths.dropin.exists())

    def test_failure_restores_exact_previous_managed_dropin_and_running_state(self):
        previous = (models.MARKER + "# Preserve spacing and comments\n[Service]\nExecStart=\n").encode()
        self.put_dropin(previous)
        self.service.health_failures = 1
        with self.assertRaisesRegex(models.ModelError, "previous drop-in and running state restored"):
            models.apply(self.paths, self.settings, self.service)
        self.assertEqual(self.paths.dropin.read_bytes(), previous)
        self.assertTrue(self.service.running)
        self.assertEqual(self.service.argv, self.baseline)
        self.assertFalse((self.paths.state / "pending.json").exists())
        self.assertFalse((self.paths.state / "recovery.json").exists())

    def test_failed_apply_preserves_last_useful_recovery(self):
        models.apply(self.paths, self.settings, self.service)
        recovery = self.paths.state / "recovery.json"
        previous = recovery.read_bytes()
        self.service.health_failures = 1
        changed = dataclasses.replace(self.settings, threads=4)
        with self.assertRaises(models.ModelError):
            models.apply(self.paths, changed, self.service)
        self.assertEqual(recovery.read_bytes(), previous)
        models.rollback(self.paths, self.service)
        self.assertEqual(self.service.argv, self.baseline)

    def test_promotion_faults_preserve_older_recovery_even_if_service_rollback_fails(self):
        replace = os.replace
        sync = models.sync_directory
        for fault in ("promotion", "fsync", "signal"):
            for rollback_fails in (False, True):
                with self.subTest(fault=fault, rollback_fails=rollback_fails):
                    root = self.root / f"{fault}-{rollback_fails}"
                    paths = dataclasses.replace(self.paths, state=root / "state",
                                                dropin=root / "service" / models.DROPIN)
                    pending = paths.state / "pending.json"
                    recovery = paths.state / "recovery.json"
                    service = FakeService(self.baseline)
                    models.apply(paths, self.settings, service)
                    previous_recovery = recovery.read_bytes()
                    previous_dropin = paths.dropin.read_bytes()
                    failed = False

                    def inject_fault():
                        nonlocal failed
                        failed = True
                        service.health_failures = int(rollback_fails)
                        if fault == "signal":
                            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                        raise OSError("mock promotion failure")

                    def promote(source, destination):
                        if Path(source) == pending and Path(destination) == recovery:
                            if fault == "promotion":
                                inject_fault()
                            replace(source, destination)
                            if fault == "signal":
                                inject_fault()
                        else:
                            replace(source, destination)

                    def sync_commit(path):
                        if (fault == "fsync" and path == paths.state
                                and recovery.exists() and not pending.exists() and not failed):
                            inject_fault()
                        sync(path)

                    message = ("automatic rollback also failed: mock health failed" if rollback_fails
                               else "previous drop-in and running state restored")
                    with mock.patch.object(models.os, "replace", side_effect=promote), \
                            mock.patch.object(models, "sync_directory", side_effect=sync_commit):
                        with self.assertRaisesRegex(models.ModelError, message) as error:
                            models.apply(paths, dataclasses.replace(self.settings, threads=4), service)
                    self.assertTrue(failed)
                    self.assertIn("interrupted by signal" if fault == "signal"
                                  else "Activation failed or interrupted", str(error.exception))
                    self.assertEqual(recovery.read_bytes(), previous_recovery)
                    self.assertEqual(paths.dropin.read_bytes(), previous_dropin)
                    self.assertEqual(pending.exists(), rollback_fails)
                    if rollback_fails:
                        snapshot = models.read_snapshot(pending)
                        self.assertEqual(snapshot["dropin"], previous_dropin.decode())
                        self.assertEqual(snapshot["argv"], self.settings.argv("v3"))
                        with self.assertRaisesRegex(models.ModelError, "Unfinished activation"):
                            models.apply(paths, self.settings, service)
                        models.rollback(paths, service)
                        self.assertFalse(pending.exists())
                        self.assertEqual(recovery.read_bytes(), previous_recovery)
                    self.assertEqual(service.argv, self.settings.argv("v3"))
                    models.rollback(paths, service)
                    self.assertFalse(paths.dropin.exists())
                    self.assertEqual(service.argv, self.baseline)
                    models.rollback(paths, service)
                    self.assertEqual(service.argv, self.baseline)

    def test_failed_activation_restores_initial_stopped_state(self):
        self.service.running = False
        self.service.argv = None
        self.service.health_failures = 1
        with self.assertRaises(models.ModelError):
            models.apply(self.paths, self.settings, self.service)
        self.assertFalse(self.service.running)
        self.assertFalse(self.paths.dropin.exists())
        self.assertIn(("stop", models.UNIT), self.service.calls)

    def test_restart_error_and_interrupt_both_roll_back(self):
        for kind in ("restart", "interrupt"):
            with self.subTest(kind=kind):
                self.service = FakeService(self.baseline)
                self.service.restart_failures = int(kind == "restart")
                self.service.interrupt = kind == "interrupt"
                with self.assertRaisesRegex(models.ModelError, "restored"):
                    models.apply(self.paths, self.settings, self.service)
                self.assertFalse(self.paths.dropin.exists())
                self.assertEqual(self.service.argv, self.baseline)

    def test_dropin_write_and_reload_failures_both_restore_before_state(self):
        replace = os.replace

        def fail_dropin_once(source, destination):
            if Path(destination) == self.paths.dropin:
                raise OSError("mock disk failure")
            return replace(source, destination)

        with mock.patch.object(models.os, "replace", side_effect=fail_dropin_once):
            with self.assertRaisesRegex(models.ModelError, "restored"):
                models.apply(self.paths, self.settings, self.service)
        self.assertFalse(self.paths.dropin.exists())
        self.assertFalse((self.paths.state / "pending.json").exists())
        command = self.service.command
        failed = False

        def fail_reload_once(*args):
            nonlocal failed
            if args[0] == "daemon-reload" and not failed:
                failed = True
                raise models.ModelError("mock reload failed")
            command(*args)

        with mock.patch.object(self.service, "command", side_effect=fail_reload_once):
            with self.assertRaisesRegex(models.ModelError, "mock reload failed; previous"):
                models.apply(self.paths, self.settings, self.service)
        self.assertFalse(self.paths.dropin.exists())
        self.assertEqual(self.service.argv, self.baseline)

    def test_failed_rollback_surfaces_both_errors_and_durable_recovery(self):
        self.service.health_failures = 2
        with self.assertRaisesRegex(models.ModelError, "mock health failed; automatic rollback also failed"):
            models.apply(self.paths, self.settings, self.service)
        pending = self.paths.state / "pending.json"
        saved = pending.read_bytes()
        self.assertEqual(models.read_snapshot(pending)["argv"], self.baseline)
        with self.assertRaisesRegex(models.ModelError, "Unfinished activation"):
            models.apply(self.paths, self.settings, self.service)
        self.assertEqual(pending.read_bytes(), saved)
        # Recovery is independent of a now-broken or missing TOML configuration.
        self.paths.config.unlink()
        models.rollback(self.paths, self.service)
        self.assertFalse(pending.exists())
        self.assertEqual(self.service.argv, self.baseline)

    def test_exclusive_apply_refuses_second_operation(self):
        with models.exclusive(self.paths):
            with self.assertRaisesRegex(models.ModelError, "busy"):
                models.apply(self.paths, self.settings, self.service)
        self.assertEqual(self.service.calls, [])
        self.assertFalse(self.paths.dropin.exists())

    def test_foreign_file_and_symlink_are_preserved(self):
        self.put_dropin(b"[Service]\n# foreign file\n")
        with self.assertRaisesRegex(models.ModelError, "foreign file"):
            models.apply(self.paths, self.settings, self.service)
        self.assertEqual(self.paths.dropin.read_bytes(), b"[Service]\n# foreign file\n")
        self.paths.dropin.unlink()
        target = self.root / "untouched"
        target.write_text("keep")
        self.paths.dropin.symlink_to(target)
        with self.assertRaises(models.ModelError):
            models.apply(self.paths, self.settings, self.service)
        self.assertTrue(self.paths.dropin.is_symlink())
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(self.service.calls, [])

    def test_unsafe_state_symlink_hardlink_and_permissions(self):
        models.directory(self.paths.state.parent, create=True)
        self.paths.state.symlink_to(self.root)
        with self.assertRaises(models.ModelError):
            models.apply(self.paths, self.settings, self.service)
        self.paths.state.unlink()
        self.paths.state.mkdir(mode=0o755)
        with self.assertRaises(models.ModelError):
            models.apply(self.paths, self.settings, self.service)
        self.paths.state.chmod(0o700)
        lock = self.paths.state / "apply.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        os.link(lock, self.root / "second-lock-link")
        with self.assertRaises(models.ModelError):
            models.apply(self.paths, self.settings, self.service)
        self.assertEqual(self.service.calls, [])

    def test_owner_lock_busy_guard_uses_read_only_control_status(self):
        runtime = self.runtime / "local-dictate"
        runtime.mkdir(mode=0o700)
        lock = runtime / "owner.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        token = "12345678-1234-1234-1234-123456789012"
        state = runtime / "state.json"
        state.write_text(json.dumps({"session": token, "state": "transcribing"}))
        state.chmod(0o600)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(runtime / "control.sock"))
        (runtime / "control.sock").chmod(0o600)
        import local_dictate

        def inspect(runtime_object, command):
            self.assertEqual(command, "status")
            self.assertEqual(runtime_object.token(), token)
            return {"state": "transcribing"}

        with mock.patch.object(local_dictate, "send_control", side_effect=inspect) as control:
            with self.assertRaisesRegex(models.ModelError, "session is active"):
                models.apply(self.paths, self.settings, self.service)
            control.assert_called_once()
        self.assertEqual(json.loads(state.read_text())["state"], "transcribing")
        self.assert_no_activation()

    def test_idle_guard_creates_nothing_and_fails_closed_on_missing_runtime(self):
        models.client_idle()
        self.assertEqual(list(self.runtime.iterdir()), [])
        os.environ.pop("XDG_RUNTIME_DIR")
        with self.assertRaises(models.ModelError):
            models.apply(self.paths, self.settings, self.service)
        self.assert_no_activation()

    def test_busy_unresponsive_client_is_not_treated_as_idle(self):
        runtime = self.runtime / "local-dictate"
        runtime.mkdir(mode=0o700)
        fd = os.open(runtime / "owner.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(models.ModelError, "busy"):
            models.apply(self.paths, self.settings, self.service)
        self.assert_no_activation()

    def test_config_symlink_and_writable_config_are_rejected_without_changes(self):
        self.paths.config.chmod(0o666)
        with self.assertRaises(models.ModelError):
            models.load_config(self.paths.config)
        self.paths.config.unlink()
        original = self.root / "original.toml"
        original.write_text(self.config_text)
        original.chmod(0o600)
        self.paths.config.symlink_to(original)
        with self.assertRaises(models.ModelError):
            models.load_config(self.paths.config)
        self.assertEqual(original.read_text(), self.config_text)
        self.assert_no_activation()

    def test_signal_handler_is_restored(self):
        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaisesRegex(models.ModelError, "signal"):
            with models.interruption_guard():
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


class ServiceTests(unittest.TestCase):
    def test_health_requires_main_pid_to_own_the_ipv4_probe_endpoint(self):
        service = models.Service()
        state = {"running": True, "argv": ["expected"], "pid": 7}

        def table(listeners):
            return "header\n" + "".join(
                f"0: {address}:1776 00000000:0000 0A 00000000:00000000 "
                f"00:00000000 00000000 1000 0 {inode}\n" for address, inode in listeners)

        for name, ipv4, ipv6, healthy in (
                ("wrong loopback plus another process", [("0200007F", 41), ("0100007F", 42)], [], False),
                ("IPv6 loopback only", [], [("00000000000000000000000001000000", 41)], False),
                ("IPv6 wildcard only", [], [("00000000000000000000000000000000", 41)], False),
                ("IPv4 loopback", [("0100007F", 41)], [], True),
                ("IPv4 wildcard", [("00000000", 41)], [], True),
                ("endpoint owned only by another process", [("0100007F", 42)], [], False)):
            with self.subTest(listener=name):
                clock = FakeClock()
                tables = {"/proc/net/tcp": table(ipv4), "/proc/net/tcp6": table(ipv6)}
                with mock.patch("builtins.open", side_effect=lambda path, **kwargs: io.StringIO(tables[str(path)])), \
                        mock.patch.object(Path, "iterdir", return_value=[Path("/proc/7/fd/3")]), \
                        mock.patch.object(models.os, "readlink", return_value="socket:[41]"), \
                        mock.patch.object(models.time, "monotonic", side_effect=clock.monotonic), \
                        mock.patch.object(models.time, "sleep", side_effect=clock.advance), \
                        mock.patch.object(service, "inspect", return_value=state), \
                        mock.patch.object(service, "probe") as probe:
                    if healthy:
                        service.health(["expected"], 5)
                        probe.assert_called_once()
                    else:
                        with self.assertRaises(models.ModelError):
                            service.health(["expected"], 5)
                        probe.assert_not_called()

    def test_health_rejects_expired_budget_after_each_phase(self):
        for phase in ("inspect", "listener", "probe", "reinspect"):
            with self.subTest(phase=phase):
                service = models.Service()
                clock = FakeClock()
                state = {"running": True, "argv": ["expected"], "pid": 7}
                inspections = 0

                def inspect(timeout):
                    nonlocal inspections
                    inspections += 1
                    if (phase == "inspect" and inspections == 1
                            or phase == "reinspect" and inspections == 2):
                        clock.advance(5.02)
                    return state

                def listener(pid):
                    if phase == "listener":
                        clock.advance(5)
                    return True

                def probe(timeout):
                    if phase == "probe":
                        clock.advance(5)

                with mock.patch.object(models.time, "monotonic", side_effect=clock.monotonic), \
                        mock.patch.object(models.time, "sleep", side_effect=clock.advance), \
                        mock.patch.object(service, "inspect", side_effect=inspect) as inspect_call, \
                        mock.patch.object(service, "owns_listener", side_effect=listener) as listener_call, \
                        mock.patch.object(service, "probe", side_effect=probe) as probe_call:
                    with self.assertRaises(models.ModelError):
                        service.health(["expected"], 5)
                self.assertEqual(inspect_call.call_count, 2 if phase == "reinspect" else 1)
                self.assertEqual(listener_call.call_count, 0 if phase == "inspect" else 1)
                self.assertEqual(probe_call.call_count, int(phase in ("probe", "reinspect")))

    def test_health_uses_positive_remaining_budget_without_a_timeout_floor(self):
        service = models.Service()
        clock = FakeClock()
        state = {"running": True, "argv": ["expected"], "pid": 7}
        inspections = 0

        def inspect(timeout):
            nonlocal inspections
            inspections += 1
            if inspections == 1:
                clock.advance(4.995)
            return state

        with mock.patch.object(models.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(models.time, "sleep", side_effect=clock.advance), \
                mock.patch.object(service, "inspect", side_effect=inspect) as inspect_call, \
                mock.patch.object(service, "owns_listener", side_effect=lambda pid: clock.advance(.002) or True), \
                mock.patch.object(service, "probe", side_effect=lambda timeout: clock.advance(.001)) as probe:
            service.health(["expected"], 5)
        self.assertEqual(inspect_call.call_args_list[0].kwargs["timeout"], 5)
        self.assertAlmostEqual(probe.call_args.args[0], .003)
        self.assertAlmostEqual(inspect_call.call_args_list[1].kwargs["timeout"], .002)
        self.assertLess(clock.now, 5)

    def test_health_does_not_inspect_without_positive_budget(self):
        service = models.Service()
        clock = FakeClock()
        with mock.patch.object(models.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(service, "inspect") as inspect, \
                mock.patch.object(service, "owns_listener") as listener, \
                mock.patch.object(service, "probe") as probe:
            for timeout in (0, -1):
                with self.assertRaises(models.ModelError):
                    service.health(["expected"], timeout)
            inspect.assert_not_called()
            listener.assert_not_called()
            probe.assert_not_called()

    def test_health_requires_expected_service_command_and_owned_listener(self):
        service = models.Service()
        state = {"running": True, "argv": ["expected"], "pid": 7}
        with mock.patch.object(service, "inspect", return_value=state), \
                mock.patch.object(service, "owns_listener", return_value=True), \
                mock.patch.object(service, "probe") as probe:
            service.health(["expected"], .02)
            probe.assert_called_once()
            state["argv"] = ["different"]
            with self.assertRaises(models.ModelError):
                service.health(["expected"], .02)
            probe.assert_called_once()
            state["argv"] = ["expected"]
            with mock.patch.object(service, "owns_listener", return_value=False):
                with self.assertRaises(models.ModelError):
                    service.health(["expected"], .02)
            probe.assert_called_once()

    def test_subprocess_timeout_discards_private_output(self):
        service = models.Service()
        with mock.patch.object(models.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                ["systemctl"], 10, output=b"private data")) as run:
            with self.assertRaises(models.ModelError) as error:
                service.command("restart", models.UNIT)
        self.assertNotIn("private data", str(error.exception))
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_probe_reuses_proxy_disabled_loopback_client_without_audio(self):
        import local_dictate
        with mock.patch.object(local_dictate, "probe_server", new_callable=mock.AsyncMock) as probe:
            models.Service().probe(.1)
        self.assertEqual(probe.call_args.args[0].server_url, "ws://127.0.0.1:6006")


if __name__ == "__main__":
    unittest.main()
