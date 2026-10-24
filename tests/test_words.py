"""Personal corrections, entirely isolated from installed settings and desktop tools."""

from __future__ import annotations

import contextlib
import fcntl
import io
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_dictate as ld
import word_rules as wr


def concurrent_add(path, source, gate, results):
    gate.wait(5)
    try:
        wr.add(Path(path), source, source.upper())
        results.put("ok")
    except Exception as error:
        results.put(type(error).__name__)


class MatchingTests(unittest.TestCase):
    def rules(self, **pairs):
        return wr.Rules(replacements=tuple(pairs.items()))

    def test_case_boundaries_and_punctuation(self):
        rules = self.rules(Orange="Omarchy")
        self.assertEqual(rules.apply("ORANGE orange (Orange), orange's orange-like"),
                         "Omarchy Omarchy (Omarchy), Omarchy's Omarchy-like")
        for text in ("oranges", "myOrange", "_Orange", "orange2", "éOrange", "Orangeé",
                     "中Orange", "Orange中", "Orange\u0301", "Orange\u203f", "\u203fOrange"):
            with self.subTest(text=text):
                self.assertEqual(rules.apply(text), text)

    def test_longest_phrase_and_one_pass(self):
        rules = self.rules(**{"New": "old", "York": "town", "New York": "NYC",
                              "NYC": "city", "A": "B", "B": "C"})
        self.assertEqual(rules.apply("New York, NEW; York. A B NYC"),
                         "NYC, old; town. B C city")
        # A second application is a separate operation and may cascade.
        self.assertEqual(rules.apply(rules.apply("A")), "C")

    def test_longer_invalid_match_falls_back_to_shorter(self):
        rules = self.rules(**{"New": "N", "New York": "NY"})
        self.assertEqual(rules.apply("New Yorkers"), "N Yorkers")

    def test_exact_phrase_spacing_and_unmatched_text(self):
        rules = self.rules(**{"New York": "NYC"})
        self.assertEqual(rules.apply("x  New  York\nNew York!"), "x  New  York\nNYC!")
        self.assertEqual(ld.process_text("x  New  York\n", rules), "x NYC")

    def test_literals_are_not_regex_shell_or_backreferences(self):
        rules = self.rules(**{"a+b": "$HOME; $(false) \\1", "[x]": "${x}"})
        self.assertEqual(rules.apply("a+b aaab [x]"), "$HOME; $(false) \\1 aaab ${x}")

    def test_normalized_accents_and_unchanged_surroundings(self):
        rules = self.rules(**{"café": "Coffee", "é": "E"})
        self.assertEqual(rules.apply("cafe\u0301! CAFÉ; x e\u0301 y"), "Coffee! Coffee; x E y")
        original = "untouched z\u0301 cafe\u0301x"
        self.assertEqual(rules.apply(original), original)
        self.assertEqual(self.rules(cafe="coffee").apply("cafe\u0301"), "cafe\u0301")

    def test_canonical_combining_order(self):
        rules = self.rules(**{"a\u0315\u0300": "X"})
        self.assertEqual(rules.apply("a\u0300\u0315!"), "X!")

    def test_casefold_length_changes_do_not_shift_offsets_or_split(self):
        rules = self.rules(**{"STRASSE": "Street", "s": "S", "i": "I"})
        self.assertEqual(rules.apply("Straße Straße! s ß İ i"), "Street Street! S ß İ I")
        self.assertEqual(self.rules(ss="SS").apply("ß ss"), "SS SS")

    def test_ambiguous_fold_and_canonical_collisions(self):
        for pairs in ((("Straße", "x"), ("STRASSE", "y")),
                      (("é", "x"), ("e\u0301", "y")),
                      (("Orange", "x"), ("orange", "y"))):
            with self.subTest(pairs=pairs), self.assertRaisesRegex(wr.WordsError, "Ambiguous"):
                wr.Rules(replacements=pairs)

    def test_empty_disabled_missing_are_noop(self):
        self.assertEqual(wr.Rules().apply("Orange"), "Orange")
        self.assertEqual(wr.Rules(False, (("Orange", "Omarchy"),)).apply("Orange"), "Orange")
        self.assertEqual(wr.load(None).apply("Orange"), "Orange")

    def test_literal_controls_types_and_sizes(self):
        for text in ("", " x", "x ", "x\ny", "x\ty", "x\x1by", "x\0y", "x\u202ey",
                     "x\u2028y", "x\ud800y", "x\u0085y", "x\u00a0y", "x" * 513, 2):
            for source in (True, False):
                pairs = ((text, "x"),) if source else (("x", text),)
                with self.subTest(text=repr(text), source=source), self.assertRaises(wr.WordsError):
                    wr.Rules(replacements=pairs)
        with self.assertRaises(wr.WordsError):
            wr.Rules(replacements=(("x" * 129, "y"),))
        with self.assertRaises(wr.WordsError):
            wr.Rules("yes")
        with self.assertRaises(wr.WordsError):
            wr.Rules(replacements=tuple((str(i), "x") for i in range(wr.MAX_RULES + 1)))

    def test_output_expansion_is_bounded(self):
        rules = self.rules(x="y" * 512)
        with self.assertRaisesRegex(wr.WordsError, "output limit"):
            rules.apply("x " * 3000)
        for rules in (rules, wr.Rules(False), wr.Rules()):
            with self.assertRaisesRegex(wr.WordsError, "input"):
                rules.apply("x" * (wr.MAX_TEXT_CHARS + 1))
        with self.assertRaisesRegex(wr.WordsError, "output limit"):
            with patch.object(wr, "MAX_OUTPUT_BYTES", 10):
                self.rules(x="é" * 6).apply("x")

    def test_sanitization_surrounds_correction_step(self):
        self.assertEqual(ld.process_text("\x1bOrange\n\tOrange\u202e", self.rules(Orange="Omarchy")),
                         "Omarchy Omarchy")
        fake = Mock(apply=Mock(return_value="target\n\t\x1btext"))
        self.assertEqual(ld.process_text("raw\ntext", fake), "target text")
        fake.apply.assert_called_once_with("raw text")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private" / "words.toml"

    def write(self, raw):
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        self.path.write_bytes(raw.encode("utf-8"))
        self.path.chmod(0o600)

    def test_missing_read_does_not_create_storage(self):
        self.assertEqual(wr.load(self.path), wr.Rules())
        self.assertFalse(self.path.parent.exists())

    def test_missing_dictionary_in_existing_nonprivate_directory_is_noop(self):
        self.path.parent.mkdir(mode=0o755)
        self.assertEqual(wr.load(self.path), wr.Rules())
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o755)
        with self.assertRaises(wr.WordsError):
            wr.add(self.path, "a", "b")

    def test_missing_dictionary_under_symlinked_directory_is_noop_but_mutation_refused(self):
        self.path.parent.mkdir(mode=0o700)
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.path.parent, target_is_directory=True)
        selected = alias / self.path.name
        self.assertEqual(wr.load(selected), wr.Rules())
        self.assertEqual(list(self.path.parent.iterdir()), [])
        with self.assertRaises(wr.WordsError):
            wr.add(selected, "a", "b")
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_dangling_dictionary_symlink_is_not_missing(self):
        self.path.parent.mkdir(mode=0o700)
        self.path.symlink_to(self.path.with_name("absent.toml"))
        with self.assertRaises(wr.WordsError):
            wr.load(self.path)
        self.assertTrue(self.path.is_symlink())

    def test_roundtrip_private_atomic_idempotent_and_existing_rules(self):
        wr.add(self.path, "Orange", "Omarchy")
        first_inode = self.path.stat().st_ino
        wr.add(self.path, "orange", "Omarchy")
        self.assertEqual(self.path.stat().st_ino, first_inode)
        wr.add(self.path, "New York", "NYC")
        self.assertNotEqual(self.path.stat().st_ino, first_inode)
        self.assertEqual(wr.load(self.path).replacements,
                         (("Orange", "Omarchy"), ("New York", "NYC")))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.with_suffix(".toml.lock").stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),
                         ["words.toml", "words.toml.lock"])

    def test_explicit_replace_and_remove_casefolded(self):
        wr.add(self.path, "Straße", "Street")
        with self.assertRaisesRegex(wr.WordsError, "--replace"):
            wr.add(self.path, "STRASSE", "Road")
        self.assertEqual(wr.load(self.path).replacements, (("Straße", "Street"),))
        wr.add(self.path, "strasse", "Road", replace=True)
        wr.remove(self.path, "STRASSE")
        self.assertEqual(wr.load(self.path), wr.Rules())

    def test_invalid_unknown_ambiguous_and_no_content_in_diagnostics(self):
        for raw in ('private = "secret"', '[replacements]\nsecret = [',
                    'enabled = "secret"', 'replacements = "secret"',
                    '[replacements]\nsecret = 2',
                    '[replacements]\nsecret = "x"\nSECRET = "y"',
                    '[replacements]\nsecret = "x"\nsecret = "y"',
                    '[replacements]\nsecret = { x = "y" }'):
            self.write(raw)
            before = self.path.read_bytes()
            with self.subTest(raw=raw), self.assertRaises(wr.WordsError) as error:
                wr.add(self.path, "new", "value")
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(before, self.path.read_bytes())

    def test_file_and_rule_limits(self):
        self.write("#" * (wr.MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(wr.WordsError, "size limit"):
            wr.load(self.path)
        self.write('[replacements]\n')
        with patch.object(wr, "MAX_FILE_BYTES", 20), self.assertRaises(wr.WordsError):
            wr.add(self.path, "new", "target")
        self.assertEqual(self.path.read_text(), '[replacements]\n')

    def test_refuse_file_permissions_symlink_hardlink_fifo_and_foreign_owner(self):
        self.write("")
        self.path.chmod(0o644)
        with self.assertRaises(wr.WordsError):
            wr.load(self.path)
        self.path.chmod(0o600)
        target = self.path.with_name("target")
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(wr.WordsError):
            wr.add(self.path, "a", "b")
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaises(wr.WordsError):
            wr.load(self.path)
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(wr.WordsError):
            wr.load(self.path)
        self.path.unlink()
        target.rename(self.path)
        real_fstat = os.fstat

        def foreign(fd):
            info = real_fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return info
            return Mock(st_mode=info.st_mode, st_uid=os.getuid() + 1, st_nlink=1)

        with patch.object(os, "fstat", side_effect=foreign), self.assertRaises(wr.WordsError):
            wr.load(self.path)

    def test_refuse_parent_and_lock_links_or_permissions(self):
        self.write("")
        self.path.parent.chmod(0o755)
        with self.assertRaises(wr.WordsError):
            wr.load(self.path)
        self.path.parent.chmod(0o700)
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.path.parent)
        with self.assertRaises(wr.WordsError):
            wr.load(alias / "words.toml")
        lock = self.path.with_suffix(".toml.lock")
        lock.symlink_to(self.path)
        with self.assertRaises(wr.WordsError):
            wr.add(self.path, "a", "b")
        lock.unlink()
        lock.write_text("")
        lock.chmod(0o644)
        with self.assertRaises(wr.WordsError):
            wr.add(self.path, "a", "b")

    def test_failed_atomic_replace_preserves_file_and_removes_temp(self):
        wr.add(self.path, "a", "b")
        raw = self.path.read_bytes()
        with patch.object(os, "replace", side_effect=OSError), self.assertRaises(wr.WordsError):
            wr.add(self.path, "c", "d")
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse(list(self.path.parent.glob(".words-*")))

    def test_external_valid_edit_during_commit_is_not_lost(self):
        wr.add(self.path, "a", "b")
        serialize = wr._serialize

        def edit(rules):
            self.path.write_bytes(serialize(wr.Rules(replacements=(("external", "value"),))))
            return serialize(rules)

        with patch.object(wr, "_serialize", side_effect=edit), self.assertRaisesRegex(wr.WordsError, "changed"):
            wr.add(self.path, "c", "d")
        self.assertEqual(wr.load(self.path).replacements, (("external", "value"),))

    def test_concurrent_updates_preserve_all_rules(self):
        context = multiprocessing.get_context("fork")
        gate = context.Event()
        results = context.Queue()
        children = [context.Process(target=concurrent_add, args=(str(self.path), f"word{i}", gate, results))
                    for i in range(8)]
        try:
            for child in children:
                child.start()
            gate.set()
            self.assertEqual([results.get(timeout=5) for _ in children], ["ok"] * len(children))
            self.assertEqual(len(wr.load(self.path).replacements), len(children))
        finally:
            for child in children:
                child.join(5)
                if child.is_alive():
                    child.kill()
                    child.join(1)
            results.close()

    def test_lock_deadline_is_bounded(self):
        wr.add(self.path, "a", "b")
        with self.path.with_suffix(".toml.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with patch.object(wr, "LOCK_TIMEOUT", .03), self.assertRaisesRegex(wr.WordsError, "busy"):
                wr.add(self.path, "c", "d")
            self.assertLess(time.monotonic() - started, .3)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Path(self.temp.name) / ".config/local-dictate/config.toml"
        self.path = self.settings.with_name("words.toml")
        self.env = patch.dict(os.environ, {"HOME": self.temp.name, "XDG_CONFIG_HOME": "",
                                           "XDG_RUNTIME_DIR": "", "HYPRLAND_INSTANCE_SIGNATURE": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("control", "probe_server", "capture_audio", "deliver", "run_tool"):
            mock = patch.object(ld, name, side_effect=AssertionError("desktop or ASR work forbidden"))
            mock.start()
            self.addCleanup(mock.stop)

    def cli(self, *arguments, stdin=""):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err, patch.object(sys, "stdin", io.StringIO(stdin)):
            status = ld.main(list(arguments))
        return status, out.getvalue(), err.getvalue()

    def test_roundtrip_headless_and_disabled_preservation(self):
        self.assertEqual(self.cli("words", "check")[0], 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.cli("words", "add", "Orange", "Omarchy")[0], 0)
        self.assertIn('"Orange" = "Omarchy"', self.cli("words", "list")[1])
        self.assertEqual(self.cli("words", "test", "ORANGE oranges")[1], "Omarchy oranges\n")
        self.assertEqual(self.cli("words", "test", stdin="Orange\n")[1], "Omarchy\n")
        self.assertEqual(self.cli("words", "disable")[0], 0)
        self.assertEqual(self.cli("words", "test", "Orange")[1], "Orange\n")
        self.assertIn("disabled", self.cli("words", "add", "York", "NYC")[1])
        self.assertEqual(len(wr.load(self.path).replacements), 2)
        self.assertEqual(self.cli("words", "enable")[0], 0)
        self.assertEqual(self.cli("words", "remove", "ORANGE")[0], 0)
        self.assertEqual(self.cli("words", "test", "Orange York")[1], "Orange NYC\n")

    def test_add_conflict_warning_and_replace(self):
        result = self.cli("words", "add", "Orange", "Omarchy")
        self.assertIn("ordinary uses", result[1])
        self.assertEqual(self.cli("words", "add", "orange", "Fruit")[0], 1)
        self.assertEqual(self.cli("words", "add", "orange", "Fruit", "--replace")[0], 0)
        self.assertEqual(self.cli("words", "test", "Orange")[1], "Fruit\n")

    def test_config_path_and_empty_unset_xdg_fallback(self):
        for value in (None, ""):
            if value is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = value
            self.assertEqual(self.cli("words", "path")[1].strip(), str(self.path))
        os.environ["XDG_CONFIG_HOME"] = str(Path(self.temp.name) / "xdg")
        self.assertEqual(self.cli("words", "path")[1].strip(),
                         str(Path(self.temp.name) / "xdg/local-dictate/words.toml"))
        selected = Path(self.temp.name) / "other/config.toml"
        selected.parent.mkdir(mode=0o700)
        selected.write_text("invalid desktop config = [")
        args = ("--config", str(selected), "words")
        self.assertEqual(self.cli(*args, "add", "a", "b")[0], 0)
        self.assertEqual(self.cli(*args, "test", "a")[1], "b\n")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.cli(*args, "path")[1].strip(), str(selected.with_name("words.toml")))

    def test_invalid_file_and_excessive_preview_do_not_echo_content(self):
        wr.add(self.path, "a", "b")
        self.path.write_text("secret-name = [")
        result = self.cli("words", "check")
        self.assertEqual(result[0], 1)
        self.assertNotIn("secret", result[2])
        self.path.unlink()
        result = self.cli("words", "test", stdin=" " * (wr.MAX_TEXT_CHARS + 1))
        self.assertEqual(result[0], 1)
        self.assertEqual(result[1], "")

    def test_invalid_unicode_preview_fails_without_echoing_content(self):
        result = self.cli("words", "test", stdin="private\udcfftext")
        self.assertEqual(result[0], 1)
        self.assertEqual(result[1], "")
        self.assertIn("valid Unicode", result[2])
        self.assertNotIn("private", result[2])


class GuiTests(unittest.TestCase):
    def setUp(self):
        StorageTests.setUp(self)
        helper = patch.object(wr.shutil, "which", return_value="/usr/bin/zenity")
        helper.start()
        self.addCleanup(helper.stop)

    def dialog(self, output, returncode=0):
        def run(argv, **kwargs):
            self.assertEqual(argv[0], "zenity")
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            kwargs["stdout"].write(output)
            return Mock(returncode=returncode)
        return run

    def test_gui_optional_dependency(self):
        with patch.object(wr.shutil, "which", return_value=None), self.assertRaisesRegex(wr.WordsError, "words add"):
            wr.gui(self.path)

    def test_gui_cancel_has_no_changes_or_lock(self):
        with patch.object(wr.subprocess, "run", side_effect=self.dialog(b"", 1)):
            self.assertEqual(wr.gui(self.path), 0)
        self.assertFalse(self.path.parent.exists())

    def test_gui_two_field_literal_add_and_disabled_warning(self):
        wr.mutate(self.path, lambda before: wr.Rules(False))
        with patch.object(wr.subprocess, "run", side_effect=self.dialog(b"Orange\x1fOmarchy\n")) as run, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wr.gui(self.path), 0)
        self.assertIn("--separator=\x1f", run.call_args.args[0])
        self.assertIn("disabled", out.getvalue())
        self.assertFalse(wr.load(self.path).enabled)
        self.assertEqual(wr.load(self.path).replacements, (("Orange", "Omarchy"),))

    def test_gui_malformed_or_error_response_cannot_commit(self):
        for raw in (b"a|b", b"a\x1fb\x1fc", b"a\x1fb\nextra", b"a\x1fb\x1b", b"\xff", b"a" * 4097):
            with self.subTest(raw=raw[:30]), patch.object(wr.subprocess, "run", side_effect=self.dialog(raw)), \
                    self.assertRaises(wr.WordsError):
                wr.gui(self.path)
            self.assertFalse(self.path.exists())
        with patch.object(wr.subprocess, "run", side_effect=self.dialog(b"", 2)), self.assertRaises(wr.WordsError):
            wr.gui(self.path)
        with patch.object(wr.subprocess, "run", side_effect=FileNotFoundError), self.assertRaises(wr.WordsError):
            wr.gui(self.path)
        with patch.object(wr.subprocess, "run", side_effect=subprocess.TimeoutExpired("zenity", 300)), \
                self.assertRaises(wr.WordsError):
            wr.gui(self.path)

    def test_gui_overwrite_cancel_preserves_bytes(self):
        wr.add(self.path, "Orange", "old")
        before = self.path.read_bytes()
        with patch.object(wr, "_dialog", side_effect=[b"Orange\x1fnew\n", None]):
            self.assertEqual(wr.gui(self.path), 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_gui_stale_confirmation_refuses_and_preserves_concurrent_updates(self):
        wr.add(self.path, "Orange", "old")

        def dialogs(arguments):
            if "--forms" in arguments:
                return b"Orange\x1fnew\n"
            # No mutation lock is held while the user is deciding.
            wr.add(self.path, "Other", "kept")
            wr.add(self.path, "Orange", "concurrent", replace=True)
            return b""

        with patch.object(wr, "_dialog", side_effect=dialogs), self.assertRaisesRegex(wr.WordsError, "changed"):
            wr.gui(self.path)
        self.assertEqual(wr.load(self.path).replacements, (("Orange", "concurrent"), ("Other", "kept")))

    def test_gui_confirmed_overwrite_preserves_unrelated_update(self):
        wr.add(self.path, "Orange", "old")

        def dialogs(arguments):
            if "--forms" in arguments:
                return b"Orange\x1fnew\n"
            wr.add(self.path, "Other", "kept")
            return b""

        with patch.object(wr, "_dialog", side_effect=dialogs), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wr.gui(self.path), 0)
        self.assertEqual(wr.load(self.path).replacements, (("Orange", "new"), ("Other", "kept")))


if __name__ == "__main__":
    unittest.main()
