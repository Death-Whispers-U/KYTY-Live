import json
import tempfile
import unittest
from pathlib import Path

from kyty_live import history

UI_ROW = {"id": "PPSA03528", "title": "GTA III", "run": "20261011-215028_PPSA03528",
          "outcome": "stopped by a fatal error (EXIT_HALT)", "crashed": True, "seconds": 9.0,
          "first_frame_s": 2.0, "max_frame": 73, "last_fps": 4.0, "stalled": False,
          "counts": {"critical": 5, "error": 0, "warning": 0, "info": 0},
          "critical": ["Unhandled host exception: pc=0x90000819e"], "errors": [],
          "snapshot": None}

CLI_ROW = {"id": "PPSA14427", "title": "CRYMACHINA", "seconds": 60.5, "alive_at_end": True, "exit": None,
           "first_frame_s": 2.0, "max_frame": 2595, "last_fps": 48.0, "stalled": False,
           "counts": {"critical": {"stub_called": 4}, "info": {"missing_file": 3}},
           "critical": ["Game called missing X"], "errors": [], "screenshot": "C:/x/end.png"}


class NormalizeTest(unittest.TestCase):
    def test_ui_row_passes_through(self):
        r = history.normalize_row(UI_ROW, "s1")
        self.assertTrue(r["crashed"])
        self.assertEqual(r["counts"]["critical"], 5)

    def test_cli_row_is_converted(self):
        r = history.normalize_row(CLI_ROW, "s0")
        self.assertFalse(r["crashed"])
        self.assertEqual(r["outcome"], "ran the full time")
        self.assertEqual(r["counts"], {"critical": 4, "error": 0, "warning": 0, "info": 3})
        self.assertEqual(r["snapshot"], "/api/sweepfiles/s0/PPSA14427/end.png")

    def test_cli_snapshot_dropped_when_file_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(history.normalize_row(CLI_ROW, "s0", Path(d))["snapshot"])


class CompareTest(unittest.TestCase):
    def test_fixed_regressed_and_deltas(self):
        a = [dict(UI_ROW), dict(UI_ROW, id="G2", title="Two", crashed=False, outcome="ran the full time",
                                max_frame=1000, last_fps=60.0, counts={"critical": 1, "error": 0, "warning": 0, "info": 0})]
        b = [dict(UI_ROW, crashed=False, outcome="ran the full time", max_frame=1195, last_fps=20.0,
                  counts={"critical": 4, "error": 0, "warning": 0, "info": 0}),
             dict(UI_ROW, id="G2", title="Two", crashed=True, max_frame=10, last_fps=None),
             dict(UI_ROW, id="G3", title="Three")]
        c = {x["id"]: x for x in history.compare(a, b)}
        self.assertEqual(c["PPSA03528"]["change"], "fixed")
        self.assertEqual(c["PPSA03528"]["frames_delta"], 1195 - 73)
        self.assertEqual(c["PPSA03528"]["critical_delta"], -1)
        self.assertEqual(c["G2"]["change"], "regressed")
        self.assertEqual(c["G3"]["change"], "new")

    def test_cancelled_run_is_incomplete_not_fixed(self):
        cancelled = dict(UI_ROW, crashed=False, outcome="stopped from Kyty Live", max_frame=1346)
        c = history.compare([UI_ROW], [history.normalize_row(cancelled, "s2")])
        self.assertEqual(c[0]["change"], "incomplete")

    def test_repeats_compare_survival_rates(self):
        ok = dict(UI_ROW, crashed=False, outcome="ran the full time", max_frame=1000)
        bad = dict(UI_ROW, crashed=True, max_frame=100)
        c = history.compare([bad, bad, bad, ok], [ok, ok, ok, bad])[0]
        self.assertEqual(c["change"], "improved")
        self.assertEqual(c["before"]["outcome"], "1/4 ran the full time")
        self.assertEqual(c["after"]["outcome"], "3/4 ran the full time")
        self.assertEqual(c["before"]["runs"], 4)
        self.assertEqual(history.compare([ok, ok], [bad, ok])[0]["change"], "worse")
        self.assertEqual(history.compare([bad, bad], [ok, ok])[0]["change"], "fixed")
        self.assertEqual(history.compare([ok, ok], [bad, bad])[0]["change"], "regressed")

    def test_same(self):
        c = history.compare([UI_ROW], [UI_ROW])
        self.assertEqual(c[0]["change"], "same")


class ReportTest(unittest.TestCase):
    def test_markdown_lists_changes(self):
        a = {"id": "s1", "meta": {"flags": [], "commit": "abc"}, "rows": [UI_ROW]}
        b = {"id": "s2", "meta": {"flags": ["--redzone"], "commit": "abc"},
             "rows": [dict(UI_ROW, crashed=False, outcome="ran the full time", max_frame=1195)]}
        md = history.report_markdown(a, b)
        self.assertIn("--redzone", md)
        self.assertIn("| GTA III |", md)
        self.assertIn("fixed", md)
        self.assertIn("1 fixed", md)
        self.assertIn("build abc", md)

    def test_duplicate_titles_get_ids(self):
        a = {"id": "s1", "meta": {}, "rows": [UI_ROW, dict(UI_ROW, id="PPSA99999")]}
        md = history.report_markdown(a, a)
        self.assertIn("| GTA III (PPSA03528) |", md)
        self.assertIn("| GTA III (PPSA99999) |", md)
        self.assertNotIn("unknown build", md)


class StoreTest(unittest.TestCase):
    def test_list_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "20261011-214517").mkdir()
            (root / "20261011-214517" / "summary.json").write_text(json.dumps([UI_ROW]))
            (root / "20261011-220816").mkdir()
            (root / "20261011-220816" / "summary.json").write_text(json.dumps([UI_ROW, UI_ROW]))
            (root / "20261011-220816" / "meta.json").write_text(json.dumps({"flags": ["--redzone"], "seconds": 60}))
            sweeps = history.list_sweeps(root)
            self.assertEqual([s["id"] for s in sweeps], ["20261011-220816", "20261011-214517"])
            self.assertEqual(sweeps[0]["games"], 2)
            self.assertEqual(sweeps[0]["crashed"], 2)
            self.assertEqual(sweeps[0]["meta"]["flags"], ["--redzone"])
            self.assertEqual(sweeps[1]["meta"]["flags"], [])
            loaded = history.load_sweep(root, "20261011-214517")
            self.assertEqual(len(loaded["rows"]), 1)
            with self.assertRaises(ValueError):
                history.load_sweep(root, "../etc")


if __name__ == "__main__":
    unittest.main()
