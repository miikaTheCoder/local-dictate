"""Private, bounded personal corrections. No ASR or desktop delivery code."""

from __future__ import annotations

import array
import contextlib
import dataclasses
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unicodedata

MAX_FILE_BYTES = 256 * 1024
MAX_RULES = 256
MAX_SOURCE_CHARS = 128
MAX_TARGET_CHARS = 512
MAX_TEXT_CHARS = 256 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
LOCK_TIMEOUT = 2.0
_ABSENT = object()
_UNCONFIRMED = object()


class WordsError(Exception):
    """A diagnostic safe to display without dictionary or transcript contents."""


def key(text: str) -> str:
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", text).casefold())


def word_character(char: str) -> bool:
    category = unicodedata.category(char)
    return category[0] in "LMN" or category == "Pc"


def valid_literal(value: str, maximum: int) -> None:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or value.strip() != value
            or any(unicodedata.category(c)[0] == "C"
                   or (c.isspace() and c != " ") for c in value)):
        raise WordsError("Correction literals must be bounded nonempty text without controls or outer spaces")


def check_input(text: str) -> None:
    if len(text) > MAX_TEXT_CHARS:
        raise WordsError("Correction input exceeds the text limit")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise WordsError("Correction input must be valid Unicode text") from None
    if size > MAX_OUTPUT_BYTES:
        raise WordsError("Correction input exceeds the text limit")


@dataclasses.dataclass(frozen=True)
class Rules:
    enabled: bool = True
    replacements: tuple[tuple[str, str], ...] = ()
    _trie: dict = dataclasses.field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if type(self.enabled) is not bool or len(self.replacements) > MAX_RULES:
            raise WordsError("Invalid correction settings or too many rules")
        trie = {}
        seen = set()
        for source, target in self.replacements:
            valid_literal(source, MAX_SOURCE_CHARS)
            valid_literal(target, MAX_TARGET_CHARS)
            normalized = key(source)
            if normalized in seen:
                raise WordsError("Ambiguous case-equivalent or canonically equivalent correction sources")
            seen.add(normalized)
            node = trie
            for char in normalized:
                node = node.setdefault(char, {})
            node[None] = target
        object.__setattr__(self, "_trie", trie)

    def apply(self, text: str) -> str:
        # Bound the input even when rules are disabled, before normalization.
        check_input(text)
        if not self.enabled or not self.replacements:
            return text
        # Normalize base-plus-mark clusters, retaining original offsets. A folded
        # expansion or a combining sequence can never be partially replaced.
        parts = []
        boundaries = array.array("i", [0])
        position = 0
        while position < len(text):
            end = position + 1
            while end < len(text) and unicodedata.category(text[end])[0] == "M":
                end += 1
            folded = key(text[position:end])
            parts.append(folded)
            boundaries.extend([-1] * (len(folded) - 1))
            boundaries.append(end)
            position = end
        folded_text = "".join(parts)
        pieces = []
        output_bytes = 0

        def append(piece):
            nonlocal output_bytes
            size = len(piece.encode("utf-8"))
            if output_bytes + size > MAX_OUTPUT_BYTES:
                raise WordsError("Corrected text exceeds the output limit")
            output_bytes += size
            pieces.append(piece)

        cursor = 0
        untouched = 0
        while cursor < len(folded_text):
            original = boundaries[cursor]
            if original < 0 or (original and word_character(text[original - 1])):
                cursor += 1
                continue
            node = self._trie
            scan = cursor
            best = None
            while scan < len(folded_text) and folded_text[scan] in node:
                node = node[folded_text[scan]]
                scan += 1
                end = boundaries[scan]
                if (None in node and end >= 0
                        and (end == len(text) or not word_character(text[end]))):
                    best = (scan, end, node[None])
            if best is None:
                cursor += 1
                continue
            scan, end, replacement = best
            append(text[untouched:original])
            append(replacement)
            untouched = end
            cursor = scan
        append(text[untouched:])
        return "".join(pieces)


def _private(info, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or (not directory and info.st_nlink != 1)):
        raise WordsError("Correction storage must be private, owned by you, and free of links")


