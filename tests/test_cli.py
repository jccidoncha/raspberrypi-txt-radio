import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "quick_mode.py"), *args],
                              capture_output=True, text=True, cwd=ROOT)

    def test_empty_txt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.txt"
            path.write_bytes(b"")
            result = self.invoke(str(path), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["bytes"], 0)
        self.assertEqual(report["data_frames"], 0)
        self.assertIs(report["radio_transmitted"], False)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(str(Path(directory) / "missing.txt"), "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_wrong_extension(self):
        result = self.invoke("anything.bin", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".txt", result.stderr)

    def test_real_mode_not_available(self):
        self.assertNotEqual(self.invoke("anything.txt").returncode, 0)
