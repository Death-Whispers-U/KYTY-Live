import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from kyty_live.freshsave import FreshSave

GAME = "PPSA21564"


class FreshSaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saves = base / "install" / "_SaveData"
        self.run_dir = base / "runs" / "20261012-200000_PPSA21564"
        self.run_dir.mkdir(parents=True)
        self.fs = FreshSave(self.saves)

    def tearDown(self):
        self.tmp.cleanup()

    def make_save(self, text):
        d = self.saves / GAME / "sce_sdmemory" / "1000"
        d.mkdir(parents=True)
        (d / "memory.dat").write_text(text, encoding="utf-8")

    def save_text(self):
        return (self.saves / GAME / "sce_sdmemory" / "1000" / "memory.dat").read_text(encoding="utf-8")

    def test_park_moves_the_save_away(self):
        self.make_save("progress")
        self.fs.park(GAME)
        self.assertFalse((self.saves / GAME).exists())
        self.assertTrue(self.fs.pending())

    def test_restore_puts_the_save_back_and_keeps_the_test_save_with_the_run(self):
        self.make_save("progress")
        self.fs.park(GAME)
        self.make_save("fresh run")  # what the game wrote during the test
        self.fs.restore(GAME, self.run_dir)
        self.assertEqual(self.save_text(), "progress")
        kept = self.run_dir / "fresh_save" / "sce_sdmemory" / "1000" / "memory.dat"
        self.assertEqual(kept.read_text(encoding="utf-8"), "fresh run")
        self.assertFalse(self.fs.pending())

    def test_a_game_without_a_save_gets_none_back(self):
        self.fs.park(GAME)
        self.make_save("fresh run")
        self.fs.restore(GAME, self.run_dir)
        self.assertFalse((self.saves / GAME).exists())
        self.assertTrue((self.run_dir / "fresh_save").exists())

    def test_restore_after_a_kyty_live_crash(self):
        self.make_save("progress")
        FreshSave(self.saves).park(GAME)
        self.make_save("fresh run")
        later = FreshSave(self.saves)  # a new Kyty Live process
        restored = later.recover(self.run_dir.parent)
        self.assertEqual(restored, [GAME])
        self.assertEqual(self.save_text(), "progress")
        self.assertFalse(later.pending())

    def test_nothing_is_overwritten_when_the_run_folder_already_has_a_save(self):
        self.make_save("progress")
        self.fs.park(GAME)
        self.make_save("fresh run")
        (self.run_dir / "fresh_save").mkdir()
        self.fs.restore(GAME, self.run_dir)
        self.assertEqual(self.save_text(), "progress")
        self.assertEqual(len([p for p in self.run_dir.iterdir() if p.name.startswith("fresh_save")]), 2)

    def test_a_save_folder_in_use_is_left_alone(self):
        self.make_save("progress")
        with mock.patch("kyty_live.freshsave.os.rename", side_effect=PermissionError("in use")):
            with self.assertRaises(RuntimeError):
                self.fs.park(GAME)
        self.assertEqual(self.save_text(), "progress")
        self.assertFalse(self.fs.pending())
        parked = self.saves / ".kytylive-parked"
        self.assertFalse(parked.exists() and any(p.name.startswith(GAME) for p in parked.iterdir()))

    def test_recovery_finds_a_save_moved_before_the_manifest_was_final(self):
        self.make_save("progress")
        real_rename = os.rename
        def rename_then_die(src, dst):
            real_rename(src, dst)
            raise KeyboardInterrupt  # Kyty Live stops right after the move
        with mock.patch("kyty_live.freshsave.os.rename", side_effect=rename_then_die):
            with self.assertRaises(KeyboardInterrupt):
                self.fs.park(GAME)
        restored = FreshSave(self.saves).recover(self.run_dir.parent)
        self.assertEqual(restored, [GAME])
        self.assertEqual(self.save_text(), "progress")

    def test_run_folder_on_another_drive_still_restores_the_save(self):
        self.make_save("progress")
        self.fs.park(GAME)
        self.make_save("fresh run")
        real_rename = os.rename
        def no_cross_drive(src, dst):
            if str(self.run_dir) in str(dst):
                raise OSError(17, "not same device")
            real_rename(src, dst)
        with mock.patch("kyty_live.freshsave.os.rename", side_effect=no_cross_drive):
            self.fs.restore(GAME, self.run_dir)
        self.assertEqual(self.save_text(), "progress")
        kept = [p for p in (self.saves / ".kytylive-parked").iterdir() if p.name.startswith("fresh-")]
        self.assertEqual(len(kept), 1)
        self.assertFalse(self.fs.pending())

    def test_park_twice_is_refused(self):
        self.make_save("progress")
        self.fs.park(GAME)
        with self.assertRaises(RuntimeError):
            self.fs.park(GAME)


if __name__ == "__main__":
    unittest.main()
