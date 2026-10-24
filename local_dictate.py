#!/usr/bin/env python3
"""Short-lived, local-only desktop dictation. No transcript or audio files."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import dataclasses
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import unicodedata
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
import uuid
import wave

import word_rules

RATE = 16000
CHUNK_BYTES = 60000
MAX_RECORD_SECONDS = 299
CONTROL_TIMEOUT = 3.0
PROCESS_TIMEOUT = 5.0
# Retain handles when this module is embedded so detached children can be reaped
# on the next command. The CLI itself exits immediately after each command.
_owner_children: list[subprocess.Popen] = []


class ClientError(Exception):
    """An error message safe to show without including user content."""


def local_url(value: str) -> str:
    """Validate and pin localhost to a numeric address before connecting."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else 80
        if (parsed.scheme != "ws" or not host or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or any(ord(c) < 33 for c in value) or not 0 < port < 65536):
            raise ValueError
        if host.lower() == "localhost":
            addresses = {item[4][0] for item in socket.getaddrinfo(
                "localhost", port, type=socket.SOCK_STREAM)}
            if not addresses or not all(ipaddress.ip_address(a).is_loopback for a in addresses):
                raise ValueError
            host = "127.0.0.1" if "127.0.0.1" in addresses else sorted(addresses)[0]
        address = ipaddress.ip_address(host)
        if not address.is_loopback or "%" in host:
            raise ValueError
        hostport = f"[{host}]:{port}" if address.version == 6 else f"{host}:{port}"
        return urlunsplit(("ws", hostport, parsed.path or "/", parsed.query, ""))
    except (ValueError, TypeError, OSError):
        raise ClientError("server_url must be a loopback ws:// URL without credentials") from None


@dataclasses.dataclass(frozen=True)
class Config:
    server_url: str = "ws://127.0.0.1:6006"
    source: str | None = None
    max_record_seconds: float = 120
    recognition_timeout: float = 30
    output: str = "auto"
    notifications: bool = True
    insertion_delay_ms: int = 300
    # Internal session metadata, never a desktop TOML setting or rule content.
    words_path: str | None = dataclasses.field(default=None, repr=False)

    @classmethod
    def from_dict(cls, values: dict) -> Config:
        if set(values) - {f.name for f in dataclasses.fields(cls) if f.name != "words_path"}:
            raise ClientError("Unknown configuration key")
        config = cls(**values)
        if not isinstance(config.server_url, str):
            raise ClientError("server_url must be a string")
        for name, maximum in (("max_record_seconds", MAX_RECORD_SECONDS),
                              ("recognition_timeout", 600)):
            value = getattr(config, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 < value <= maximum):
                raise ClientError(f"{name} must be greater than zero and at most {maximum}")
        if (config.source is not None and (not isinstance(config.source, str)
                or not config.source or any(ord(c) < 32 for c in config.source))):
            raise ClientError("source must be a nonempty PulseAudio source name")
        if config.output not in ("auto", "clipboard"):
            raise ClientError("output must be auto or clipboard")
        if not isinstance(config.notifications, bool):
            raise ClientError("notifications must be true or false")
        if (type(config.insertion_delay_ms) is not int
                or not 0 <= config.insertion_delay_ms <= 5000):
            raise ClientError("insertion_delay_ms must be between 0 and 5000")
        return dataclasses.replace(config, server_url=local_url(config.server_url))


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as stream:
            config = Config.from_dict(tomllib.load(stream))
    except FileNotFoundError:
        config = Config.from_dict({})
    except (OSError, tomllib.TOMLDecodeError):
        raise ClientError("Cannot read configuration TOML") from None
    return dataclasses.replace(config, words_path=str(words_path(path)))


def words_path(config_path: Path) -> Path:
    return config_path.absolute().with_name("words.toml")


def load_words(config: Config) -> word_rules.Rules:
    return word_rules.load(Path(config.words_path) if config.words_path is not None else None)


def new_session_config(path: Path) -> Config:
    config = load_config(path)
    load_words(config)  # Report invalid rules before starting a detached owner.
    return config


def websocket_connect(config: Config):
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        raise ClientError("websockets>=15 is required; run the documented setup") from None

    class LocalConnect(connect):
        def process_redirect(self, exc):
            # Keep every connection at its validated URL, including after HTTP 302.
            return exc

    # Validation is repeated at the network boundary, including for internal callers.
    return LocalConnect(local_url(config.server_url), proxy=None, open_timeout=3,
                        close_timeout=1, ping_interval=None, compression=None,
                        max_size=1024 * 1024)


