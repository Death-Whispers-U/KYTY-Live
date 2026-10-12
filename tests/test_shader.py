import unittest

from kyty_live.classifier import parse_shader_line

NAME = "ps_4aa5db68601dcd19_9c1f04d27e3b55a0"


class ParseShaderLineTest(unittest.TestCase):
    def test_translated(self):
        e = parse_shader_line(f"ShaderOverride: translated {NAME}\n")
        self.assertEqual(e, {"event": "translated", "name": NAME, "stage": "ps", "hash": "4aa5db68601dcd19",
                             "error": ""})

    def test_applied_reverted(self):
        self.assertEqual(parse_shader_line(f"ShaderOverride: applied {NAME}")["event"], "applied")
        self.assertEqual(parse_shader_line(f"ShaderOverride: reverted {NAME}")["event"], "reverted")

    def test_rejected_keeps_error(self):
        e = parse_shader_line(f"ShaderOverride: rejected {NAME}: assembly failed: line 12: Expected operand")
        self.assertEqual(e["event"], "rejected")
        self.assertEqual(e["name"], NAME)
        self.assertEqual(e["error"], "assembly failed: line 12: Expected operand")

    def test_other_lines(self):
        self.assertIsNone(parse_shader_line("Shaders: VS 1 | PS 1"))
        self.assertIsNone(parse_shader_line("ShaderOverride: cannot write x"))


class ShaderOverrideFilesTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        from kyty_live import server

        self.server = server
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = server.SHADERS
        server.SHADERS = Path(self.tmp.name)
        (server.SHADERS / "dump").mkdir()
        (server.SHADERS / "dump" / f"{NAME}.spvasm").write_text("; translator output\n")

    def tearDown(self):
        self.server.SHADERS = self.saved
        self.tmp.cleanup()

    def test_rejects_paths_and_bad_names(self):
        for bad in ("../../etc/passwd", "ps_x", f"{NAME}/../a", "", None):
            with self.assertRaises(ValueError):
                self.server.shader_override(bad, "copy")

    def test_copy_then_revert(self):
        r = self.server.shader_override(NAME, "copy")
        target = self.server.SHADERS / "override" / f"{NAME}.spvasm"
        self.assertEqual(r["path"], str(target))
        self.assertEqual(target.read_text(), "; translator output\n")
        target.write_text("; edited\n")
        self.server.shader_override(NAME, "copy")  # an existing edit is never overwritten
        self.assertEqual(target.read_text(), "; edited\n")
        self.assertEqual(self.server.overrides(), [NAME])
        self.assertEqual(self.server.shader_override(NAME, "revert"), {"removed": [f"{NAME}.spvasm"]})
        self.assertEqual(self.server.overrides(), [])

    def test_undumped_shader_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.server.shader_override("cs_0000000000000000_0000000000000000", "copy")



class IssueSnapshotTest(unittest.TestCase):
    def test_caps_minor_issues_and_keeps_totals(self):
        from kyty_live import server

        issues = ([{"sig": f"c{i}", "severity": "critical"} for i in range(3)]
                  + [{"sig": f"w{i}", "severity": "warning"} for i in range(1000)]
                  + [{"sig": "i0", "severity": "info"}])
        snap = server.issue_snapshot(issues)
        self.assertEqual(snap["issue_totals"], {"critical": 3, "error": 0, "warning": 1000, "info": 1})
        self.assertEqual(len(snap["issues"]), 3 + server.MAX_MINOR_ISSUES)
        self.assertEqual([i["sig"] for i in snap["issues"][:3]], ["c0", "c1", "c2"])



class ParseFlagsTest(unittest.TestCase):
    def test_flags(self):
        from kyty_live.server import parse_flags

        self.assertEqual(parse_flags("--redzone"), ["--redzone"])
        self.assertEqual(parse_flags(" --vblank-frequency 60  --amd-cpu "), ["--vblank-frequency", "60", "--amd-cpu"])
        self.assertEqual(parse_flags(""), [])
        for bad in ("redzone", "--x & del", "--a 'b c'", "--a;b"):
            with self.assertRaises(ValueError):
                parse_flags(bad)


if __name__ == "__main__":
    unittest.main()