@contextlib.contextmanager
def _parent(path: Path, create=False):
    fd = None
    try:
        if create:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            try:
                path.lstat()
            except FileNotFoundError:
                # An absent optional dictionary must not constrain config paths.
                # lstat keeps dangling dictionary symlinks from counting as absent.
                yield None
                return
        try:
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            if create:
                raise
            yield None
            return
        if not create:
            try:
                os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                # Existing client configs may live in ordinary directories. A
                # missing dictionary imposes no new permission requirement.
                yield None
                return
        _private(os.fstat(fd), directory=True)
        yield fd
    except OSError:
        raise WordsError("Cannot access private correction storage") from None
    finally:
        if fd is not None:
            os.close(fd)


def _read(parent_fd: int, name: str) -> Rules:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent_fd)
    except FileNotFoundError:
        return Rules()
    except OSError:
        raise WordsError("Cannot open private correction file") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _private(info)
        if info.st_size > MAX_FILE_BYTES:
            raise WordsError("Correction file exceeds the size limit")
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise WordsError("Correction file exceeds the size limit")
    try:
        values = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise WordsError("Cannot read correction TOML") from None
    if set(values) - {"enabled", "replacements"}:
        raise WordsError("Unknown correction setting")
    replacements = values.get("replacements", {})
    if not isinstance(replacements, dict):
        raise WordsError("Corrections require a replacements table")
    return Rules(values.get("enabled", True), tuple(replacements.items()))


def load(path: Path | None) -> Rules:
    if path is None:
        return Rules()
    with _parent(path) as fd:
        return Rules() if fd is None else _read(fd, path.name)


def _serialize(rules: Rules) -> bytes:
    lines = ["# Personal literal corrections. CLI edits rewrite this file.",
             "enabled = " + str(rules.enabled).lower(), "", "[replacements]"]
    for source, target in rules.replacements:
        lines.append(f"{json.dumps(source, ensure_ascii=False)} = {json.dumps(target, ensure_ascii=False)}")
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise WordsError("Correction file exceeds the size limit")
    return raw


def mutate(path: Path, change) -> Rules:
    """Serialize read-modify-write operations using a private, bounded lock."""
    with _parent(path, create=True) as parent_fd:
        lock_fd = os.open(path.name + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
                          | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
        try:
            _private(os.fstat(lock_fd))
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise WordsError("Another correction command is busy") from None
                    time.sleep(.02)
            before = _read(parent_fd, path.name)
            after = change(before)
            raw = _serialize(after)
            if before == after:
                return after
            # Use the held directory, including during atomic replacement. Do not
            # follow a directory renamed or replaced while this command runs.
            name = ".words-" + os.urandom(12).hex()
            fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
                         0o600, dir_fd=parent_fd)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if _read(parent_fd, path.name) != before:
                    raise WordsError("Correction file changed during the update; try again")
                os.replace(name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=parent_fd)
            return after
        finally:
            os.close(lock_fd)


def existing(rules: Rules, source: str):
    normalized = key(source)
    return next((target for old, target in rules.replacements if key(old) == normalized), _ABSENT)


def add(path: Path, source: str, target: str, replace=False, expected=_UNCONFIRMED) -> Rules:
    Rules(replacements=((source, target),))

    def change(before):
        old = existing(before, source)
        if expected is not _UNCONFIRMED and old != expected:
            raise WordsError("Correction changed while the dialog was open; try again")
        if old is not _ABSENT and old != target and not replace:
            raise WordsError("Correction exists with a different target; use --replace to overwrite")
        pairs = list(before.replacements)
        for index, (prior, _) in enumerate(pairs):
            if key(prior) == key(source):
                pairs[index] = (prior, target)
                break
        else:
            pairs.append((source, target))
        return Rules(before.enabled, tuple(pairs))

    return mutate(path, change)


def remove(path: Path, source: str) -> Rules:
    valid_literal(source, MAX_SOURCE_CHARS)
    return mutate(path, lambda before: Rules(before.enabled, tuple(
        (old, target) for old, target in before.replacements if key(old) != key(source))))