async def probe_server(config: Config) -> None:
    try:
        async with asyncio.timeout(4):
            async with websocket_connect(config):
                pass
    except ClientError:
        raise
    except Exception:
        raise ClientError("Local ASR server is unavailable") from None


async def transcribe_audio(audio: bytes, config: Config) -> str:
    if not audio:
        return ""
    if len(audio) % 4 or len(audio) > int(MAX_RECORD_SECONDS * RATE) * 4:
        raise ClientError("Audio must be bounded float32 mono PCM")
    try:
        async with asyncio.timeout(config.recognition_timeout):
            async with websocket_connect(config) as ws:
                await ws.send(struct.pack("<ii", RATE, len(audio)) + audio[:CHUNK_BYTES])
                for offset in range(CHUNK_BYTES, len(audio), CHUNK_BYTES):
                    await ws.send(audio[offset:offset + CHUNK_BYTES])
                    await asyncio.sleep(0)
                response = await ws.recv()
                if not isinstance(response, str):
                    raise ClientError("ASR returned a binary response")
                try:
                    result = json.loads(response)
                except (ValueError, TypeError):
                    raise ClientError("ASR returned invalid JSON") from None
                if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                    raise ClientError("ASR response has no text string")
                await ws.send("Done")
                return result["text"]
    except ClientError:
        raise
    except TimeoutError:
        raise ClientError("Recognition timed out") from None
    except Exception:
        raise ClientError("Local ASR connection failed") from None


def read_wav(path: Path, config: Config) -> bytes:
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getcomptype() != "NONE" or wav.getsampwidth() != 2
                    or wav.getnchannels() not in (1, 2) or wav.getframerate() != RATE):
                raise ClientError("WAV must be uncompressed PCM16, mono or stereo, at 16000 Hz")
            frames = wav.getnframes()
            channels = wav.getnchannels()
            if frames > int(config.max_record_seconds * RATE):
                raise ClientError("WAV exceeds max_record_seconds")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * channels * 2:
                raise ClientError("WAV sample data is truncated")
            result = bytearray(frames * 4)
            for i, samples in enumerate(struct.iter_unpack("<" + "h" * channels, pcm)):
                struct.pack_into("<f", result, i * 4, sum(samples) / (32768.0 * channels))
            return bytes(result)
    except ClientError:
        raise
    except (OSError, wave.Error, EOFError, ValueError):
        raise ClientError("Cannot read PCM16 WAV") from None


def plain_text(text: str) -> str:
    # Includes C0/C1, DEL, format controls, surrogates, and Unicode separators.
    text = "".join(" " if c.isspace() or unicodedata.category(c).startswith("C") else c
                   for c in text)
    return " ".join(text.split())


def process_text(text: str, rules: word_rules.Rules) -> str:
    # The same policy serves desktop delivery, WAV output, and safe previews.
    if len(text) > word_rules.MAX_TEXT_CHARS:
        raise word_rules.WordsError("Correction input exceeds the text limit")
    return plain_text(rules.apply(plain_text(text)))


