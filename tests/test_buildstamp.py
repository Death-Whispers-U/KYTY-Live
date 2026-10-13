import os
import tempfile
import unittest
from pathlib import Path

from kyty_live.buildstamp import read_build_stamp


class BuildStampTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exe = Path(self.tmp.name) / "kyty_emulator.exe"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, payload):
        # The stamp sits somewhere in the middle of a large binary.
        self.exe.write_bytes(os.urandom(4096) + payload + os.urandom(4096))

    def test_reads_the_clean_stamp_the_binary_was_built_from(self):
        self.write(b"\x00Source build 27a260d\x00")
        self.assertEqual(read_build_stamp(self.exe), {"build": "27a260d", "dirty": False})

    def test_reports_a_dirty_build(self):
        self.write(b"\x00Source build 95c061f-dirty\x00")
        self.assertEqual(read_build_stamp(self.exe), {"build": "95c061f", "dirty": True})

    def test_a_binary_without_a_stamp_gives_nothing(self):
        self.write(b"no stamp here")
        self.assertIsNone(read_build_stamp(self.exe))

    def test_a_missing_binary_gives_nothing(self):
        self.assertIsNone(read_build_stamp(self.exe))

    def test_a_replaced_binary_is_read_again(self):
        self.write(b"Source build 1111111\x00")
        self.assertEqual(read_build_stamp(self.exe)["build"], "1111111")
        self.write(b"Source build 2222222\x00 and more bytes so the size changes")
        self.assertEqual(read_build_stamp(self.exe)["build"], "2222222")


if __name__ == "__main__":
    unittest.main()
