import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.file_io import atomic_write_text


class AtomicWriteTests(unittest.TestCase):
    def test_failed_replace_preserves_original_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.cfg"
            path.write_text("original")
            with patch("src.file_io.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    atomic_write_text(path, "replacement")
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_successful_replace_preserves_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.cfg"
            path.write_text("original")
            path.chmod(0o640)
            atomic_write_text(path, "replacement")
            self.assertEqual(path.read_text(), "replacement")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
