#!/usr/bin/env python3
"""Explicit, reversible CPU model selection for the existing shared service."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import types
import uuid

UNIT = "sherpa-onnx-asr.service"
DROPIN = "60-local-dictate-model.conf"
MARKER = "# Managed by local-dictate-model v1\n"
LIMIT = 65536
FILES = ("encoder", "decoder", "joiner", "tokens")


class ModelError(Exception):
    """A diagnostic that contains no subprocess output or private audio data."""


def absolute(value: object, label: str) -> Path:
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ModelError(f"{label} must be a nonempty path without control characters")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ModelError(f"{label} must be absolute without parent traversal")
    return path


def directory(path: Path, *, create: bool = False, private: bool = False) -> None:
    """Reject symlink ancestors and writable foreign directories, without chmod."""
    absolute(str(path), "directory")
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if not create:
                raise ModelError(f"Missing directory: {item}") from None
            try:
                item.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = item.lstat()
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or (info.st_mode & 0o022 and not sticky_root)):
            raise ModelError(f"Unsafe directory ownership, permissions, or symlink: {item}")
        if item == path and (info.st_uid != os.getuid()
                             or (private and info.st_mode & 0o077)):
            raise ModelError(f"Directory must be owned by you with safe permissions: {item}")


def read_file(path: Path, *, private: bool = False, missing: bool = False) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if missing:
            return None
        raise ModelError(f"Missing file: {path}") from None
    except OSError:
        raise ModelError(f"Cannot safely read file: {path}") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & (0o077 if private else 0o022)
                or info.st_size > LIMIT):
            raise ModelError(f"Unsafe file ownership, size, or permissions: {path}")
        data = stream.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ModelError("File exceeds size limit")
        return data


def atomic_write(path: Path, data: bytes) -> None:
    directory(path.parent)
    read_file(path, private=True, missing=True)
    fd, name = tempfile.mkstemp(prefix=".model-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def keys(values: object, allowed: set[str], label: str) -> dict:
    if not isinstance(values, dict) or set(values) - allowed:
        raise ModelError(f"Invalid or unknown keys in {label}")
    return values


def bounded(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ModelError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


@dataclasses.dataclass(frozen=True)
class Profile:
    directory: Path
    filenames: tuple[str, ...]
    validated: bool
    feat_dim: int | None = None
    model_type: str | None = None

    def paths(self) -> list[Path]:
        return [self.directory / name for name in self.filenames]

    def problems(self) -> list[str]:
        problems = []
        if not self.validated:
            problems.append("not externally validated")
        for label, path in zip(FILES, self.paths()):
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or not info.st_size or not os.access(path, os.R_OK):
                    raise OSError
            except OSError:
                problems.append(f"missing or invalid {label}")
        return problems


@dataclasses.dataclass(frozen=True)
class Settings:
    active_model: str
    binary: Path
    profiles: dict[str, Profile]
    work_threads: int = 4
    threads: int = 8
    io_threads: int = 1
    max_batch_size: int = 5
    health_timeout: int = 60

    def argv(self, model: str) -> list[str]:
        profile = self.profiles[model]
        return [str(self.binary), "--port=6006", "--provider=cpu",
                f"--num-work-threads={self.work_threads}", f"--num-threads={self.threads}",
                f"--num-io-threads={self.io_threads}", f"--max-batch-size={self.max_batch_size}",
                *([f"--feat-dim={profile.feat_dim}"] if profile.feat_dim is not None else []),
                *([f"--model-type={profile.model_type}"] if profile.model_type is not None else []),
                *(f"--{key}={path}" for key, path in zip(FILES, profile.paths())),
                "--log-file=/dev/null", "--print-args=false"]


def load_config(path: Path) -> Settings:
    directory(path.parent)
    try:
        data = keys(tomllib.loads(read_file(path).decode("utf-8")),
                    {"active_model", "server", "models"}, "configuration")
        server = keys(data.get("server", {}), {"binary", "provider", "port", "work_threads",
                      "threads", "io_threads", "max_batch_size", "health_timeout"}, "server")
        if server.get("provider", "cpu") != "cpu" or server.get("port", 6006) != 6006:
            raise ModelError("Only provider=cpu and port=6006 are supported")
        if type(server.get("port", 6006)) is not int:
            raise ModelError("port must be the integer 6006")
        profiles = {}
        for name, raw in keys(data.get("models", {}), set(data.get("models", {})), "models").items():
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
                raise ModelError("Invalid model profile name")
            raw = keys(raw, {"directory", "validated", "feat_dim", "model_type", *FILES}, f"model {name}")
            names = []
            for key in FILES:
                filename = raw.get(key)
                if (not isinstance(filename, str) or not filename or len(filename) > 255
                        or filename in (".", "..") or "/" in filename
                        or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
                    raise ModelError(f"{name}.{key} must be one filename without control characters")
                names.append(filename)
            if len(set(names)) != 4 or type(raw.get("validated", False)) is not bool:
                raise ModelError("Profiles need four distinct files and a boolean validated field")
            feat_dim = bounded(raw["feat_dim"], 1, 256, f"{name}.feat_dim") if "feat_dim" in raw else None
            model_type = raw.get("model_type")
            if "model_type" in raw and model_type not in ("transducer", "nemo_transducer"):
                raise ModelError("model_type must be transducer or nemo_transducer; omit for autodetection")
            profiles[name] = Profile(absolute(raw.get("directory"), f"{name}.directory"),
                                     tuple(names), raw.get("validated", False), feat_dim, model_type)
        selected = data.get("active_model")
        if not isinstance(selected, str) or selected not in profiles:
            raise ModelError("active_model must name a configured profile")
        return Settings(selected, absolute(server.get("binary"), "server.binary"), profiles,
                        bounded(server.get("work_threads", 4), 1, 16, "work_threads"),
                        bounded(server.get("threads", 8), 1, 32, "threads"),
                        bounded(server.get("io_threads", 1), 1, 8, "io_threads"),
                        bounded(server.get("max_batch_size", 5), 1, 32, "max_batch_size"),
                        bounded(server.get("health_timeout", 60), 5, 180, "health_timeout"))
    except (UnicodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
        raise ModelError("Invalid ASR configuration TOML") from None


def validate(settings: Settings, model: str) -> list[str]:
    if model not in settings.profiles:
        raise ModelError("Unknown model profile")
    problems = settings.profiles[model].problems()
    if problems:
        raise ModelError(f"{model}: {', '.join(problems)}")
    if any(character in str(settings.binary) for character in ('"', "\\")):
        raise ModelError("Systemd executable paths cannot contain quotes or backslashes")
    try:
        info = settings.binary.lstat()
        if (not stat.S_ISREG(info.st_mode) or not os.access(settings.binary, os.X_OK)
                or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022):
            raise OSError
    except OSError:
        raise ModelError("Server binary must be a regular executable with safe ownership and permissions") from None
    argv = settings.argv(model)
    try:
        help_result = subprocess.run([str(settings.binary), "--help"], stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ModelError("Server --help failed or timed out") from None
    help_text = (help_result.stdout + help_result.stderr).decode("utf-8", errors="replace")
    if help_result.returncode not in (0, 1):
        raise ModelError("Server --help returned an unexpected status")
    flags = set(re.findall(r"(?m)^\s+(--[a-z0-9.-]+)\s+:", help_text))
    if any(arg.split("=", 1)[0] not in flags for arg in argv[1:]):
        raise ModelError("Server --help does not advertise all required flags")
    return argv


def systemd_arg(value: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ModelError("Systemd arguments cannot contain control characters")
    # Double quoting protects whitespace and backslashes. %% suppresses
    # specifiers. render() uses ':' to disable environment expansion entirely.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render(model: str, argv: list[str]) -> bytes:
    return (MARKER + f"# Profile: {model}\n[Service]\nExecStart=\nExecStart="
            + " ".join(systemd_arg(arg) for arg in [":" + argv[0], *argv[1:]]) + "\n").encode("utf-8")


@dataclasses.dataclass(frozen=True)
class Paths:
    config: Path
    dropin: Path
    state: Path

    @classmethod
    def from_environment(cls, config: str | None = None) -> Paths:
        home = absolute(os.environ.get("HOME"), "HOME")
        config_home = absolute(os.environ.get("XDG_CONFIG_HOME") or str(home / ".config"), "XDG_CONFIG_HOME")
        state_home = absolute(os.environ.get("XDG_STATE_HOME") or str(home / ".local/state"), "XDG_STATE_HOME")
        return cls(absolute(config, "config") if config else config_home / "local-dictate/asr.toml",
                   config_home / "systemd/user" / (UNIT + ".d") / DROPIN,
                   state_home / "local-dictate/model-manager")


@contextlib.contextmanager
def exclusive(paths: Paths):
    directory(paths.state, create=True, private=True)
    lock = paths.state / "apply.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ModelError("Unsafe model-manager lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ModelError("Another model operation is busy") from None
        yield
    finally:
        os.close(fd)


def managed_content(paths: Paths) -> bytes | None:
    if not paths.dropin.parent.exists() and not paths.dropin.parent.is_symlink():
        # Validate existing ancestors even when the final directory is absent.
        ancestor = paths.dropin.parent
        while not ancestor.exists() and not ancestor.is_symlink():
            ancestor = ancestor.parent
        directory(ancestor)
        return None
    directory(paths.dropin.parent)
    data = read_file(paths.dropin, private=True, missing=True)
    if data is not None and not data.startswith(MARKER.encode()):
        raise ModelError("Refusing foreign file at the managed drop-in path")
    return data


def client_idle() -> None:
    """Read existing locks/control state only. Never call the mutating status CLI."""
    root = absolute(os.environ.get("XDG_RUNTIME_DIR"), "XDG_RUNTIME_DIR")
    directory(root, private=True)
    runtime = root / "local-dictate"
    if not runtime.exists() and not runtime.is_symlink():
        return
    directory(runtime, private=True)
    try:
        fd = os.open(runtime / "owner.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if (runtime / "control.sock").exists() or (runtime / "control.sock").is_symlink():
            raise ModelError("Cannot verify local client is idle") from None
        return
    except OSError:
        raise ModelError("Cannot safely inspect local client lock") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ModelError("Unsafe local client lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        import local_dictate

        try:
            info = (runtime / "control.sock").lstat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise OSError
        except OSError:
            raise ModelError("Local client is busy or its control socket is unavailable") from None

        def token():
            try:
                state = json.loads(read_file(runtime / "state.json", private=True))
                return str(uuid.UUID(state["session"]))
            except (ValueError, KeyError, TypeError):
                raise ModelError("Cannot verify local client state") from None

        control = types.SimpleNamespace(socket_path=runtime / "control.sock", token=token)
        try:
            local_dictate.send_control(control, "status")
        except local_dictate.ClientError:
            raise ModelError("Local client is busy or not responding; stop it before switching") from None
        raise ModelError("Local client session is active; stop it before switching")
    finally:
        os.close(fd)


class Service:
    def command(self, *args: str, timeout: float = 10) -> str:
        try:
            result = subprocess.run(["systemctl", "--user", *args], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ModelError(f"systemctl {args[0]} failed or timed out") from None
        if result.returncode:
            raise ModelError(f"systemctl {args[0]} failed")
        return result.stdout.decode("utf-8", errors="replace")

    def inspect(self, timeout: float = 10) -> dict:
        output = self.command("show", UNIT, "--property=ActiveState,MainPID,LoadState", timeout=timeout)
        data = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if data.get("LoadState") != "loaded" or data.get("ActiveState") not in ("active", "inactive", "failed"):
            raise ModelError("Service is missing or in a transitional state; retry when settled")
        try:
            pid = int(data["MainPID"])
        except (KeyError, ValueError):
            raise ModelError("Cannot read service MainPID") from None
        running = data["ActiveState"] == "active"
        argv = self.process_argv(pid) if running and pid > 0 else None
        if running and not argv:
            raise ModelError("Active service has no inspectable main process")
        return {"running": running, "argv": argv, "pid": pid}

    def process_argv(self, pid: int) -> list[str]:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as stream:
                data = stream.read(LIMIT + 1)
            if len(data) > LIMIT or not data.endswith(b"\0"):
                raise ValueError
            return [os.fsdecode(arg) for arg in data[:-1].split(b"\0")]
        except (OSError, ValueError):
            raise ModelError("Cannot verify service process command") from None

    def owns_listener(self, pid: int) -> bool:
        try:
            inodes = set()
            # The probe uses IPv4 127.0.0.1. IPv6 tables cannot prove dual-stack
            # support, and other IPv4 addresses cannot serve that endpoint.
            with open("/proc/net/tcp", encoding="ascii") as stream:
                for line in stream.readlines()[1:]:
                    fields = line.split()
                    if (len(fields) > 9 and fields[3] == "0A"
                            and fields[1] in ("0100007F:1776", "00000000:1776")):
                        inodes.add("socket:[" + fields[9] + "]")
            return any(os.readlink(fd) in inodes for fd in Path(f"/proc/{pid}/fd").iterdir())
        except OSError:
            return False

    def probe(self, timeout: float) -> None:
        import local_dictate

        async def run():
            async with asyncio.timeout(timeout):
                await local_dictate.probe_server(local_dictate.Config(server_url="ws://127.0.0.1:6006"))

        try:
            asyncio.run(run())
        except (local_dictate.ClientError, TimeoutError):
            raise ModelError("Loopback WebSocket health check failed") from None

    def health(self, argv: list[str], timeout: float) -> None:
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise ModelError("Service health deadline expired")
            return budget

        while True:
            try:
                state = self.inspect(timeout=min(10, remaining()))
                if state["running"] and state["argv"] == argv:
                    remaining()
                    if self.owns_listener(state["pid"]):
                        self.probe(min(4, remaining()))
                        after = self.inspect(timeout=min(10, remaining()))
                        if after == state:
                            remaining()
                            return
            except ModelError:
                pass
            budget = deadline - time.monotonic()
            if budget <= 0:
                break
            time.sleep(min(.2, budget))
        raise ModelError("Service did not become healthy with the expected command and listener")


def read_snapshot(path: Path) -> dict:
    try:
        data = json.loads(read_file(path, private=True))
        if (set(data) != {"version", "dropin", "running", "argv", "timeout"}
                or data["version"] != 1 or type(data["running"]) is not bool
                or type(data["timeout"]) is not int or not 5 <= data["timeout"] <= 180
                or (data["dropin"] is not None and (not isinstance(data["dropin"], str)
                    or not data["dropin"].startswith(MARKER)))
                or (data["running"] and (not isinstance(data["argv"], list) or not data["argv"]
                    or any(not isinstance(arg, str) or "\0" in arg for arg in data["argv"])))):
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError):
        raise ModelError("Invalid private recovery snapshot") from None


@contextlib.contextmanager
def interruption_guard(*, recovering: bool = False):
    def interrupted(signum, frame):
        raise ModelError(f"Model activation interrupted by signal {signum}")

    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, signal.SIG_IGN if recovering else interrupted)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def restore(paths: Paths, snapshot: dict, service: Service) -> None:
    managed_content(paths)
    if snapshot["dropin"] is None:
        with contextlib.suppress(FileNotFoundError):
            paths.dropin.unlink()
            sync_directory(paths.dropin.parent)
    else:
        directory(paths.dropin.parent, create=True)
        atomic_write(paths.dropin, snapshot["dropin"].encode("utf-8"))
    service.command("daemon-reload")
    service.command("restart" if snapshot["running"] else "stop", UNIT)
    if snapshot["running"]:
        service.health(snapshot["argv"], snapshot["timeout"])
    elif service.inspect()["running"]:
        raise ModelError("Previous stopped service state was not restored")


def apply(paths: Paths, settings: Settings, service: Service) -> str:
    # Validation and --help inspection precede even lock/directory creation.
    argv = validate(settings, settings.active_model)
    wanted = render(settings.active_model, argv)
    client_idle()
    with exclusive(paths):
        pending = paths.state / "pending.json"
        recovery = paths.state / "recovery.json"
        if read_file(pending, private=True, missing=True) is not None:
            raise ModelError("Unfinished activation exists; run rollback before applying")
        previous_recovery = read_file(recovery, private=True, missing=True)
        old = managed_content(paths)
        state = service.inspect()
        if old == wanted and state["running"] and state["argv"] == argv:
            service.health(argv, min(5, settings.health_timeout))
            return "Already applied and healthy; recovery snapshot preserved."
        snapshot = {"version": 1, "dropin": old.decode("utf-8") if old is not None else None,
                    "running": state["running"], "argv": state["argv"], "timeout": settings.health_timeout}
        serialized = json.dumps(snapshot, ensure_ascii=True).encode("utf-8")
        if len(serialized) > LIMIT:
            raise ModelError("Recovery snapshot exceeds size limit")
        client_idle()
        with interruption_guard():
            atomic_write(pending, serialized)
            try:
                directory(paths.dropin.parent, create=True)
                atomic_write(paths.dropin, wanted)
                service.command("daemon-reload")
                service.command("restart", UNIT)
                service.health(argv, settings.health_timeout)
                # Promote only after successful activation. Caught promotion
                # failures reconstruct pending.json before attempting recovery.
                os.replace(pending, recovery)
                sync_directory(paths.state)
            except BaseException as error:
                message = str(error) if isinstance(error, ModelError) else "Activation failed or interrupted"
                try:
                    with interruption_guard(recovering=True):
                        # Promotion may have completed just before an interruption
                        # or fsync failure. Keep durable retry data during recovery.
                        if read_file(pending, private=True, missing=True) is None:
                            atomic_write(pending, serialized)
                        # Preserve the earlier recovery point before service
                        # recovery, which can fail independently of these files.
                        if read_file(recovery, private=True, missing=True) != previous_recovery:
                            if previous_recovery is None:
                                recovery.unlink()
                                sync_directory(paths.state)
                            else:
                                atomic_write(recovery, previous_recovery)
                        restore(paths, snapshot, service)
                        pending.unlink()
                        sync_directory(paths.state)
                except BaseException as rollback_error:
                    reason = str(rollback_error) if isinstance(rollback_error, ModelError) else "recovery operation failed"
                    raise ModelError(f"{message}; automatic rollback also failed: {reason}. Run rollback to retry recovery.") from None
                raise ModelError(f"{message}; previous drop-in and running state restored.") from None
    return f"Applied {settings.active_model}; service command and loopback health verified."


def rollback(paths: Paths, service: Service) -> str:
    client_idle()
    with exclusive(paths):
        pending = paths.state / "pending.json"
        target = pending if read_file(pending, private=True, missing=True) is not None else paths.state / "recovery.json"
        snapshot = read_snapshot(target)
        managed_content(paths)
        with interruption_guard(recovering=True):
            restore(paths, snapshot, service)
            if target == pending:
                pending.unlink()
                sync_directory(paths.state)
    return "Recovered the previous managed drop-in and running state; desired config is unchanged."


def install(source: Path) -> None:
    """Install only the manager link and first config. Existing config is preserved."""
    if sys.version_info < (3, 11):
        raise ModelError("Python 3.11+ is required")
    paths = Paths.from_environment()
    destination = absolute(os.environ.get("HOME"), "HOME") / ".local/bin/local-dictate-model"
    launcher = source / "bin/local-dictate-model"
    example = source / "asr.example.toml"
    if not launcher.is_file() or not os.access(launcher, os.X_OK) or not example.is_file():
        raise ModelError("Missing model manager launcher or config example")
    for parent in (destination.parent, paths.config.parent):
        ancestor = parent
        while not ancestor.exists() and not ancestor.is_symlink():
            ancestor = ancestor.parent
        directory(ancestor)
    if destination.exists() or destination.is_symlink():
        if (not destination.is_symlink() or destination.lstat().st_uid != os.getuid()
                or destination.resolve() != launcher.resolve()):
            raise ModelError("Refusing to replace an existing command")
    if paths.config.is_dir():
        raise ModelError("Config path is a directory")
    directory(destination.parent, create=True)
    directory(paths.config.parent, create=True)
    if not destination.is_symlink():
        destination.symlink_to(launcher)
    try:
        fd = os.open(paths.config, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        print(f"Preserved existing config: {paths.config}")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(example.read_bytes())
        print(f"Created config: {paths.config}")
    print(f"Manager link: {destination}")
    print("No models, services, desktop settings, or packages changed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="absolute ASR TOML path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    check = commands.add_parser("check", help="validate files and flags; preview without activation")
    check.add_argument("--model", help="check another profile without selecting it")
    commands.add_parser("apply", help="restart the shared service using active_model")
    commands.add_parser("rollback", help="recover the state before the last activation")
    args = parser.parse_args(argv)
    try:
        paths = Paths.from_environment(args.config)
        service = Service()
        if args.command == "rollback":
            print("Stop Pi dictation before rollback; Pi activity cannot be detected.", file=sys.stderr)
            print(rollback(paths, service))
            return 0
        settings = load_config(paths.config)
        if args.command == "list":
            print(f"Desired: {settings.active_model} (changes take effect only after apply)")
            try:
                state = service.inspect()
                actual = []
                for name, profile in settings.profiles.items():
                    argv = state["argv"] or []
                    if argv and argv[0] == str(settings.binary) and all(
                            f"--{key}={path}" in argv for key, path in zip(FILES, profile.paths())):
                        actual.append(name)
                print("Actual: " + (", ".join(actual) if actual else "unrecognized" if state["running"] else "stopped"))
            except ModelError:
                print("Actual: unavailable (service process could not be inspected)")
            for name, profile in settings.profiles.items():
                problems = profile.problems()
                print(f"{name}: " + (", ".join(problems) if problems else "available locally; check before apply"))
        elif args.command == "check":
            model = args.model or settings.active_model
            argv = validate(settings, model)
            managed_content(paths)
            print(f"Validated local files and server flags for {model}; no inference or service change.")
            print(render(model, argv).decode("utf-8"), end="")
        else:
            print("Stop Pi dictation before apply; Pi activity cannot be detected.", file=sys.stderr)
            print(apply(paths, settings, service))
        return 0
    except (ModelError, OSError, UnicodeError):
        error = sys.exception()
        message = str(error) if isinstance(error, ModelError) else "Filesystem operation failed; inspect paths and permissions"
        print("local-dictate-model: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
