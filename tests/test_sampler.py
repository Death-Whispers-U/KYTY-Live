import os
import struct
import tempfile
import unittest

from kyty_live.sampler import addresses_to_symbolize, frame_label, preferred_image_base, profile_report

EXE = "kyty_emulator.exe"
MODULES = [(0x7FF600000000, 0x1000000, EXE), (0x7FFA00000000, 0x100000, "nvgpucomp64.dll")]
NAMES = {0x7FF600001000: "Libs::Graphics::TileImage", 0x7FF600002000: "Libs::Graphics::RenderExecutor::Draw",
         0x7FF600003000: "Libs::Graphics::GuestGpu::Run"}


def stack(*names):
    reverse = {v: k for k, v in NAMES.items()}
    return [reverse.get(n, 0x7FFA00000500 if n == "nvgpucomp64.dll" else 0x10) for n in names]


class PeTest(unittest.TestCase):
    def test_reads_the_pe32_plus_image_base(self):
        head = bytearray(4096)
        struct.pack_into("<I", head, 0x3C, 0x80)
        head[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", head, 0x80 + 24, 0x20B)
        struct.pack_into("<Q", head, 0x80 + 24 + 24, 0x140000000)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".exe") as f:
            f.write(head)
        try:
            self.assertEqual(preferred_image_base(f.name), 0x140000000)
        finally:
            os.unlink(f.name)


class ReportTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(frame_label(0x7FF600001000, MODULES, NAMES, EXE), "Libs::Graphics::TileImage")
        self.assertEqual(frame_label(0x7FF600009000, MODULES, NAMES, EXE), "kyty_emulator.exe+0x9000")
        self.assertEqual(frame_label(0x7FFA00000500, MODULES, NAMES, EXE), "nvgpucomp64.dll")
        self.assertEqual(frame_label(0x10, MODULES, NAMES, EXE), "?")

    def test_self_and_inclusive(self):
        samples = [
            (1.0, stack("Libs::Graphics::TileImage", "Libs::Graphics::RenderExecutor::Draw", "Libs::Graphics::GuestGpu::Run")),
            (1.1, stack("Libs::Graphics::TileImage", "Libs::Graphics::GuestGpu::Run")),
            (1.2, stack("nvgpucomp64.dll", "Libs::Graphics::RenderExecutor::Draw", "Libs::Graphics::GuestGpu::Run")),
            (1.3, stack("Libs::Graphics::GuestGpu::Run")),
        ]
        r = profile_report(samples, MODULES, NAMES)
        self.assertEqual(r["samples"], 4)
        self.assertEqual(r["self"][0], {"function": "Libs::Graphics::TileImage", "samples": 2, "percent": 50.0})
        inc = {row["function"]: row["samples"] for row in r["inclusive"]}
        self.assertEqual(inc["Libs::Graphics::GuestGpu::Run"], 4)  # also on top once: kept
        self.assertEqual(inc["Libs::Graphics::RenderExecutor::Draw"], 2)
        self.assertEqual(inc["nvgpucomp64.dll"], 1)

    def test_emulator_view_charges_system_time_to_the_emulator_caller(self):
        samples = [
            (1.0, stack("nvgpucomp64.dll", "Libs::Graphics::RenderExecutor::Draw", "Libs::Graphics::GuestGpu::Run")),
            (1.1, stack("nvgpucomp64.dll", "Libs::Graphics::RenderExecutor::Draw", "Libs::Graphics::GuestGpu::Run")),
            (1.2, stack("Libs::Graphics::TileImage", "Libs::Graphics::GuestGpu::Run")),
        ]
        r = profile_report(samples, MODULES, NAMES)
        self.assertEqual(r["emulator"][0], {"function": "Libs::Graphics::RenderExecutor::Draw", "samples": 2,
                                            "percent": 66.7})

    def test_frames_on_every_sample_are_left_out_of_the_inclusive_view(self):
        samples = [(1.0, stack("Libs::Graphics::TileImage", "Libs::Graphics::GuestGpu::Run")),
                   (1.1, stack("Libs::Graphics::RenderExecutor::Draw", "Libs::Graphics::GuestGpu::Run"))]
        inc = [row["function"] for row in profile_report(samples, MODULES, NAMES)["inclusive"]]
        self.assertNotIn("Libs::Graphics::GuestGpu::Run", inc)
        self.assertIn("Libs::Graphics::TileImage", inc)

    def test_a_function_counts_once_per_sample(self):
        r = profile_report([(1.0, stack("Libs::Graphics::TileImage", "Libs::Graphics::TileImage"))], MODULES, NAMES)
        self.assertEqual(r["inclusive"][0]["samples"], 1)

    def test_windows_keep_only_samples_inside_them(self):
        samples = [(1.0, stack("Libs::Graphics::TileImage")), (5.0, stack("nvgpucomp64.dll"))]
        r = profile_report(samples, MODULES, NAMES, windows=[(4.5, 6.0)])
        self.assertEqual(r["samples"], 1)
        self.assertEqual(r["self"][0]["function"], "nvgpucomp64.dll")

    def test_only_emulator_addresses_are_symbolized(self):
        samples = [(1.0, stack("Libs::Graphics::TileImage", "nvgpucomp64.dll")),
                   (1.1, stack("Libs::Graphics::TileImage"))]
        self.assertEqual(addresses_to_symbolize(samples, MODULES), [0x7FF600001000])


if __name__ == "__main__":
    unittest.main()
