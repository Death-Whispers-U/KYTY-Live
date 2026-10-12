import unittest

from kyty_live.classifier import parse_hot_line


class ParseHotLineTest(unittest.TestCase):
    def test_patched(self):
        e = parse_hot_line("HotHLE: patched fzyMKs9kim0 sceKernelWaitEqueue (3 slots)\n")
        self.assertEqual(e, {"event": "patched", "nid": "fzyMKs9kim0", "name": "sceKernelWaitEqueue", "slots": 3,
                             "text": "patched sceKernelWaitEqueue (3 slots)"})

    def test_restored(self):
        e = parse_hot_line("HotHLE: restored fzyMKs9kim0 sceKernelWaitEqueue (3 slots)")
        self.assertEqual(e["event"], "restored")
        self.assertEqual(e["nid"], "fzyMKs9kim0")

    def test_not_imported(self):
        e = parse_hot_line("HotHLE: 6UgtwV+0zb4 scePthreadCreate is not imported yet; it will be used when it is")
        self.assertEqual(e["event"], "waiting")
        self.assertEqual(e["name"], "scePthreadCreate")

    def test_active_generation(self):
        e = parse_hot_line("HotHLE: kyty_hot.dll #4 active with 2 functions")
        self.assertEqual(e["event"], "active")
        self.assertEqual(e["generation"], 4)
        self.assertEqual(e["count"], 2)

    def test_patch_log_and_other_lines(self):
        self.assertEqual(parse_hot_line("HotHLE patch: v2: called 500 times")["event"], "patch_log")
        self.assertEqual(parse_hot_line("HotHLE: cannot load x.dll (error 193)")["event"], "error")
        self.assertEqual(parse_hot_line("HotHLE: watching E:/hot/kyty_hot.dll")["event"], "info")
        self.assertIsNone(parse_hot_line("Initialized: Audio"))



class DiagnosticsTest(unittest.TestCase):
    def test_reads_clang_cl_errors(self):
        from kyty_live.hot import parse_diagnostics

        out = "\n".join([
            r"E:\kyty-live\hot\patch.cpp(14,9): error: use of undeclared identifier 'x'",
            "   14 |         x = 1;",
            r"E:\kyty-live\hot\patch.cpp(3,1): warning: unused function 'f' [-Wunused-function]",
        ])
        d = parse_diagnostics(out)
        self.assertEqual(len(d), 2)
        self.assertEqual(d[0], {"file": "patch.cpp", "line": 14, "col": 9, "kind": "error",
                                "msg": "use of undeclared identifier 'x'"})
        self.assertEqual(d[1]["kind"], "warning")


if __name__ == "__main__":
    unittest.main()
