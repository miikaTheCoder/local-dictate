"""Session and WAV correction integration, with no real capture or delivery."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_dictate as ld
import word_rules as wr
from websockets.asyncio.server import serve


class SessionWordsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Path(self.temp.name) / "config.toml"
        self.path = self.settings.with_name("words.toml")
        self.config = dataclasses.replace(ld.load_config(self.settings), notifications=False, insertion_delay_ms=0)
        self.session = ld.Session(self.config, Mock())
        for obj, name, value in ((ld, "probe_server", AsyncMock()),
                                 (ld, "notify", AsyncMock()),
                                 (ld, "deliver", AsyncMock(return_value="typed")),
                                 (ld.FocusGuard, "start", AsyncMock()),
                                 (ld.FocusGuard, "close", AsyncMock()),
                                 (ld.FocusGuard, "capture", AsyncMock())):
            mock = patch.object(obj, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    async def test_snapshot_before_capture_applies_once_despite_file_edit(self):
        wr.add(self.path, "Orange", "Omarchy")
        wr.add(self.path, "Omarchy", "other")

        async def capture(*args):
            wr.add(self.path, "Orange", "changed", replace=True)
            return b"1234"

        with patch.object(ld, "capture_audio", side_effect=capture), \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="ORANGE oranges\n")):
            await self.session.run()
        ld.deliver.assert_awaited_once_with("Omarchy oranges", self.config, self.session.focus)
        self.assertEqual(wr.load(self.path).apply("Orange"), "changed")
        self.assertNotIn("Orange", repr(ld.notify.call_args_list))

    async def test_invalid_snapshot_prevents_probe_capture_decode_and_delivery(self):
        self.path.write_text('private word = [')
        self.path.chmod(0o600)
        with patch.object(ld, "capture_audio", AsyncMock()) as capture, \
                patch.object(ld, "transcribe_audio", AsyncMock()) as decode:
            await self.session.run()
        ld.probe_server.assert_not_called()
        capture.assert_not_called()
        decode.assert_not_called()
        ld.deliver.assert_not_called()
        self.assertNotIn("private word", repr(ld.notify.call_args_list))

    async def test_output_limit_fails_before_delivery_without_content_notification(self):
        wr.add(self.path, "x", "y" * 512)
        with patch.object(ld, "capture_audio", AsyncMock(return_value=b"1234")), \
                patch.object(ld, "transcribe_audio", AsyncMock(return_value="x " * 3000)):
            await self.session.run()
        ld.deliver.assert_not_called()
        self.assertEqual(ld.notify.call_args.args[-1], "Corrected text exceeds the output limit")

    async def test_cancel_pending_decode_discards_corrected_output(self):
        wr.add(self.path, "Orange", "Omarchy")
        decoding = asyncio.Event()
        reaped = asyncio.Event()

        async def decode(*args):
            decoding.set()
            try:
                await asyncio.Future()
            finally:
                reaped.set()

        with patch.object(ld, "capture_audio", AsyncMock(return_value=b"1234")), \
                patch.object(ld, "transcribe_audio", side_effect=decode):
            self.session.task = asyncio.create_task(self.session.run())
            await asyncio.wait_for(decoding.wait(), 1)
            self.session.request_cancel()
            await asyncio.wait_for(self.session.task, 1)
        self.assertTrue(reaped.is_set())
        ld.deliver.assert_not_called()
        self.assertEqual(ld.notify.call_args.args[-1], "Cancelled")

    async def test_missing_and_disabled_rules_preserve_sanitized_result(self):
        for disabled in (False, True):
            if disabled:
                wr.add(self.path, "Orange", "Omarchy")
                wr.mutate(self.path, lambda before: wr.Rules(False, before.replacements))
            session = ld.Session(self.config, Mock())
            with patch.object(ld, "capture_audio", AsyncMock(return_value=b"1234")), \
                    patch.object(ld, "transcribe_audio", AsyncMock(return_value="Orange\n fruit")):
                await session.run()
            self.assertEqual(ld.deliver.call_args.args[0], "Orange fruit")

    async def test_doctor_reports_invalid_rules_without_capture(self):
        self.path.write_text('private term = [')
        self.path.chmod(0o600)
        with patch.object(ld, "Runtime", side_effect=ld.ClientError("no runtime")), \
                patch.object(ld, "capture_audio", AsyncMock()) as capture, \
                patch.object(ld, "run_tool", AsyncMock()) as tool, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(await ld.doctor(self.config), 1)
        capture.assert_not_called()
        tool.assert_not_called()
        self.assertIn("corrections: Cannot read correction TOML", stdout.getvalue())
        self.assertNotIn("private term", stdout.getvalue())


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Path(self.temp.name) / "config.toml"
        self.path = self.settings.with_name("words.toml")
        self.wav = Path(self.temp.name) / "synthetic.wav"
        with wave.open(str(self.wav), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * 16)
        env = patch.dict(os.environ, {"HOME": self.temp.name, "XDG_CONFIG_HOME": "",
                                      "XDG_RUNTIME_DIR": self.temp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_wav_corrects_stdout_only_and_uses_same_processor(self):
        wr.add(self.path, "Orange", "Omarchy")
        with patch.object(ld, "transcribe_audio", AsyncMock(return_value="ORANGE\nfruit")), \
                patch.object(ld, "deliver", AsyncMock()) as deliver, \
                patch.object(ld, "run_tool", AsyncMock()) as tool, \
                patch.object(ld, "capture_audio", AsyncMock()) as capture, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(ld.main(["--config", str(self.settings), "transcribe", str(self.wav)]), 0)
        self.assertEqual(stdout.getvalue(), "Omarchy fruit\n")
        deliver.assert_not_called()
        capture.assert_not_called()
        tool.assert_not_called()

    def test_missing_dictionary_beneath_symlinked_config_directory(self):
        actual = Path(self.temp.name) / "actual"
        actual.mkdir(mode=0o700)
        (actual / "config.toml").write_text("notifications = false\n")
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        selected = alias / "config.toml"
        config = ld.new_session_config(selected)
        self.assertFalse(config.notifications)
        self.assertEqual(config.words_path, str(alias / "words.toml"))
        with patch.object(ld, "transcribe_audio", AsyncMock(return_value="Orange\nfruit")) as decode, \
                patch.object(ld, "deliver", AsyncMock()) as deliver, \
                patch.object(ld, "run_tool", AsyncMock()) as tool, \
                patch.object(ld, "capture_audio", AsyncMock()) as capture, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(ld.main(["--config", str(selected), "transcribe", str(self.wav)]), 0)
        self.assertEqual(stdout.getvalue(), "Orange fruit\n")
        decode.assert_awaited_once()
        deliver.assert_not_called()
        capture.assert_not_called()
        tool.assert_not_called()
        self.assertEqual(sorted(path.name for path in actual.iterdir()), ["config.toml"])

    def test_invalid_rules_prevent_wav_asr_and_new_owner(self):
        self.path.write_text("private term = [")
        self.path.chmod(0o600)
        with patch.object(ld, "transcribe_audio", AsyncMock()) as decode, \
                patch.object(ld, "read_wav") as read, \
                patch.object(ld, "start_owner") as start, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ld.main(["--config", str(self.settings), "transcribe", str(self.wav)]), 1)
            self.assertEqual(ld.main(["--config", str(self.settings), "start"]), 1)
        decode.assert_not_called()
        read.assert_not_called()
        start.assert_not_called()

    def test_status_stop_cancel_toggle_existing_owner_ignore_broken_rules(self):
        self.settings.write_text("broken config = [")
        self.path.write_text("broken words = [")
        self.path.chmod(0o600)
        runtime = ld.Runtime()
        owner_fd = ld.checked_file(runtime.path / "owner.lock")
        self.addCleanup(os.close, owner_fd)
        self.assertTrue(ld.try_lock(owner_fd))
        for action in ("status", "stop", "cancel", "toggle"):
            with patch.object(ld, "send_control", return_value={"state": "recording"}) as control, \
                    patch.object(ld, "load_config", side_effect=AssertionError("must not load config")), \
                    patch.object(ld, "load_words", side_effect=AssertionError("must not load rules")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ld.main(["--config", str(self.settings), action]), 0)
                control.assert_called_once_with(unittest.mock.ANY, action)

    def test_owner_argv_contains_path_not_dictionary_contents(self):
        wr.add(self.path, "private-source", "private-target")
        config = ld.new_session_config(self.settings)
        command = ld.owner_command(config, 10, 11, "token")
        self.assertIn(str(self.path), command[-1])
        self.assertNotIn("private-source", repr(command))
        self.assertNotIn("private-target", repr(command))
        self.assertNotIn("replacements", json.loads(command[-1]))
        with self.assertRaises(ld.ClientError):
            ld.Config.from_dict({"words_path": "not-a-desktop-setting"})


class NativeProtocolWordsTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_cli_wav_corrects_raw_ws_result_and_sends_done(self):
        done = asyncio.Event()

        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({"text": "ORANGE oranges"}))
            self.assertEqual(await ws.recv(), "Done")
            done.set()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as directory:
                settings = Path(directory) / "config.toml"
                settings.write_text(f'server_url = "ws://127.0.0.1:{port}"\n')
                wr.add(settings.with_name("words.toml"), "Orange", "Omarchy")
                wav_path = Path(directory) / "synthetic.wav"
                with wave.open(str(wav_path), "wb") as wav:
                    wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    wav.writeframes(b"\0\0" * 16)
                process = await asyncio.create_subprocess_exec(
                    "/usr/bin/python3", str(Path(ld.__file__)), "--config", str(settings),
                    "transcribe", str(wav_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env={**os.environ, "HOME": directory, "XDG_CONFIG_HOME": "", "XDG_RUNTIME_DIR": "",
                         "PYTHONPATH": os.pathsep.join(str(path) for path in sys.path if path)})
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
                finally:
                    await ld.reap_process(process)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout, b"Omarchy oranges\n")
                self.assertEqual(stderr, b"")
                await asyncio.wait_for(done.wait(), 1)


if __name__ == "__main__":
    unittest.main()