def recorder_child_setup(parent_pid: int) -> None:
    """Linux: do not leave a microphone process after an owner is killed."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        os._exit(1)


def recorder_process_command(command: list[str]) -> list[str]:
    # A fresh interpreter sets the death signal and execs parec in the same PID.
    # Python preexec_fn could deadlock after fork when asyncio has watcher threads.
    return [sys.executable, str(Path(__file__).resolve()), "_recorder",
            str(os.getpid()), json.dumps(command)]


async def reap_process(proc: asyncio.subprocess.Process) -> None:
    async def reap():
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 1.5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 1)
            except TimeoutError:
                raise ClientError("Child process did not exit after kill") from None

    # Finish bounded TERM/KILL reaping even if cancellation arrives repeatedly.
    cleanup = asyncio.create_task(reap())
    cancelled = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as error:
            cancelled = error
        except Exception:
            break  # Retrieve the cleanup error below, after honoring cancellation.
    try:
        cleanup.result()
    finally:
        if cancelled is not None:
            raise cancelled


async def capture_audio(config: Config, stop: asyncio.Event, on_limit, on_ready=None) -> bytes:
    if stop.is_set():
        return b""
    command = ["parec", "--raw", "--format=float32le", "--rate=16000", "--channels=1",
               "--latency-msec=40"]
    if config.source:
        command.append("--device=" + config.source)
    try:
        proc = await asyncio.create_subprocess_exec(
            *recorder_process_command(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
    except OSError:
        raise ClientError("Cannot start parec; check PipeWire PulseAudio support") from None
    audio = bytearray()
    limit = asyncio.Event()
    maximum = int(config.max_record_seconds * RATE) * 4

    async def drain():
        ready = False
        while True:
            block = await proc.stdout.read(65536)
            if not block:
                return
            audio.extend(block[:max(0, maximum - len(audio))])
            if not ready and len(audio) >= 4:
                ready = True
                if on_ready is not None:
                    on_ready()
            if len(audio) >= maximum:
                limit.set()

    reader = asyncio.create_task(drain())
    stopped = asyncio.create_task(stop.wait())
    limited = asyncio.create_task(limit.wait())
    try:
        done, _ = await asyncio.wait((reader, stopped, limited),
                                    timeout=config.max_record_seconds,
                                    return_when=asyncio.FIRST_COMPLETED)
        if reader in done and not stop.is_set() and not limit.is_set():
            await reader
            raise ClientError("Recorder exited unexpectedly")
        if not stop.is_set():
            on_limit()
        await reap_process(proc)
        try:
            await asyncio.wait_for(asyncio.shield(reader), 1)
        except TimeoutError:
            raise ClientError("Recorder output did not close") from None
        return bytes(audio[:len(audio) // 4 * 4])
    finally:
        try:
            await reap_process(proc)
        finally:
            for task in (reader, stopped, limited):
                task.cancel()
            await asyncio.gather(reader, stopped, limited, return_exceptions=True)
            audio.clear()


async def run_tool(command: list[str], text: str | None = None,
                   capture: bool = False, timeout: float = PROCESS_TIMEOUT) -> bytes:
    """No shells, command-line transcript arguments, or inherited stderr."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdin=subprocess.PIPE if text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except OSError:
        raise ClientError("Desktop helper is unavailable") from None
    try:
        async with asyncio.timeout(timeout):
            stdout, _ = await proc.communicate(text.encode("utf-8") if text is not None else None)
        if proc.returncode:
            raise ClientError("Desktop helper failed")
        return stdout or b""
    except TimeoutError:
        raise ClientError("Desktop helper timed out") from None
    finally:
        await reap_process(proc)


async def focused_window() -> str | None:
    try:
        raw = await run_tool(["hyprctl", "-j", "activewindow"], capture=True, timeout=1)
        address = json.loads(raw).get("address")
        if isinstance(address, str) and re.fullmatch(r"0x[0-9a-fA-F]+", address):
            return address.lower() if int(address, 16) else None
    except (ClientError, ValueError, AttributeError):
        pass
    return None


class FocusGuard:
    """Watch focus transitions as well as comparing stop and delivery targets."""

    def __init__(self):
        self.address: str | None = None
        self.changed = False
        self.watching = False
        self.writer = None
        self.task = None

    async def start(self):
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        if runtime and re.fullmatch(r"[A-Za-z0-9_.-]+", signature):
            path = Path(runtime) / "hypr" / signature / ".socket2.sock"
            try:
                info = path.stat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise OSError
                reader, self.writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(path), limit=65536), .5)
                self.watching = True
                self.task = asyncio.create_task(self.watch(reader))
            except (OSError, TimeoutError):
                pass
        return self

    async def capture(self):
        self.address = await focused_window()

    async def watch(self, reader):
        try:
            while True:
                event = await reader.readline()
                if not event:
                    break
                if event.startswith(b"activewindowv2>>"):
                    self.changed = True
        except (OSError, ValueError):
            pass
        finally:
            self.watching = False

    async def safe(self) -> bool:
        current = await focused_window()
        return bool(self.address and current == self.address and self.watching and not self.changed)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.writer:
            self.writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(self.writer.wait_closed(), .5)


async def notify(config: Config, message: str) -> None:
    if config.notifications:
        with contextlib.suppress(ClientError):
            await run_tool(["notify-send", "--app-name=Local Dictate",
                            "--hint=string:x-canonical-private-synchronous:local-dictate",
                            "Local Dictate (beta)", message],
                           timeout=2)


