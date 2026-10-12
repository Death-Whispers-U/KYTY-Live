import unittest

from kyty_live.diagnostics import (CrashCollector, RunFacts, discover_test_suites, exe_return_addresses,
                                   module_for, parse_suite_output)

CMAKE = """
add_executable(bit_array_tests EXCLUDE_FROM_ALL
	"${KYTY_TESTS_DIR}/BitArrayTests.cpp")
add_executable(resource_materialization_tests EXCLUDE_FROM_ALL
add_kyty_full_emulator_test(shader_recompiler_compute_tests "${KYTY_TESTS_DIR}/ShaderRecompilerComputeTests.cpp")
add_executable(kyty_emulator ${kyty_emulator_src})
"""

COMPUTE_SRC = """
  if (argc == 2 && std::strcmp(argv[1], "--sampled-depth-resource-only") == 0) {
  if (argc == 2 && std::strcmp(argv[1], "--polygon-mode-only") == 0) {
"""

FAULT = """Read 126208 bytes
--- Guest fault context ---
thread: (host thread)
rax=0000010057a7ce08 rbx=0000010057a7d3a0 rcx=0000010057a7ce08 rdx=0000000000000018
code (pc-48 .. pc+48, fault at byte 48):
 89 00 89 48 08 c3 8b 0a 44 0f b7 42 04 89 08 66
stack:
  0000000140291c83 0000000000000040 0000000100000000 0000010057a7d420
  0000000140291c83 000000014029628a 0000000000000000 0000000000000001
--- Build ---
Source build b80d2a0
--- Error ---
Unhandled host exception: type=1 code=3221225477 pc=0x00007fffde80c12a access=1 address=0x0000000000000018
 in C:/src/KytyPS5/src/loader/runtimeLinker.cpp:713
Initialized: Audio
"""


class DiscoverTest(unittest.TestCase):
    def test_finds_suites_and_modes(self):
        suites = discover_test_suites(CMAKE, {"shader_recompiler_compute_tests": COMPUTE_SRC})
        self.assertEqual([s["name"] for s in suites],
                         ["bit_array_tests", "resource_materialization_tests", "shader_recompiler_compute_tests"])
        compute = suites[2]
        self.assertTrue(compute["gpu"])
        self.assertEqual(compute["modes"], ["--sampled-depth-resource-only", "--polygon-mode-only"])
        self.assertFalse(suites[0]["gpu"])


class SuiteOutputTest(unittest.TestCase):
    def test_counts_cases_and_failure(self):
        out = ("[host]    SampledDepthResource             ok\n"
               "[gpu]     ComparisonDepthTexture           ok\n"
               "ShaderRecompilerComputeTests: NullComparisonTexture failed at depth stand-in: bound as color\n")
        r = parse_suite_output(out, rc=3)
        self.assertFalse(r["passed"])
        self.assertEqual(r["cases_ok"], 2)
        self.assertIn("NullComparisonTexture failed", r["failure"])

    def test_all_passed(self):
        r = parse_suite_output("ResourceMaterializationTests: all cases passed\n", rc=0)
        self.assertTrue(r["passed"])
        self.assertEqual(r["failure"], "")

    def test_crash_exit_code_is_a_failure(self):
        r = parse_suite_output("[host] x ok\n", rc=0xC0000005)
        self.assertFalse(r["passed"])
        self.assertIn("0xc0000005", r["failure"])


class CrashTest(unittest.TestCase):
    def test_collects_fault_and_stack(self):
        c = CrashCollector()
        for line in FAULT.replace("--- Guest fault context ---", "[31m--- Guest fault context ---").splitlines():
            c.feed(line)
        crash = c.crash
        self.assertEqual(crash["pc"], 0x7FFFDE80C12A)
        self.assertEqual(crash["address"], 0x18)
        self.assertEqual(crash["thread"], "(host thread)")
        self.assertEqual(crash["stack"][:2], [0x140291C83, 0x40])

    def test_only_first_fault_is_kept(self):
        c = CrashCollector()
        for line in (FAULT + FAULT.replace("de80c12a", "11111111")).splitlines():
            c.feed(line)
        self.assertEqual(c.crash["pc"], 0x7FFFDE80C12A)

    def test_module_and_exe_frames(self):
        modules = [(0x140000000, 0x2000000, "kyty_emulator.exe"), (0x7FFFDE7F0000, 0x30000, "VCRUNTIME140.dll")]
        self.assertEqual(module_for(modules, 0x7FFFDE80C12A), ("VCRUNTIME140.dll", 0x1C12A))
        self.assertIsNone(module_for(modules, 0x18))
        stack = [0x140291C83, 0x40, 0x140291C83, 0x14029628A, 0x0]
        self.assertEqual(exe_return_addresses(modules, stack), [0x140291C83, 0x14029628A])


class FactsTest(unittest.TestCase):
    def test_reads_device_and_build(self):
        f = RunFacts()
        for line in ["[0mVulkan device: NVIDIA GeForce RTX 5080",
                     "Vulkan robustness: robustImageAccess=true robustImageAccess2=true",
                     "Source build b80d2a0", "Initialized: Audio"]:
            f.feed(line)
        self.assertEqual(f.facts, {"gpu": "NVIDIA GeForce RTX 5080",
                                   "robustness": "robustImageAccess=true robustImageAccess2=true",
                                   "source_build": "b80d2a0"})


    def test_device_fault_reporting_fact(self):
        f = RunFacts()
        f.feed("Vulkan device fault reporting: enabled")
        self.assertEqual(f.facts["device_fault"], "enabled")

    def test_gpu_checkpoints_fact(self):
        f = RunFacts()
        f.feed("Vulkan GPU checkpoints: enabled")
        self.assertEqual(f.facts["gpu_checkpoints"], "enabled")


class SearchLogTest(unittest.TestCase):
    def test_finds_lines_case_insensitively(self):
        import tempfile
        from pathlib import Path

        from kyty_live.server import search_log

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "output.log"
            log.write_bytes(b"Initialized: Audio\r\nVulkan device: RTX 5080\r\nvulkan robustness: on\r\nbye\r\n")
            r = search_log(log, "VULKAN", 1)
            self.assertEqual(r["total"], 2)
            self.assertEqual(r["hits"], [{"line": 2, "text": "Vulkan device: RTX 5080"}])
            with self.assertRaises(ValueError):
                search_log(log, "v")


if __name__ == "__main__":
    unittest.main()