def _dialog(arguments: list[str]) -> bytes | None:
    try:
        # TemporaryFile is private and unlinked. Bound reads even if a helper
        # returns malformed output, and discard all helper stderr.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(["zenity", *arguments], stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.DEVNULL, timeout=300)
            if result.returncode == 1:
                return None
            if result.returncode != 0:
                raise WordsError("Correction dialog failed")
            output.seek(0)
            raw = output.read(4097)
            if len(raw) > 4096:
                raise WordsError("Correction dialog returned invalid output")
            return raw
    except (OSError, subprocess.TimeoutExpired):
        raise WordsError("Correction dialog unavailable or timed out; use words add") from None


def gui(path: Path) -> int:
    if shutil.which("zenity") is None:
        raise WordsError("Optional zenity is unavailable; use words add")
    load(path)  # Diagnose invalid storage without launching a dialog.
    raw = _dialog(["--forms", "--title=Add Local Dictate correction",
                   "--text=Literal replacements also change the ordinary word, including fruit names.",
                   "--add-entry=Heard", "--add-entry=Replace with", "--separator=\x1f"])
    if raw is None:
        return 0
    try:
        text = raw.decode("utf-8")
        if text.endswith("\n"):
            text = text[:-1]
        source, target = text.split("\x1f")
    except (UnicodeError, ValueError):
        raise WordsError("Correction dialog returned invalid output") from None
    Rules(replacements=((source, target),))
    latest = load(path)
    previous = existing(latest, source)
    if previous is not _ABSENT and previous != target:
        confirmed = _dialog(["--question", "--title=Replace correction?",
                             "--text=This correction already has a different target. Replace it?"])
        if confirmed is None:
            return 0
        if confirmed:
            raise WordsError("Correction dialog returned invalid output")
    rules = add(path, source, target, replace=True, expected=previous)
    print("Correction saved. Literal matches also replace ordinary uses of that word.")
    if not rules.enabled:
        print("Corrections are disabled; use words enable when ready.")
    return 0


def add_parser(sub):
    parser = sub.add_parser("words", help="manage personal literal corrections")
    commands = parser.add_subparsers(dest="words_command", required=True)
    add_command = commands.add_parser("add")
    add_command.add_argument("source")
    add_command.add_argument("target")
    add_command.add_argument("--replace", action="store_true")
    commands.add_parser("remove").add_argument("source")
    for command in ("list", "enable", "disable", "check", "path", "gui"):
        commands.add_parser(command)
    commands.add_parser("test", help="preview text from an argument or stdin").add_argument("text", nargs="?")


def run(args, path: Path, process_text) -> int:
    command = args.words_command
    if command == "path":
        print(path)
        return 0
    if command == "gui":
        return gui(path)
    if command == "add":
        rules = add(path, args.source, args.target, args.replace)
        print("Correction saved. Literal matches also replace ordinary uses of that word, including fruit names.")
    elif command == "remove":
        rules = remove(path, args.source)
        print("Correction removed (if present).")
    elif command in ("enable", "disable"):
        rules = mutate(path, lambda before: Rules(command == "enable", before.replacements))
        print("Corrections " + ("enabled." if rules.enabled else "disabled."))
    else:
        rules = load(path)
        if command == "list":
            print("enabled = " + str(rules.enabled).lower())
            for source, target in rules.replacements:
                print(json.dumps(source, ensure_ascii=False) + " = " + json.dumps(target, ensure_ascii=False))
        elif command == "check":
            print(f"Corrections valid: {len(rules.replacements)} rules, "
                  + ("enabled" if rules.enabled else "disabled"))
        elif command == "test":
            text = args.text if args.text is not None else sys.stdin.read(MAX_TEXT_CHARS + 1)
            # Check before sanitization so excessive whitespace cannot hide size.
            check_input(text)
            print(process_text(text, rules))
    if command == "add" and not rules.enabled:
        print("Corrections are disabled; use words enable when ready.")
    return 0