async def deliver(text: str, config: Config, focus: FocusGuard | None) -> str:
    text = plain_text(text)
    if not text:
        return "empty"
    if config.output == "auto" and focus is not None and await focus.safe():
        try:
            # A failed or timed out wtype can have typed a prefix. Never retry it.
            await run_tool(["wtype", "-"], text=text)
            return "typed"
        except ClientError:
            pass
    try:
        await run_tool(["wl-copy", "--type", "text/plain;charset=utf-8"], text=text)
    except ClientError:
        raise ClientError("Could not deliver dictation; wtype or clipboard failed") from None
    return "copied"


class Session:
    def __init__(self, config: Config, update):
        self.config = config
        self.update = update
        self.state = "starting"
        self.stop = asyncio.Event()
        self.cancelled = False
        self.focus = FocusGuard()
        self.focus_task = None
        self.task = None
        self.run_started = False
        self.delivery_result: str | None = None
        self.notification_tasks = []

    def set_state(self, state: str):
        self.state = state
        self.cancel_notifications()
        # State-file failures must not disable stop, cancellation, or cleanup.
        with contextlib.suppress(OSError):
            self.update(state)

    def cancel_notifications(self):
        for task in self.notification_tasks:
            if not task.done() and not task.cancelling():
                task.cancel()

    def notify_state(self, message: str):
        state = self.state
        previous = tuple(self.notification_tasks)

        async def announce():
            # Order helpers without putting their cleanup on the audio/decode path.
            await asyncio.gather(*(asyncio.shield(task) for task in previous),
                                 return_exceptions=True)
            if self.state == state and not self.cancelled:
                await notify(self.config, message)

        self.notification_tasks.append(asyncio.create_task(announce()))

    async def drain_notifications(self):
        self.cancel_notifications()
        # Cancellation during a drain must not interrupt a helper's child reaping.
        await asyncio.gather(*(asyncio.shield(task) for task in self.notification_tasks),
                             return_exceptions=True)
        self.notification_tasks.clear()

    async def finish_notification(self, message: str):
        await self.drain_notifications()
        if not self.cancelled:
            await notify(self.config, message)

    async def cleanup(self):
        await self.drain_notifications()
        if self.focus_task:
            self.focus_task.cancel()
            await asyncio.gather(self.focus_task, return_exceptions=True)
        await self.focus.close()

    def recording_ready(self):
        if self.state == "starting" and not self.stop.is_set() and not self.cancelled:
            self.set_state("recording")
            self.notify_state("Recording")

    def request_stop(self):
        if self.state in ("starting", "recording") and not self.stop.is_set():
            self.stop.set()
            self.focus.changed = False
            self.focus_task = asyncio.create_task(self.focus.capture())
            self.set_state("stopping")

    def request_cancel(self):
        # A late cancel cannot undo completed typing or clipboard delivery.
        if self.delivery_result is not None:
            return
        if not self.cancelled:
            self.cancelled = True
            if self.task and self.run_started:
                self.task.cancel()
            self.set_state("cancelling")

    async def run(self):
        self.run_started = True
        try:
            if self.cancelled:
                return
            rules = load_words(self.config)
            await probe_server(self.config)
            if self.stop.is_set() or self.cancelled:
                return
            # Subscribe before recording so focus transitions during the stop
            # snapshot and decode cannot disappear in a subscription gap.
            await self.focus.start()
            if self.stop.is_set() or self.cancelled:
                return
            audio = await capture_audio(self.config, self.stop, self.request_stop,
                                        self.recording_ready)
            if self.cancelled:
                return
            if not audio:
                await self.finish_notification("No audio")
                return
            self.set_state("transcribing")
            self.notify_state("Transcribing")
            text = process_text(await transcribe_audio(audio, self.config), rules)
            del audio
            if self.cancelled:
                return
            self.set_state("delivering")
            if self.focus_task:
                await self.focus_task
            await asyncio.sleep(self.config.insertion_delay_ms / 1000)
            if self.cancelled:
                return
            result = await deliver(text, self.config, self.focus)
            self.delivery_result = result
            del text
            messages = {"typed": "Dictation inserted", "copied": "Dictation copied to clipboard",
                        "empty": "No speech recognized"}
            await self.finish_notification(messages[result])
        except asyncio.CancelledError:
            # No transcript delivery after cancellation, including during decode or delay.
            pass
        except (ClientError, word_rules.WordsError) as error:
            await self.finish_notification(str(error))
        except Exception:
            await self.finish_notification("Dictation failed")
        finally:
            cleanup = asyncio.create_task(self.cleanup())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            if self.cancelled:
                await notify(self.config, "Cancelled")


