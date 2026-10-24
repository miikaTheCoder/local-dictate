"""Synthetic-only tests. Never invoke a recorder or desktop delivery helper."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import errno
import io
import json
import multiprocessing
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_dictate as ld
from websockets.asyncio.server import serve


def config(**values):
    return ld.Config.from_dict({"notifications": False, **values})


class ConfigTests(unittest.TestCase):
    def test_defaults_and_example(self):
        example = ld.load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.assertEqual(example.max_record_seconds, 120)
        self.assertEqual(example.insertion_delay_ms, 300)
        self.assertEqual(example.server_url, "ws://127.0.0.1:6006/")

    def test_default_config_when_xdg_is_unset_or_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".config/local-dictate/config.toml"
            path.parent.mkdir(parents=True)
            path.write_text('output = "clipboard"\n')
            for value in (None, ""):
                with self.subTest(xdg=value), patch.dict(os.environ, {"HOME": directory}):
                    if value is None:
                        os.environ.pop("XDG_CONFIG_HOME", None)
                    else:
                        os.environ["XDG_CONFIG_HOME"] = value
                    with patch.object(ld, "doctor", new_callable=AsyncMock, return_value=0) as doctor:
                        self.assertEqual(ld.main(["doctor"]), 0)
                        self.assertEqual(doctor.call_args.args[0].output, "clipboard")

    def test_default_config_uses_nonempty_xdg(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local-dictate/config.toml"
            path.parent.mkdir()
            path.write_text('output = "clipboard"\n')
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}), \
                    patch.object(ld, "doctor", new_callable=AsyncMock, return_value=0) as doctor:
                self.assertEqual(ld.main(["doctor"]), 0)
                self.assertEqual(doctor.call_args.args[0].output, "clipboard")

    def test_loopback_literals(self):
        for url in ("ws://127.0.0.1:6006", "ws://127.1.2.3/a", "ws://[::1]:6006/"):
            with self.subTest(url=url):
                self.assertTrue(ld.local_url(url).startswith("ws://"))

    def test_remote_and_ambiguous_urls_rejected(self):
        for url in ("wss://127.0.0.1", "http://127.0.0.1", "ws://example.com",
                    "ws://0.0.0.0", "ws://192.168.1.1", "ws://[::]", "ws://127.1",
                    "ws://127.0.0.1:0", "ws://127.0.0.1:65536", "ws://user@127.0.0.1",
                    "ws://127.0.0.1/#fragment", "ws://127.0.0.1/\n", "ws://localhost.evil"):
            with self.subTest(url=url), self.assertRaises(ld.ClientError):
                ld.local_url(url)

    def test_localhost_is_pinned(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 6006)),
                   (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 6006, 0, 0))]
        with patch.object(socket, "getaddrinfo", return_value=answers):
            self.assertEqual(ld.local_url("ws://localhost:6006"), "ws://127.0.0.1:6006/")

    def test_localhost_with_nonlocal_resolution_rejected(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 6006))]
        with patch.object(socket, "getaddrinfo", return_value=answers):
            with self.assertRaises(ld.ClientError):
                ld.local_url("ws://localhost:6006")

    def test_invalid_settings(self):
        for values in ({"max_record_seconds": 300}, {"max_record_seconds": 0},
                       {"max_record_seconds": float("inf")}, {"max_record_seconds": True},
                       {"recognition_timeout": 0}, {"output": "shell"},
                       {"notifications": "yes"}, {"insertion_delay_ms": -1},
                       {"insertion_delay_ms": 0.5}, {"source": "a\nb"},
                       {"device": "not-a-key"}, {"server_url": 12}):
            with self.subTest(values=values), self.assertRaises(ld.ClientError):
                ld.Config.from_dict(values)

    def test_bad_toml_error_has_no_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("secret private text = [")
            with self.assertRaisesRegex(ld.ClientError, "Cannot read configuration TOML"):
                ld.load_config(path)

    def test_proxy_is_disabled_explicitly(self):
        with patch("websockets.asyncio.client.connect.__init__", return_value=None) as connect:
            ld.websocket_connect(config())
            self.assertIsNone(connect.call_args.kwargs["proxy"])

    def test_plain_text_controls_and_whitespace(self):
        self.assertEqual(ld.plain_text(" a\n\tb\x1b\x00\x7f\x85c\u2028d\u202ee\ud800f "),
                         "a b c d e f")


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def exchange(self, handler, audio=b"\0" * 4, **options):
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            return await ld.transcribe_audio(audio, config(server_url=f"ws://127.0.0.1:{port}",
                                                           **options))

    async def test_native_frame_sizes_and_done_after_json(self):
        audio = b"\0\0\x00\x3f" * 31001
        frames = []
        completed = asyncio.Event()

        async def handler(ws):
            first = await ws.recv()
            frames.append(first)
            rate, length = struct.unpack("<ii", first[:8])
            self.assertEqual((rate, length), (16000, len(audio)))
            received = bytearray(first[8:])
            while len(received) < length:
                block = await ws.recv()
                self.assertIsInstance(block, bytes)
                frames.append(block)
                received.extend(block)
            self.assertEqual(bytes(received), audio)
            # The audio byte count, without Done, must be sufficient to decode.
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(ws.recv(), .03)
            await ws.send(json.dumps({"text": "synthetic result"}))
            self.assertEqual(await ws.recv(), "Done")
            completed.set()

        self.assertEqual(await self.exchange(handler, audio), "synthetic result")
        await asyncio.wait_for(completed.wait(), 1)
        self.assertEqual([len(frame) for frame in frames], [60008, 60000, 4004])
        self.assertTrue(all(len(frame) % 4 == 0 for frame in frames))

    async def test_empty_audio_does_not_connect(self):
        with patch.object(ld, "websocket_connect") as connect:
            self.assertEqual(await ld.transcribe_audio(b"", config()), "")
            connect.assert_not_called()

    async def test_unaligned_audio_rejected(self):
        with self.assertRaises(ld.ClientError):
            await ld.transcribe_audio(b"abc", config())

    async def test_empty_response_still_gets_done(self):
        done = asyncio.Event()

        async def handler(ws):
            await ws.recv()
            await ws.send('{"text":""}')
            self.assertEqual(await ws.recv(), "Done")
            done.set()

        self.assertEqual(await self.exchange(handler), "")
        await asyncio.wait_for(done.wait(), 1)

    async def test_malformed_json(self):
        async def handler(ws):
            await ws.recv()
            await ws.send("not json")
            await ws.wait_closed()

        with self.assertRaisesRegex(ld.ClientError, "invalid JSON"):
            await self.exchange(handler)

    async def test_missing_text(self):
        for reply in ('{}', '[]', '{"text":null}', '{"text":7}'):
            async def handler(ws):
                await ws.recv()
                await ws.send(reply)
                await ws.wait_closed()

            with self.subTest(reply=reply), self.assertRaisesRegex(ld.ClientError, "text string"):
                await self.exchange(handler)

    async def test_binary_reply_rejected(self):
        async def handler(ws):
            await ws.recv()
            await ws.send(b'{"text":"fake"}')
            await ws.wait_closed()

        with self.assertRaisesRegex(ld.ClientError, "binary response"):
            await self.exchange(handler)

    async def test_disconnect(self):
        async def handler(ws):
            await ws.recv()
            await ws.close()

        with self.assertRaisesRegex(ld.ClientError, "connection failed"):
            await self.exchange(handler)

    async def test_decode_timeout_closes_socket(self):
        closed = asyncio.Event()

        async def handler(ws):
            await ws.recv()
            await ws.wait_closed()
            closed.set()

        with self.assertRaisesRegex(ld.ClientError, "timed out"):
            await self.exchange(handler, recognition_timeout=.05)
        self.assertTrue(closed.is_set())

    async def test_cancel_decode_closes_socket_without_done(self):
        received = asyncio.Event()
        frames = []

        async def handler(ws):
            frames.append(await ws.recv())
            received.set()
            try:
                frames.append(await ws.recv())
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            task = asyncio.create_task(ld.transcribe_audio(b"\0" * 4,
                                      config(server_url=f"ws://127.0.0.1:{port}")))
            await asyncio.wait_for(received.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(frames), 1)

    async def test_probe_has_no_audio_frames(self):
        frames = []

        async def handler(ws):
            try:
                frames.append(await ws.recv())
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            await ld.probe_server(config(server_url=f"ws://127.0.0.1:{port}"))
        self.assertEqual(frames, [])

    async def test_unavailable_probe(self):
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
            with self.assertRaisesRegex(ld.ClientError, "unavailable"):
                await ld.probe_server(config(server_url=f"ws://127.0.0.1:{port}"))

    async def assert_redirect_rejected(self, operation, status=302,
                                       location="ws://203.0.113.9:6006/"):
        attempts = []
        payloads = []
        closed = asyncio.Event()

        async def redirect(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write((f"HTTP/1.1 {status} Redirect\r\nLocation: {location}\r\n"
                              "Content-Length: 0\r\n\r\n").encode("ascii"))
                await writer.drain()
                payloads.append(await asyncio.wait_for(reader.read(), 1))
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        async with await asyncio.start_server(redirect, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            loop = asyncio.get_running_loop()
            real_connect = loop.create_connection

            async def guarded_connect(factory, host=None, port=None, **kwargs):
                attempts.append((host, port))
                # A regression must fail safely before opening any other endpoint.
                if (host, port) != ("127.0.0.1", server.sockets[0].getsockname()[1]):
                    raise AssertionError("Redirect attempted an unapproved connection")
                return await real_connect(factory, host, port, **kwargs)

            conf = config(server_url=f"ws://127.0.0.1:{port}")
            with patch.object(loop, "create_connection", side_effect=guarded_connect), \
                    self.assertRaises(ld.ClientError):
                await operation(conf)
            await asyncio.wait_for(closed.wait(), 1)
        self.assertEqual(attempts, [("127.0.0.1", port)])
        self.assertEqual(payloads, [b""])

    async def test_probe_rejects_every_redirect_status(self):
        for status in (300, 301, 302, 303, 307, 308):
            with self.subTest(status=status):
                await self.assert_redirect_rejected(ld.probe_server, status)

    async def test_transcription_rejects_every_redirect_status_without_audio(self):
        async def transcribe(conf):
            return await ld.transcribe_audio(b"1234", conf)

        for status in (300, 301, 302, 303, 307, 308):
            with self.subTest(status=status):
                await self.assert_redirect_rejected(transcribe, status)

    async def test_relative_and_loopback_redirects_are_also_rejected(self):
        async def transcribe(conf):
            return await ld.transcribe_audio(b"1234", conf)

        for operation in (ld.probe_server, transcribe):
            for location in ("/elsewhere", "ws://127.0.0.1:6006/", "wss://203.0.113.9/"):
                with self.subTest(operation=operation.__name__, location=location):
                    await self.assert_redirect_rejected(operation, location=location)

    async def test_cli_synthetic_wav_to_fake_websocket(self):
        done = asyncio.Event()

        async def handler(ws):
            frame = await ws.recv()
            self.assertEqual(struct.unpack("<ii", frame[:8]), (16000, 16))
            self.assertEqual(frame[8:], struct.pack("<ffff", 0, .5, -.5, 0))
            await ws.send(json.dumps({"text": "synthetic\nresult"}))
            self.assertEqual(await ws.recv(), "Done")
            done.set()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "synthetic.wav"
                settings = Path(directory) / "config.toml"
                with wave.open(str(path), "wb") as wav:
                    wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    wav.writeframes(struct.pack("<hhhh", 0, 16384, -16384, 0))
                settings.write_text(f'server_url = "ws://127.0.0.1:{port}"\noutput = "clipboard"\n')
                proc = await asyncio.create_subprocess_exec(
                    "/usr/bin/python3", str(Path(ld.__file__)), "--config", str(settings),
                    "transcribe", str(path), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), 3)
                finally:
                    await ld.reap_process(proc)
                self.assertEqual(proc.returncode, 0, stderr)
                self.assertEqual(stdout, b"synthetic result\n")
                self.assertEqual(stderr, b"")
                await asyncio.wait_for(done.wait(), 1)


class WavTests(unittest.TestCase):
    def make_wav(self, path, samples, channels=1, rate=16000, width=2):
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(width)
            wav.setframerate(rate)
            wav.writeframes(struct.pack("<" + "h" * len(samples), *samples))

    def test_pcm16_mono_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.wav"
            self.make_wav(path, [-32768, 0, 16384])
            self.assertEqual(struct.unpack("<fff", ld.read_wav(path, config())), (-1., 0., .5))

    def test_stereo_downmix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.wav"
            self.make_wav(path, [16384, -16384, 32767, 32767], channels=2)
            self.assertEqual(struct.unpack("<ff", ld.read_wav(path, config())),
                             (0., 32767 / 32768))

    def test_wrong_rate_or_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.wav"
            for rate, width in ((48000, 2), (16000, 1)):
                self.make_wav(path, [0] * 10, rate=rate, width=width)
                with self.assertRaisesRegex(ld.ClientError, "PCM16"):
                    ld.read_wav(path, config())

    def test_oversized_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.wav"
            self.make_wav(path, [0] * 160)
            with self.assertRaisesRegex(ld.ClientError, "exceeds"):
                ld.read_wav(path, config(max_record_seconds=.001))

    def test_truncated_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.wav"
            self.make_wav(path, [0] * 100)
            path.write_bytes(path.read_bytes()[:-40])
            with self.assertRaisesRegex(ld.ClientError, "truncated"):
                ld.read_wav(path, config())

    def test_cli_transcribe_has_only_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.wav"
            self.make_wav(path, [0])
            with patch.object(ld, "transcribe_audio", AsyncMock(return_value="fake\ntext")), \
                    patch.object(ld, "run_tool", AsyncMock()) as tool, \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(ld.main(["--config", str(Path(directory) / "none"),
                                          "transcribe", str(path)]), 0)
                self.assertEqual(stdout.getvalue(), "fake text\n")
                tool.assert_not_called()


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def target(self, safe=True):
        return Mock(safe=AsyncMock(return_value=safe))

    async def test_guarded_typing_uses_stdin_and_filters_controls(self):
        with patch.object(ld, "run_tool", AsyncMock(return_value=b"")) as tool:
            result = await ld.deliver("hello\n\tthere\x1b\x00", config(), self.target())
            self.assertEqual(result, "typed")
            tool.assert_awaited_once_with(["wtype", "-"], text="hello there")

    async def test_focus_switch_copies(self):
        with patch.object(ld, "run_tool", AsyncMock(return_value=b"")) as tool:
            self.assertEqual(await ld.deliver("hello", config(), self.target(False)), "copied")
            self.assertEqual(tool.call_args.args[0][0], "wl-copy")

    async def test_absent_target_copies(self):
        with patch.object(ld, "run_tool", AsyncMock(return_value=b"")) as tool:
            self.assertEqual(await ld.deliver("hello", config(), None), "copied")
            self.assertEqual(tool.call_args.args[0][0], "wl-copy")

    async def test_forced_clipboard_skips_focus(self):
        target = self.target()
        with patch.object(ld, "run_tool", AsyncMock(return_value=b"")) as tool:
            await ld.deliver("hello", config(output="clipboard"), target)
            target.safe.assert_not_called()
            self.assertEqual(tool.call_args.args[0][0], "wl-copy")

    async def test_partial_wtype_failure_copies_and_never_retypes(self):
        with patch.object(ld, "run_tool", AsyncMock(side_effect=[ld.ClientError("failed"), b""])) as tool:
            self.assertEqual(await ld.deliver("hello", config(), self.target()), "copied")
            self.assertEqual([call.args[0][0] for call in tool.call_args_list], ["wtype", "wl-copy"])

    async def test_failed_clipboard_is_reported(self):
        with patch.object(ld, "run_tool", AsyncMock(side_effect=ld.ClientError("failed"))):
            with self.assertRaisesRegex(ld.ClientError, "Could not deliver"):
                await ld.deliver("hello", config(output="clipboard"), None)

    async def test_both_delivery_tools_fail(self):
        with patch.object(ld, "run_tool", AsyncMock(side_effect=ld.ClientError("failed"))) as tool:
            with self.assertRaises(ld.ClientError):
                await ld.deliver("hello", config(), self.target())
            self.assertEqual(tool.await_count, 2)

    async def test_empty_result_has_no_effect(self):
        with patch.object(ld, "run_tool", AsyncMock()) as tool:
            self.assertEqual(await ld.deliver("\n\t\x1b", config(), self.target()), "empty")
            tool.assert_not_called()

    async def test_notifications_do_not_include_transcript(self):
        with patch.object(ld, "run_tool", AsyncMock(return_value=b"")) as tool:
            await ld.notify(config(notifications=True), "Recording")
            self.assertEqual(tool.call_args.args[0][-1], "Recording")
            self.assertIn("--hint=string:x-canonical-private-synchronous:local-dictate",
                          tool.call_args.args[0])
            self.assertIn("Local Dictate (beta)", tool.call_args.args[0])
            self.assertNotIn("text", tool.call_args.kwargs)

    async def test_disabled_notifications_never_start_a_helper(self):
        with patch.object(ld, "run_tool", AsyncMock()) as tool:
            for message in ("Recording", "Transcribing", "Dictation inserted", "Cancelled", "Error"):
                await ld.notify(config(notifications=False), message)
            tool.assert_not_called()

    async def test_tool_timeout_reaps_child(self):
        # This command cannot record, insert, or copy anything.
        spawned = []
        real_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            proc = await real_spawn("/usr/bin/python3", "-c", "import time; time.sleep(30)", **kwargs)
            spawned.append(proc)
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=spawn):
            with self.assertRaisesRegex(ld.ClientError, "timed out"):
                await ld.run_tool(["FAKE"], text="synthetic", timeout=.05)
        self.assertIsNotNone(spawned[0].returncode)

    async def test_cancelled_tool_reaps_child(self):
        spawned = []
        ready = asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            proc = await real_spawn("/usr/bin/python3", "-c", "import time; time.sleep(30)", **kwargs)
            spawned.append(proc)
            ready.set()
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(ld.run_tool(["FAKE"], text="synthetic"))
            await ready.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(spawned[0].returncode)

    async def test_timed_out_tool_reaps_stubborn_child_despite_repeated_cancellation(self):
        terminating = asyncio.Event()
        reaped = asyncio.Event()
        spawned = []
        real_spawn = asyncio.create_subprocess_exec
        before = asyncio.all_tasks()

        async def spawn(*args, **kwargs):
            code = ("import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                    "os.write(1,b'!'); time.sleep(30)")
            proc = await real_spawn("/usr/bin/python3", "-c", code, **kwargs)
            spawned.append(proc)
            # The child's signal handler is installed before the helper times out.
            await proc.stdout.readexactly(1)
            terminate = proc.terminate
            wait = proc.wait

            def term():
                terminate()
                terminating.set()

            async def reap():
                result = await wait()
                reaped.set()
                return result

            proc.terminate = Mock(side_effect=term)
            proc.kill = Mock(wraps=proc.kill)
            proc.wait = AsyncMock(side_effect=reap)
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(ld.run_tool(["FAKE"], capture=True, timeout=.01))
            try:
                await asyncio.wait_for(terminating.wait(), 2)
                self.assertEqual(task.cancelling(), 0)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 4)
                self.assertTrue(reaped.is_set())
                self.assertEqual(spawned[0].returncode, -signal.SIGKILL)
                spawned[0].terminate.assert_called_once()
                spawned[0].kill.assert_called_once()
                self.assertEqual(asyncio.all_tasks() - before, set())
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                for proc in spawned:
                    if proc.returncode is None:
                        proc.kill()
                    await asyncio.wait_for(proc.wait(), 1)

    async def test_reaper_preserves_bounded_kill_failure(self):
        proc = Mock(returncode=None, wait=AsyncMock(side_effect=TimeoutError))
        before = asyncio.all_tasks()
        with patch.object(asyncio, "wait_for", wraps=asyncio.wait_for) as wait_for:
            with self.assertRaisesRegex(ld.ClientError, "did not exit after kill"):
                await ld.reap_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual([call.args[1] for call in wait_for.call_args_list], [1.5, 1])
        self.assertEqual(asyncio.all_tasks() - before, set())

    async def test_missing_tool(self):
        with patch.object(asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError)):
            with self.assertRaisesRegex(ld.ClientError, "unavailable"):
                await ld.run_tool(["FAKE"])

    async def test_focus_guard_rechecks_address(self):
        guard = ld.FocusGuard()
        guard.address = "0x123"
        guard.watching = True
        with patch.object(ld, "focused_window", AsyncMock(return_value="0x456")):
            self.assertFalse(await guard.safe())

    async def test_focus_change_and_return_is_unsafe(self):
        guard = ld.FocusGuard()
        guard.address = "0x123"
        guard.watching = True
        reader = asyncio.StreamReader()
        task = asyncio.create_task(guard.watch(reader))
        reader.feed_data(b"activewindowv2>>456\nactivewindowv2>>123\n")
        await asyncio.sleep(0)
        with patch.object(ld, "focused_window", AsyncMock(return_value="0x123")):
            self.assertFalse(await guard.safe())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_focus_socket_disconnect_fails_closed(self):
        guard = ld.FocusGuard()
        guard.address = "0x123"
        guard.watching = True
        reader = asyncio.StreamReader()
        reader.feed_eof()
        await guard.watch(reader)
        with patch.object(ld, "focused_window", AsyncMock(return_value="0x123")):
            self.assertFalse(await guard.safe())

    async def test_real_focus_event_socket_detects_change(self):
        with tempfile.TemporaryDirectory(prefix="dictate-focus-") as directory:
            path = Path(directory) / "hypr" / "test-instance" / ".socket2.sock"
            path.parent.mkdir(parents=True)
            ready = asyncio.Event()
            finish = asyncio.Event()

            async def events(reader, writer):
                try:
                    await ready.wait()
                    writer.write(b"activewindowv2>>456\nactivewindowv2>>123\n")
                    await writer.drain()
                    await finish.wait()
                finally:
                    writer.close()
                    await writer.wait_closed()

            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory,
                                         "HYPRLAND_INSTANCE_SIGNATURE": "test-instance"}), \
                    patch.object(ld, "focused_window", AsyncMock(return_value="0x123")):
                async with await asyncio.start_unix_server(events, path=str(path)):
                    guard = await ld.FocusGuard().start()
                    try:
                        await guard.capture()
                        self.assertTrue(await guard.safe())
                        ready.set()
                        for _ in range(100):
                            if guard.changed:
                                break
                            await asyncio.sleep(.001)
                        self.assertTrue(guard.changed)
                        self.assertFalse(await guard.safe())
                    finally:
                        finish.set()
                        await guard.close()


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_stopped_capture_never_spawns(self):
        stop = asyncio.Event()
        stop.set()
        with patch.object(asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            self.assertEqual(await ld.capture_audio(config(), stop, Mock()), b"")
            spawn.assert_not_called()

    async def run_recorder(self, code, stop_after=.15, **options):
        real_spawn = asyncio.create_subprocess_exec
        self.spawned = []
        self.commands = []

        async def spawn(*args, **kwargs):
            self.commands.append(tuple(json.loads(args[4])))
            command = [*args[:4], json.dumps(["/usr/bin/python3", "-c", code])]
            proc = await real_spawn(*command, **kwargs)
            self.spawned.append(proc)
            return proc

        stop = asyncio.Event()
        limited = Mock(side_effect=stop.set)
        with patch.object(asyncio, "create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(ld.capture_audio(config(**options), stop, limited))
            if stop_after is not None:
                await asyncio.sleep(stop_after)
                stop.set()
            audio = await task
        return audio, limited

    async def test_exact_parec_format_and_source_argument(self):
        audio, _ = await self.run_recorder(
            "import os,time; os.write(1,b'1234'*10); time.sleep(30)", source="source;literal")
        self.assertEqual(audio, b"1234" * 10)
        self.assertEqual(self.commands[0], ("parec", "--raw", "--format=float32le", "--rate=16000",
                                          "--channels=1", "--latency-msec=40", "--device=source;literal"))
        self.assertIsNotNone(self.spawned[0].returncode)

    async def test_stop_drains_trailing_audio(self):
        code = ("import os,signal,time\n"
                "def done(*a):\n os.write(1,b'5678'); raise SystemExit\n"
                "signal.signal(signal.SIGTERM,done)\n"
                "os.write(1,b'1234')\ntime.sleep(30)\n")
        audio, _ = await self.run_recorder(code, stop_after=.2)
        self.assertEqual(audio, b"12345678")

    async def test_incomplete_float_is_discarded(self):
        audio, _ = await self.run_recorder("import os,time; os.write(1,b'12345'); time.sleep(30)")
        self.assertEqual(audio, b"1234")

    async def test_recording_wall_timeout(self):
        audio, limited = await self.run_recorder("import time; time.sleep(30)", stop_after=None,
                                                max_record_seconds=.05)
        self.assertEqual(audio, b"")
        limited.assert_called_once()
        self.assertIsNotNone(self.spawned[0].returncode)

    async def test_memory_bound(self):
        audio, limited = await self.run_recorder("import os,time; os.write(1,b'1234'*100000); time.sleep(30)",
                                                stop_after=None, max_record_seconds=.3)
        self.assertEqual(len(audio), 19200)
        limited.assert_called_once()

    async def test_unexpected_recorder_exit(self):
        with self.assertRaisesRegex(ld.ClientError, "unexpectedly"):
            await self.run_recorder("raise SystemExit(7)", stop_after=None)
        self.assertIsNotNone(self.spawned[0].returncode)

    async def test_missing_recorder(self):
        with patch.object(asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError)):
            with self.assertRaisesRegex(ld.ClientError, "Cannot start parec"):
                await ld.capture_audio(config(), asyncio.Event(), Mock())

    async def test_cancellation_kills_stubborn_recorder(self):
        real_spawn = asyncio.create_subprocess_exec
        spawned = []
        ready = asyncio.Event()

        async def spawn(*args, **kwargs):
            command = [*args[:4], json.dumps(["/usr/bin/python3", "-c",
                       "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"])]
            proc = await real_spawn(*command, **kwargs)
            spawned.append(proc)
            ready.set()
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(ld.capture_audio(config(), asyncio.Event(), Mock()))
            await ready.wait()
            await asyncio.sleep(.15)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 4)
        self.assertEqual(spawned[0].returncode, -signal.SIGKILL)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.real_notify = ld.notify
        self.patches = [patch.object(ld, "probe_server", AsyncMock()),
                        patch.object(ld.FocusGuard, "start", AsyncMock()),
                        patch.object(ld.FocusGuard, "capture", AsyncMock()),
                        patch.object(ld.FocusGuard, "close", AsyncMock()),
                        patch.object(ld, "notify", AsyncMock()),
                        patch.object(ld, "deliver", AsyncMock(return_value="typed"))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.states = []
        self.session = ld.Session(config(insertion_delay_ms=0), self.states.append)
        self.spawned = []

    async def asyncTearDown(self):
        if self.session.task and not self.session.task.done():
            self.session.request_cancel()
            await asyncio.wait_for(self.session.task, 4)
        for proc in self.spawned:
            await ld.reap_process(proc)

    async def sample_capture(self, conf, stop, on_limit, on_ready):
        on_ready()
        return b"\0" * 4

    def synthetic_recorder(self, code, on_read=None):
        real_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            command = [*args[:4], json.dumps(["/usr/bin/python3", "-c", code])]
            proc = await real_spawn(*command, **kwargs)
            self.spawned.append(proc)
            if on_read is not None:
                read = proc.stdout.read

                async def observed_read(count):
                    block = await read(count)
                    on_read(block)
                    return block

                proc.stdout.read = observed_read
            return proc

        return patch.object(asyncio, "create_subprocess_exec", side_effect=spawn)

    async def test_cancel_while_recording_does_not_decode(self):
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        async def capture(*args):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        with patch.object(ld, "capture_audio", side_effect=capture), \
                patch.object(ld, "transcribe_audio", AsyncMock()) as decode:
            self.session.task = asyncio.create_task(self.session.run())
            await entered.wait()
            self.session.request_cancel()
            await self.session.task
            self.assertTrue(cleaned.is_set())
            decode.assert_not_called()
            ld.deliver.assert_not_called()

    async def test_cancel_before_run_starts_reports_cancelled_without_capture(self):
        with patch.object(ld, "capture_audio", AsyncMock()) as capture:
            self.session.task = asyncio.create_task(self.session.run())
            self.session.request_cancel()
            await asyncio.wait_for(self.session.task, 1)
        ld.probe_server.assert_not_called()
        capture.assert_not_called()
        ld.deliver.assert_not_called()
        self.assertEqual([call.args[-1] for call in ld.notify.call_args_list], ["Cancelled"])
        self.assertEqual(self.session.notification_tasks, [])

    async def test_cancel_while_decoding_does_not_deliver(self):
        entered = asyncio.Event()

        async def decode(*args):
            entered.set()
            await asyncio.Future()

        with patch.object(ld, "capture_audio", side_effect=self.sample_capture), \
                patch.object(ld, "transcribe_audio", side_effect=decode):
            self.session.task = asyncio.create_task(self.session.run())
            await entered.wait()
            self.session.request_cancel()
            self.session.request_cancel()
            await self.session.task
            ld.deliver.assert_not_called()

    async def test_cancel_during_insertion_delay(self):
        self.session.config = dataclasses.replace(self.session.config, insertion_delay_ms=500)
        with patch.object(ld, "capture_audio", side_effect=self.sample_capture), \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="synthetic")):
            self.session.task = asyncio.create_task(self.session.run())
            while self.session.state != "delivering":
                await asyncio.sleep(.001)
            self.session.request_cancel()
            await self.session.task
            ld.deliver.assert_not_called()

    async def test_probe_failure_prevents_recording(self):
        ld.probe_server.side_effect = ld.ClientError("Unavailable")
        with patch.object(ld, "capture_audio", AsyncMock()) as capture:
            await self.session.run()
            capture.assert_not_called()

    async def test_blocked_recording_notification_preserves_early_stop_audio(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        completed = []
        notification_tasks = []

        async def notification(conf, message):
            if message == "Recording":
                notification_tasks.append(asyncio.current_task())
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            completed.append(message)

        code = ("import os,signal,time\n"
                "def done(*a):\n os.write(1,b'5678'); raise SystemExit\n"
                "signal.signal(signal.SIGTERM,done)\n"
                "os.write(1,b'1234')\ntime.sleep(30)\n")
        with self.synthetic_recorder(code), \
                patch.object(ld, "notify", side_effect=notification), \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="synthetic")) as decode:
            self.session.task = asyncio.create_task(self.session.run())
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(self.session.state, "recording")
            self.assertIsNone(self.spawned[0].returncode)
            self.session.request_stop()
            await asyncio.wait_for(self.session.task, 2)
        decode.assert_awaited_once_with(b"12345678", self.session.config)
        ld.deliver.assert_awaited_once()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(all(task.done() for task in notification_tasks))
        self.assertEqual(self.session.notification_tasks, [])
        self.assertEqual(completed, ["Dictation inserted"])
        self.assertIsNotNone(self.spawned[0].returncode)

    async def test_recording_ready_waits_for_a_complete_pcm_sample(self):
        partial = asyncio.Event()
        ready = asyncio.Event()

        def read(block):
            if block == b"1":
                partial.set()

        async def notification(conf, message):
            if message == "Recording":
                ready.set()

        code = ("import os,signal,time\n"
                "signal.signal(signal.SIGUSR1,lambda *a: os.write(1,b'234'))\n"
                "os.write(1,b'1')\ntime.sleep(30)\n")
        with self.synthetic_recorder(code, read), \
                patch.object(ld, "notify", side_effect=notification) as notifier, \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="synthetic")) as decode:
            self.session.task = asyncio.create_task(self.session.run())
            await asyncio.wait_for(partial.wait(), 2)
            self.assertEqual(self.session.state, "starting")
            notifier.assert_not_called()
            self.spawned[0].send_signal(signal.SIGUSR1)
            await asyncio.wait_for(ready.wait(), 2)
            self.assertEqual(self.states, ["recording"])
            self.session.request_stop()
            await asyncio.wait_for(self.session.task, 2)
        decode.assert_awaited_once_with(b"1234", self.session.config)

    async def test_cancel_with_blocked_notifier_and_failed_state_write_reaps_recorder(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        completed = []

        async def notification(conf, message):
            if message == "Recording":
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            completed.append(message)

        code = "import os,time; os.write(1,b'1234'); time.sleep(30)"
        with self.synthetic_recorder(code), \
                patch.object(ld, "notify", side_effect=notification), \
                patch.object(ld, "transcribe_audio", AsyncMock()) as decode:
            self.session.task = asyncio.create_task(self.session.run())
            await asyncio.wait_for(entered.wait(), 2)
            self.session.update = Mock(side_effect=OSError(errno.ENOSPC, "No space"))
            self.session.request_cancel()
            self.session.request_cancel()
            await asyncio.wait_for(self.session.task, 2)
        self.assertTrue(self.session.cancelled)
        self.assertEqual(self.session.state, "cancelling")
        self.assertTrue(cancelled.is_set())
        self.assertIsNotNone(self.spawned[0].returncode)
        self.assertEqual(self.session.notification_tasks, [])
        self.assertEqual(completed, ["Cancelled"])
        decode.assert_not_called()
        ld.deliver.assert_not_called()

    async def test_blocked_transcribing_notification_does_not_delay_decode(self):
        entered = asyncio.Event()
        completed = []

        async def notification(conf, message):
            if message == "Transcribing":
                entered.set()
                await asyncio.Future()
            completed.append(message)

        async def decode(*args):
            await asyncio.wait_for(entered.wait(), 1)
            return "synthetic"

        with patch.object(ld, "capture_audio", side_effect=self.sample_capture), \
                patch.object(ld, "notify", side_effect=notification), \
                patch.object(ld, "transcribe_audio", side_effect=decode):
            await asyncio.wait_for(self.session.run(), 2)
        self.assertEqual(completed, ["Dictation inserted"])
        self.assertEqual(self.session.notification_tasks, [])
        ld.deliver.assert_awaited_once()

    async def assert_timed_out_notification_reaped(self, action):
        terminating = asyncio.Event()
        term_timeout = asyncio.Event()
        killed = asyncio.Event()
        release_reap = asyncio.Event()
        reaped = asyncio.Event()
        notifier_tasks = []
        waiter_tasks = []
        completed = []
        proc = Mock(returncode=None)
        before = asyncio.all_tasks()
        request = getattr(self.session, "request_" + action)

        async def communicate(*args):
            await asyncio.Future()

        async def wait():
            waiter_tasks.append(asyncio.current_task())
            if proc.returncode is None:
                terminating.set()
                await term_timeout.wait()
                raise TimeoutError
            await release_reap.wait()
            reaped.set()
            return proc.returncode

        def kill():
            proc.returncode = -signal.SIGKILL
            killed.set()

        proc.communicate = AsyncMock(side_effect=communicate)
        proc.wait = AsyncMock(side_effect=wait)
        proc.kill = Mock(side_effect=kill)

        async def notification(conf, message):
            if message == "Recording":
                notifier_tasks.append(asyncio.current_task())
                with contextlib.suppress(ld.ClientError):
                    await ld.run_tool(["FAKE"], timeout=.01)
            completed.append(message)

        async def capture(conf, stop, on_limit, on_ready):
            on_ready()
            await stop.wait()
            return b"1234"

        with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), \
                patch.object(ld, "notify", side_effect=notification), \
                patch.object(ld, "capture_audio", side_effect=capture), \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="synthetic")) as decode:
            self.session.task = asyncio.create_task(self.session.run())
            try:
                # Enter reaping through the helper timeout, before any cancellation.
                await asyncio.wait_for(terminating.wait(), 1)
                notifier = notifier_tasks[0]
                self.assertEqual(notifier.cancelling(), 0)
                request()
                await asyncio.sleep(0)
                term_timeout.set()
                await asyncio.wait_for(killed.wait(), 1)
                # Further requests during the KILL wait must also finish reaping.
                for _ in range(2):
                    request()
                    notifier.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(notifier.done())
                self.assertFalse(self.session.task.done())
                release_reap.set()
                await asyncio.wait_for(self.session.task, 2)
            finally:
                term_timeout.set()
                release_reap.set()
                if not self.session.task.done():
                    self.session.request_cancel()
                    await asyncio.wait_for(self.session.task, 2)

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertEqual(proc.wait.await_count, 2)
        self.assertTrue(reaped.is_set())
        self.assertTrue(all(task.done() for task in notifier_tasks + waiter_tasks))
        self.assertTrue(notifier_tasks[0].cancelled())
        self.assertEqual(self.session.notification_tasks, [])
        self.assertEqual(asyncio.all_tasks() - before, set())
        self.assertNotIn("Recording", completed)
        if action == "cancel":
            decode.assert_not_called()
            ld.deliver.assert_not_called()
            self.assertEqual(completed, ["Cancelled"])
        else:
            decode.assert_awaited_once_with(b"1234", self.session.config)
            ld.deliver.assert_awaited_once()
            self.assertEqual(completed[-1], "Dictation inserted")

    async def test_stop_after_notification_timeout_finishes_helper_reaping(self):
        await self.assert_timed_out_notification_reaped("stop")

    async def test_cancel_after_notification_timeout_finishes_helper_reaping(self):
        await self.assert_timed_out_notification_reaped("cancel")

    async def test_cancel_during_notification_drain_does_not_interrupt_helper_cleanup(self):
        entered = asyncio.Event()
        reaping = asyncio.Event()
        release = asyncio.Event()
        reaped = asyncio.Event()
        delivered = asyncio.Event()
        notifier_tasks = []
        completed = []

        async def notification(conf, message):
            if message == "Recording":
                notifier_tasks.append(asyncio.current_task())
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    reaping.set()
                    await release.wait()
                    reaped.set()
                    raise
            completed.append(message)

        async def capture(conf, stop, on_limit, on_ready):
            on_ready()
            await stop.wait()
            return b"1234"

        async def deliver(*args):
            delivered.set()
            return "typed"

        with patch.object(ld, "notify", side_effect=notification), \
                patch.object(ld, "capture_audio", side_effect=capture), \
                patch.object(ld, "deliver", side_effect=deliver), \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="synthetic")):
            self.session.task = asyncio.create_task(self.session.run())
            await asyncio.wait_for(entered.wait(), 1)
            self.session.request_stop()
            await asyncio.wait_for(reaping.wait(), 1)
            # Let delivery finish and block only on the notification drain.
            await asyncio.wait_for(delivered.wait(), 1)
            self.session.request_cancel()
            await asyncio.sleep(0)
            self.assertFalse(self.session.task.done())
            self.assertEqual(notifier_tasks[0].cancelling(), 1)
            release.set()
            await asyncio.wait_for(self.session.task, 2)
        self.assertTrue(reaped.is_set())
        self.assertTrue(all(task.done() for task in notifier_tasks))
        self.assertEqual(completed[-1], "Dictation inserted")
        self.assertNotIn("Recording", completed)
        self.assertNotIn("Cancelled", completed)
        self.assertFalse(self.session.cancelled)
        self.assertEqual(self.session.delivery_result, "typed")
        self.assertEqual(self.session.notification_tasks, [])

    async def test_late_cancel_preserves_each_completed_delivery_outcome(self):
        outcomes = {"typed": "Dictation inserted", "copied": "Dictation copied to clipboard",
                    "empty": "No speech recognized"}
        for result, message in outcomes.items():
            with self.subTest(result=result):
                self.session = ld.Session(config(insertion_delay_ms=0), self.states.append)
                notifying = asyncio.Event()
                release = asyncio.Event()
                completed = []

                async def notification(conf, text):
                    if text == message:
                        notifying.set()
                        await release.wait()
                    completed.append(text)

                with patch.object(ld, "capture_audio", side_effect=self.sample_capture), \
                        patch.object(ld, "transcribe_audio", AsyncMock(return_value="synthetic")), \
                        patch.object(ld, "deliver", AsyncMock(return_value=result)) as delivery, \
                        patch.object(ld, "notify", side_effect=notification):
                    self.session.task = asyncio.create_task(self.session.run())
                    try:
                        await asyncio.wait_for(notifying.wait(), 1)
                        self.session.request_cancel()
                        self.session.request_cancel()
                        self.assertFalse(self.session.cancelled)
                        self.assertEqual(self.session.task.cancelling(), 0)
                        self.assertEqual(self.session.delivery_result, result)
                    finally:
                        release.set()
                        await asyncio.wait_for(self.session.task, 2)
                    delivery.assert_awaited_once()
                self.assertEqual(completed[-1], message)
                self.assertNotIn("Cancelled", completed)
                self.assertEqual(self.session.notification_tasks, [])

    async def test_stop_during_recorder_spawn_does_not_publish_late_recording(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            command = [*args[:4], json.dumps(["/usr/bin/python3", "-c",
                       "import os,time; os.write(1,b'1234'); time.sleep(30)"])]
            proc = await real_spawn(*command, **kwargs)
            self.spawned.append(proc)
            # Observe real PCM while the spawn coroutine is still pending.
            sample = await proc.stdout.readexactly(4)
            proc.stdout.feed_data(sample)
            entered.set()
            await release.wait()
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=spawn), \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="synthetic")) as decode:
            self.session.task = asyncio.create_task(self.session.run())
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(self.session.state, "starting")
            self.session.request_stop()
            release.set()
            await asyncio.wait_for(self.session.task, 2)
        self.assertNotIn("recording", self.states)
        self.assertNotIn("Recording", [call.args[-1] for call in ld.notify.call_args_list])
        decode.assert_awaited_once_with(b"1234", self.session.config)
        self.assertIsNotNone(self.spawned[0].returncode)

    async def test_disabled_notifications_cover_session_success_and_cancel(self):
        with patch.object(ld, "notify", new=self.real_notify), \
                patch.object(ld, "run_tool", AsyncMock()) as tool, \
                patch.object(ld, "capture_audio", side_effect=self.sample_capture), \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="synthetic")):
            await self.session.run()
            self.session.request_cancel()
            await self.session.run()
        tool.assert_not_called()
        self.assertEqual(self.session.notification_tasks, [])

    async def test_decode_failure_does_not_deliver(self):
        with patch.object(ld, "capture_audio", side_effect=self.sample_capture), \
                patch.object(ld, "transcribe_audio", AsyncMock(side_effect=ld.ClientError("timed out"))):
            await self.session.run()
            ld.deliver.assert_not_called()
            self.assertEqual(ld.notify.call_args.args[-1], "timed out")

    async def test_empty_recording_prevents_decode(self):
        with patch.object(ld, "capture_audio", AsyncMock(return_value=b"")), \
                patch.object(ld, "transcribe_audio", AsyncMock()) as decode:
            await self.session.run()
            decode.assert_not_called()

    async def test_rapid_stop_during_probe_never_opens_recorder(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def probe(*args):
            entered.set()
            await release.wait()

        with patch.object(ld, "probe_server", side_effect=probe), \
                patch.object(ld, "capture_audio", AsyncMock()) as capture:
            self.session.task = asyncio.create_task(self.session.run())
            await entered.wait()
            self.session.request_stop()
            release.set()
            await self.session.task
            capture.assert_not_called()

    async def test_stop_captures_target_once(self):
        self.session.set_state("recording")
        self.session.request_stop()
        self.session.request_stop()
        await self.session.focus_task
        ld.FocusGuard.capture.assert_awaited_once()

    async def test_failed_state_publication_does_not_prevent_stop_or_decode(self):
        entered = asyncio.Event()

        async def capture(conf, stop, on_limit, on_ready):
            on_ready()
            entered.set()
            await stop.wait()
            return b"1234"

        self.session.update = Mock(side_effect=OSError(errno.ENOSPC, "No space"))
        with patch.object(ld, "capture_audio", side_effect=capture), \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="synthetic")) as decode:
            self.session.task = asyncio.create_task(self.session.run())
            await asyncio.wait_for(entered.wait(), 1)
            self.session.request_stop()
            self.assertTrue(self.session.stop.is_set())
            self.assertEqual(self.session.state, "stopping")
            await asyncio.wait_for(self.session.task, 2)
        decode.assert_awaited_once_with(b"1234", self.session.config)
        ld.deliver.assert_awaited_once()
        ld.FocusGuard.capture.assert_awaited_once()

    async def test_failed_cancel_publication_during_decode_prevents_delivery(self):
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        async def decode(*args):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        with patch.object(ld, "capture_audio", side_effect=self.sample_capture), \
                patch.object(ld, "transcribe_audio", side_effect=decode):
            self.session.task = asyncio.create_task(self.session.run())
            await asyncio.wait_for(entered.wait(), 1)
            self.session.update = Mock(side_effect=OSError(errno.ENOSPC, "No space"))
            self.session.request_cancel()
            self.assertGreater(self.session.task.cancelling(), 0)
            await asyncio.wait_for(self.session.task, 2)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(ld.notify.call_args.args[-1], "Cancelled")
        ld.deliver.assert_not_called()

    async def test_control_socket_stop_and_cancel_survive_failed_state_writes(self):
        capturing = asyncio.Event()
        decoding = asyncio.Event()
        runtime = Mock()
        runtime.write_state.side_effect = OSError(errno.ENOSPC, "No space")
        session_class = ld.Session

        def make_session(conf, update):
            self.session = session_class(conf, update)
            return self.session

        async def capture(conf, stop, on_limit, on_ready):
            on_ready()
            capturing.set()
            await stop.wait()
            return b"1234"

        async def decode(*args):
            decoding.set()
            await asyncio.Future()

        async def command(path, action):
            reader, writer = await asyncio.open_unix_connection(str(path))
            try:
                writer.write(json.dumps({"command": action, "session": "synthetic"}).encode() + b"\n")
                await writer.drain()
                return json.loads(await asyncio.wait_for(reader.readline(), 1))
            finally:
                writer.close()
                await writer.wait_closed()

        loop = asyncio.get_running_loop()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(ld, "Session", side_effect=make_session), \
                patch.object(ld, "capture_audio", side_effect=capture), \
                patch.object(ld, "transcribe_audio", side_effect=decode), \
                patch.object(loop, "add_signal_handler"), \
                patch.object(loop, "remove_signal_handler"):
            path = Path(directory) / "control.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listener.listen(16)
            listener.setblocking(False)
            owner = asyncio.create_task(ld.serve_owner(self.session.config, listener, runtime,
                                                      "synthetic"))
            try:
                await asyncio.wait_for(capturing.wait(), 1)
                self.assertEqual(await command(path, "status"), {"state": "recording"})
                self.assertEqual(await command(path, "stop"), {"state": "stopping"})
                await asyncio.wait_for(decoding.wait(), 1)
                self.assertEqual(await command(path, "cancel"), {"state": "cancelling"})
                await asyncio.wait_for(owner, 2)
            finally:
                if not owner.done():
                    self.session.request_cancel()
                    await asyncio.wait_for(owner, 2)
                listener.close()
        ld.deliver.assert_not_called()
        self.assertTrue(self.session.stop.is_set())
        self.assertTrue(self.session.cancelled)
        self.assertEqual(self.session.notification_tasks, [])
        self.assertEqual([call.args[0] for call in runtime.write_state.call_args_list],
                         ["recording", "stopping", "transcribing", "cancelling"])

    async def test_success_state_and_notification(self):
        with patch.object(ld, "capture_audio", side_effect=self.sample_capture), \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="synthetic secret")):
            await self.session.run()
        self.assertEqual(self.states, ["recording", "transcribing", "delivering"])
        self.assertNotIn("synthetic secret", repr(ld.notify.call_args_list))
        ld.deliver.assert_awaited_once()


def fake_owner(args):
    """Real IPC owner with all microphone, ASR, and desktop work substituted."""
    async def capture(conf, stop, on_limit, on_ready):
        on_ready()
        await stop.wait()
        return b"\0" * 4

    async def decode(*args):
        await asyncio.sleep(30)
        return "synthetic"

    runtime = ld.Runtime()
    with (runtime.path / "test-starts").open("a") as stream:
        stream.write(args[2] + "\n")
    with patch.object(ld, "probe_server", AsyncMock()), \
            patch.object(ld, "capture_audio", side_effect=capture), \
            patch.object(ld, "transcribe_audio", side_effect=decode), \
            patch.object(ld, "run_tool", AsyncMock(return_value=b"{}")), \
            patch.object(ld.FocusGuard, "start", AsyncMock()):
        return ld.owner_main(args)


def run_concurrent_control(directory, command, gate, results):
    os.environ["XDG_RUNTIME_DIR"] = directory
    original = ld.owner_command

    def fake_command(*args):
        command_line = original(*args)
        return [sys.executable, str(Path(__file__).resolve()), "--fake-owner", *command_line[3:]]

    ld.owner_command = fake_command
    gate.wait(5)
    try:
        results.put(ld.control(command, config()))
    except Exception as error:
        results.put({"error": str(error)})


def synthetic_recorder_owner(connection):
    async def run():
        real_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            command = [*args[:4], json.dumps(["/usr/bin/python3", "-c", "import time; time.sleep(30)"])]
            proc = await real_spawn(*command, **kwargs)
            connection.send(proc.pid)
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=spawn):
            await ld.capture_audio(config(), asyncio.Event(), Mock())

    asyncio.run(run())


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dictate-test-")
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.temp.name,
                                          "XDG_CONFIG_HOME": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runtime = ld.Runtime()

    def test_private_modes(self):
        self.assertEqual(self.runtime.path.stat().st_mode & 0o777, 0o700)
        ld.control("status", config())
        for file in self.runtime.path.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_stale_state_with_reused_pid_never_signals(self):
        self.runtime.state_path.write_text(json.dumps({"state": "recording", "pid": os.getpid()}))
        self.runtime.socket_path.write_text("stale")
        with patch.object(os, "kill") as kill:
            self.assertEqual(ld.control("cancel", config()), {"state": "idle"})
            kill.assert_not_called()
        self.assertFalse(self.runtime.state_path.exists())

    def test_symlink_lock_rejected(self):
        (self.runtime.path / "command.lock").symlink_to(Path(self.temp.name) / "elsewhere")
        with self.assertRaises(ld.ClientError):
            ld.control("start", config())

    def test_public_runtime_rejected(self):
        os.chmod(self.temp.name, 0o755)
        with self.assertRaisesRegex(ld.ClientError, "0700"):
            ld.Runtime()

    def test_symlink_runtime_rejected(self):
        link = Path(self.temp.name) / "alias"
        link.symlink_to(self.runtime.path)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(link)}):
            with self.assertRaises(ld.ClientError):
                ld.Runtime()

    def test_missing_runtime_fails_cleanly(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}):
            with self.assertRaises(ld.ClientError):
                ld.Runtime()

    def test_busy_owner_with_stale_pid_does_not_duplicate(self):
        fd = ld.checked_file(self.runtime.path / "owner.lock")
        self.addCleanup(os.close, fd)
        self.assertTrue(ld.try_lock(fd))
        self.runtime.write_state("starting", "00000000-0000-0000-0000-000000000000")
        with patch.object(subprocess, "Popen") as popen, self.assertRaises(ld.ClientError):
            ld.control("start", config())
        popen.assert_not_called()

    def test_failed_spawn_cleans_state(self):
        with patch.object(subprocess, "Popen", side_effect=OSError):
            with self.assertRaises(ld.ClientError):
                ld.control("start", config())
        self.assertFalse(self.runtime.socket_path.exists())
        self.assertFalse(self.runtime.state_path.exists())

    def test_status_json_idle(self):
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(ld.main(["status", "--json"]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"state": "idle"})

    def test_status_json_error(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(ld.main(["status", "--json"]), 1)
        self.assertEqual(json.loads(stdout.getvalue())["state"], "error")

    def test_bad_config_is_rejected_before_new_owner(self):
        bad = Path(self.temp.name) / "bad.toml"
        bad.write_text("invalid = [")
        with patch("sys.stderr", new_callable=io.StringIO), \
                patch.object(ld, "start_owner") as start:
            self.assertEqual(ld.main(["--config", str(bad), "start"]), 1)
            start.assert_not_called()

    def test_status_and_cancel_work_with_broken_config(self):
        self.addCleanup(self.cancel_owner)
        self.concurrent_commands(["start"])
        self.wait_state({"recording"})
        bad = Path(self.temp.name) / "bad.toml"
        bad.write_text("invalid = [")
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(ld.main(["--config", str(bad), "status", "--json"]), 0)
            self.assertEqual(json.loads(stdout.getvalue()), {"state": "recording"})
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(ld.main(["--config", str(bad), "cancel"]), 0)
        self.wait_state({"idle"})

    def wait_state(self, states):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = ld.control("status", config())
            if result["state"] in states:
                return result["state"]
            time.sleep(.02)
        self.fail(f"Session did not reach {states}")

    def concurrent_commands(self, commands):
        ctx = multiprocessing.get_context("fork")
        gate = ctx.Event()
        results = ctx.Queue()
        children = [ctx.Process(target=run_concurrent_control,
                               args=(self.temp.name, command, gate, results)) for command in commands]
        for child in children:
            child.start()
        gate.set()
        replies = [results.get(timeout=8) for child in children]
        for child in children:
            child.join(timeout=5)
            self.assertEqual(child.exitcode, 0)
        results.close()
        return replies

    def cancel_owner(self):
        with contextlib.suppress(ld.ClientError):
            ld.control("cancel", config())
        self.wait_state({"idle"})

    def test_rapid_starts_share_one_owner_then_decode_cancel(self):
        self.addCleanup(self.cancel_owner)
        replies = self.concurrent_commands(["start"] * 8)
        self.assertTrue(all("error" not in reply for reply in replies), replies)
        self.wait_state({"recording"})
        self.assertEqual(len((self.runtime.path / "test-starts").read_text().splitlines()), 1)
        token = self.runtime.token()
        self.assertEqual(ld.control("start", config()), {"state": "recording"})
        self.assertEqual(self.runtime.token(), token)
        ld.control("stop", config())
        self.wait_state({"transcribing"})
        self.assertEqual(ld.control("toggle", config()), {"state": "transcribing"})
        ld.control("cancel", config())
        self.wait_state({"idle"})
        self.assertFalse(self.runtime.socket_path.exists())

    def test_concurrent_toggles_stop_first_session_without_duplicates(self):
        self.addCleanup(self.cancel_owner)
        replies = self.concurrent_commands(["toggle"] * 8)
        self.assertTrue(all("error" not in reply for reply in replies), replies)
        self.wait_state({"idle", "transcribing"})
        if ld.control("status", config())["state"] != "idle":
            self.cancel_owner()

    def test_cancel_recording_via_socket(self):
        self.addCleanup(self.cancel_owner)
        self.concurrent_commands(["start"])
        self.wait_state({"recording"})
        ld.control("cancel", config())
        self.wait_state({"idle"})

    def test_stale_session_token_rejected(self):
        self.addCleanup(self.cancel_owner)
        self.concurrent_commands(["start"])
        self.wait_state({"recording"})
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1)
            client.connect(str(self.runtime.socket_path))
            client.sendall(b'{"command":"cancel","session":"old"}\n')
            result = json.loads(client.recv(4096))
        self.assertEqual(result["error"], "Session mismatch")
        self.assertEqual(ld.control("status", config())["state"], "recording")

    def test_oversized_control_request_does_not_break_owner(self):
        self.addCleanup(self.cancel_owner)
        self.concurrent_commands(["start"])
        self.wait_state({"recording"})
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2)
            client.connect(str(self.runtime.socket_path))
            client.sendall(b"x" * 5000 + b"\n")
            self.assertEqual(client.recv(10), b"")
        self.assertEqual(ld.control("status", config())["state"], "recording")

    def test_owner_killed_abruptly_allows_restart(self):
        self.addCleanup(self.cancel_owner)
        original = ld.owner_command

        def fake_command(*args):
            command_line = original(*args)
            return [sys.executable, str(Path(__file__).resolve()), "--fake-owner", *command_line[3:]]

        with patch.object(ld, "owner_command", side_effect=fake_command):
            ld.control("start", config())
            owner = ld._owner_children[-1]
            self.wait_state({"recording"})
            old = self.runtime.token()
            owner.kill()
            owner.wait(timeout=3)
            self.assertTrue(self.runtime.socket_path.exists())
            ld.control("start", config())
            self.wait_state({"recording"})
            self.assertNotEqual(self.runtime.token(), old)

    def test_recorder_dies_when_its_owner_is_killed(self):
        ctx = multiprocessing.get_context("fork")
        receive, send = ctx.Pipe(duplex=False)
        owner = ctx.Process(target=synthetic_recorder_owner, args=(send,))
        owner.start()
        try:
            self.assertTrue(receive.poll(3))
            recorder_pid = receive.recv()
            descriptor = os.pidfd_open(recorder_pid)
            try:
                owner.kill()
                owner.join(timeout=3)
                poller = select.poll()
                poller.register(descriptor, select.POLLIN)
                self.assertTrue(poller.poll(2000), "Synthetic recorder survived its owner")
            finally:
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                os.close(descriptor)
        finally:
            if owner.is_alive():
                owner.kill()
                owner.join(timeout=3)
            receive.close()
            send.close()

    def test_signal_owner_releases_lock(self):
        self.addCleanup(self.cancel_owner)
        original = ld.owner_command
        children = []
        real_popen = subprocess.Popen

        def fake_command(*args):
            command_line = original(*args)
            return [sys.executable, str(Path(__file__).resolve()), "--fake-owner", *command_line[3:]]

        def popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc

        with patch.object(ld, "owner_command", side_effect=fake_command), \
                patch.object(subprocess, "Popen", side_effect=popen):
            ld.control("start", config())
        self.wait_state({"recording"})
        children[0].send_signal(signal.SIGTERM)
        self.assertEqual(children[0].wait(timeout=5), 0)
        self.assertEqual(ld.control("status", config()), {"state": "idle"})

    def test_command_lock_has_deadline(self):
        fd = ld.checked_file(self.runtime.path / "command.lock")
        self.addCleanup(os.close, fd)
        ld.try_lock(fd)
        with patch.object(ld, "CONTROL_TIMEOUT", .03), self.assertRaisesRegex(ld.ClientError, "busy"):
            ld.control("status", config())


class LauncherTests(unittest.TestCase):
    def test_symlink_uses_repo_and_explicit_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "local-dictate"
            launcher.symlink_to(Path(__file__).resolve().parents[1] / "bin" / "local-dictate")
            env = {**os.environ, "LOCAL_DICTATE_PYTHON": "/usr/bin/python3"}
            result = subprocess.run([str(launcher), "--help"], capture_output=True, text=True,
                                    env=env, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("transcribe", result.stdout)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake-owner":
        raise SystemExit(fake_owner(sys.argv[2:]))
    unittest.main()
