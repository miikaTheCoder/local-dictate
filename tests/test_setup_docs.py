"""Check documented commands without downloads or desktop/service actions."""

import io
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SetupDocumentationTests(unittest.TestCase):
    def test_documented_bash_blocks_parse_without_execution(self):
        count = 0
        for filename in ("README.md", "docs/server-setup.md"):
            content = (ROOT / filename).read_text()
            for snippet in re.findall(r"```bash\n(.*?)\n```", content, re.S):
                count += 1
                result = subprocess.run(
                    ["/bin/bash", "-n"], input=snippet, capture_output=True,
                    text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, filename + ": " + result.stderr)
        self.assertGreater(count, 0)

    def run_unpack(self, member):
        if sys.version_info < (3, 12):
            self.skipTest("Documented safe extraction requires Python 3.12+")
        content = (ROOT / "docs/server-setup.md").read_text()
        match = re.search(r"<<'PY'\n(.*?)\nPY", content, re.S)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory(prefix="local-dictate-extract-") as directory:
            stage = Path(directory)
            archive_path = stage / "expected.tar.bz2"
            with tarfile.open(archive_path, "w:bz2") as archive:
                if member.isreg():
                    data = b"synthetic release fixture"
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
                else:
                    archive.addfile(member)
            result = subprocess.run(
                [sys.executable, "-", directory, "expected"],
                input=match.group(1), capture_output=True, text=True, timeout=5,
            )
            files = [p.relative_to(stage).as_posix() for p in stage.rglob("*")]
            return result, files

    def test_documented_extraction_accepts_synthetic_release_root(self):
        result, files = self.run_unpack(tarfile.TarInfo("expected/file.txt"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("extracted/expected/expected/file.txt", files)

    def test_documented_extraction_refuses_traversal_absolute_and_other_root(self):
        for name in ("expected/../../escape", "/absolute", "unexpected/file.txt"):
            with self.subTest(name=name):
                result, files = self.run_unpack(tarfile.TarInfo(name))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unexpected archive path", result.stderr)
                self.assertNotIn("escape", files)

    def test_documented_extraction_refuses_escaping_symlink(self):
        member = tarfile.TarInfo("expected/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../../escape"
        result, files = self.run_unpack(member)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("extracted/expected/expected/link", files)

    def test_documented_extraction_refuses_special_file(self):
        member = tarfile.TarInfo("expected/pipe")
        member.type = tarfile.FIFOTYPE
        result, files = self.run_unpack(member)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("extracted/expected/expected/pipe", files)


if __name__ == "__main__":
    unittest.main()