def checked_file(path: Path) -> int:
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            os.close(fd)
            raise ClientError("Unsafe runtime lock file")
        return fd
    except OSError:
        raise ClientError("Cannot open private runtime lock") from None


class Runtime:
    def __init__(self):
        value = os.environ.get("XDG_RUNTIME_DIR", "")
        if not value or not Path(value).is_absolute():
            raise ClientError("XDG_RUNTIME_DIR must name a private runtime directory")
        root = Path(value)
        self.path = root / "local-dictate"
        try:
            for path in (root, self.path):
                if path == self.path:
                    path.mkdir(mode=0o700, exist_ok=True)
                info = path.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077):
                    raise ClientError("Runtime directory must be owned by you and mode 0700")
        except OSError:
            raise ClientError("Cannot access private runtime directory") from None
        self.socket_path = self.path / "control.sock"
        self.state_path = self.path / "state.json"
        if len(os.fsencode(self.socket_path)) >= 108:
            raise ClientError("Runtime socket path is too long")

    def write_state(self, state: str, token: str):
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.path)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"state": state, "session": token}, stream)
                stream.write("\n")
            os.replace(name, self.state_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)

    def token(self) -> str:
        try:
            fd = os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > 4096):
                    raise ValueError
                token = json.loads(stream.read(4096))["session"]
            return str(uuid.UUID(token))
        except (OSError, ValueError, KeyError, TypeError):
            raise ClientError("Active session state is unavailable") from None

    def clear(self):
        for path in (self.socket_path, self.state_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


@contextlib.contextmanager
def command_lock(runtime: Runtime):
    fd = checked_file(runtime.path / "command.lock")
    deadline = time.monotonic() + CONTROL_TIMEOUT
    try:
        while not try_lock(fd):
            if time.monotonic() >= deadline:
                raise ClientError("Another control command is busy")
            time.sleep(.02)
        yield
    finally:
        os.close(fd)


def owner_command(config: Config, lock_fd: int, socket_fd: int, token: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "_owner", str(lock_fd),
            str(socket_fd), token, json.dumps(dataclasses.asdict(config))]


def start_owner(runtime: Runtime, config: Config, owner_fd: int) -> dict:
    runtime.clear()
    token = str(uuid.uuid4())
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(runtime.socket_path))
        os.chmod(runtime.socket_path, 0o600)
        listener.listen(16)
        listener.setblocking(False)
        runtime.write_state("starting", token)
        child = subprocess.Popen(owner_command(config, owner_fd, listener.fileno(), token),
                                 pass_fds=(owner_fd, listener.fileno()), start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
        _owner_children.append(child)
    except OSError:
        runtime.clear()
        raise ClientError("Could not start dictation owner") from None
    finally:
        listener.close()
    return {"state": "starting"}


def send_control(runtime: Runtime, command: str) -> dict:
    request = {"command": command, "session": runtime.token()}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(CONTROL_TIMEOUT)
            deadline = time.monotonic() + CONTROL_TIMEOUT
            client.connect(str(runtime.socket_path))
            client.sendall(json.dumps(request).encode() + b"\n")
            response = bytearray()
            while b"\n" not in response:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                client.settimeout(remaining)
                block = client.recv(4096 - len(response))
                if not block or len(response) + len(block) >= 4096:
                    raise ValueError
                response.extend(block)
            result = json.loads(response)
            if not isinstance(result, dict) or not isinstance(result.get("state"), str):
                raise ValueError
            if result.get("error"):
                raise ValueError
            return {"state": result["state"]}
    except (OSError, ValueError):
        raise ClientError("Active owner is not responding; no new recorder was started") from None


def control(command: str, config: Config | Callable[[], Config]) -> dict:
    _owner_children[:] = [child for child in _owner_children if child.poll() is None]
    runtime = Runtime()
    with command_lock(runtime):
        owner_fd = checked_file(runtime.path / "owner.lock")
        try:
            if try_lock(owner_fd):
                if command in ("start", "toggle"):
                    return start_owner(runtime, config() if callable(config) else config, owner_fd)
                runtime.clear()
                return {"state": "idle"}
            try:
                return send_control(runtime, command)
            except ClientError:
                # Closing the listener and releasing the lifetime lock are
                # separate operations. Allow bounded time for normal teardown.
                deadline = time.monotonic() + .25
                while True:
                    if try_lock(owner_fd):
                        if command in ("start", "toggle"):
                            return start_owner(runtime, config() if callable(config) else config, owner_fd)
                        runtime.clear()
                        return {"state": "idle"}
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(.01)
                raise
        finally:
            os.close(owner_fd)


async def serve_owner(config: Config, listener: socket.socket, runtime: Runtime,
                      token: str):
    session = Session(config, lambda state: runtime.write_state(state, token))

    async def handle(reader, writer):
        try:
            request = json.loads(await asyncio.wait_for(reader.readline(), 1))
            if not isinstance(request, dict) or request.get("session") != token:
                reply = {"state": session.state, "error": "Session mismatch"}
            else:
                command = request.get("command")
                if command == "cancel":
                    session.request_cancel()
                elif command in ("stop", "toggle"):
                    session.request_stop()
                elif command not in ("start", "status"):
                    raise ValueError
                reply = {"state": session.state}
            writer.write(json.dumps(reply).encode() + b"\n")
            await asyncio.wait_for(writer.drain(), 1)
        except (OSError, ValueError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), .5)

    server = await asyncio.start_unix_server(handle, sock=listener, limit=4096)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, session.request_cancel)
    session.task = asyncio.create_task(session.run())
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await session.task
    finally:
        server.close()
        await server.wait_closed()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.remove_signal_handler(sig)


def owner_main(args: list[str]) -> int:
    lock_fd, socket_fd = int(args[0]), int(args[1])
    token, values = args[2], json.loads(args[3])
    dictionary_path = values.pop("words_path", None)
    config = dataclasses.replace(Config.from_dict(values), words_path=dictionary_path)
    runtime = Runtime()
    listener = socket.socket(fileno=socket_fd)
    try:
        asyncio.run(serve_owner(config, listener, runtime, token))
    finally:
        listener.close()
        runtime.clear()
        os.close(lock_fd)
    return 0


async def doctor(config: Config) -> int:
    import shutil
    problems = False
    try:
        rules = load_words(config)
        print(f"corrections: valid, {len(rules.replacements)} rules, "
              + ("enabled" if rules.enabled else "disabled"))
    except word_rules.WordsError as error:
        print("corrections: " + str(error))
        problems = True
    for command in ("parec", "wtype", "wl-copy", "hyprctl"):
        available = shutil.which(command) is not None
        print(f"{command}: {'available' if available else 'missing'}")
        if command in ("parec", "wl-copy") and not available:
            problems = True
    try:
        Runtime()
        print("runtime: private")
        print("session: " + control("status", config)["state"])
    except ClientError as error:
        print("runtime: " + str(error))
        problems = True
    try:
        await probe_server(config)
        print("ASR: reachable loopback WebSocket (proxy disabled)")
    except ClientError as error:
        print("ASR: " + str(error))
        problems = True
    print("Server bind address and user-unit firewall enforcement are not verified here.")
    print("No microphone, typing, or clipboard check was performed.")
    return int(problems)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_recorder":
        recorder_child_setup(int(argv[1]))
        command = json.loads(argv[2])
        try:
            os.execvp(command[0], command)
        except OSError:
            return 127
    if argv and argv[0] == "_owner":
        return owner_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
                        / "local-dictate" / "config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("toggle", "start", "stop", "cancel", "status", "doctor", "transcribe"):
        part = sub.add_parser(command)
        if command == "status":
            part.add_argument("--json", action="store_true")
        if command == "transcribe":
            part.add_argument("wav", type=Path)
    word_rules.add_parser(sub)
    args = parser.parse_args(argv)
    try:
        if args.command == "words":
            return word_rules.run(args, words_path(args.config), process_text)
        if args.command == "transcribe":
            config = load_config(args.config)
            rules = load_words(config)
            text = asyncio.run(transcribe_audio(read_wav(args.wav, config), config))
            print(process_text(text, rules))
        elif args.command == "doctor":
            return asyncio.run(doctor(load_config(args.config)))
        else:
            # Recovery and inspection must still work if config was edited badly
            # during a session. Only a new owner needs to load its configuration.
            result = control(args.command, lambda: new_session_config(args.config))
            print(json.dumps(result) if getattr(args, "json", False) else result["state"])
        return 0
    except (ClientError, word_rules.WordsError, OSError) as error:
        message = str(error) if isinstance(error, (ClientError, word_rules.WordsError)) else "Operating system error"
        if args.command == "status" and args.json:
            print(json.dumps({"state": "error", "error": message}))
        else:
            print("local-dictate: " + message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
