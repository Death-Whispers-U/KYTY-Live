import unittest

from kyty_live.perf import CompileMeter, summarize, verdict


def sample(fps=30.0, gpu3d=20.0, compute=0.0, top=40.0, compile_ms=0, name="Thread_Gpu"):
    return {"fps": fps, "gpu": {"3D": gpu3d, "Compute": compute},
            "threads": [{"name": name, "cpu": top}, {"name": "PoolThread 1", "cpu": 10.0}],
            "compile_ms": compile_ms, "compiles": 1 if compile_ms else 0}


class VerdictTest(unittest.TestCase):
    def test_shader_compile_stall_wins(self):
        self.assertEqual(verdict(sample(compile_ms=400, gpu3d=99))["kind"], "compile")

    def test_gpu_bound(self):
        v = verdict(sample(gpu3d=97))
        self.assertEqual(v["kind"], "gpu")
        self.assertIn("97", v["text"])

    def test_compute_counts_as_gpu(self):
        self.assertEqual(verdict(sample(gpu3d=10, compute=95))["kind"], "gpu")

    def test_cpu_bound_names_the_thread(self):
        v = verdict(sample(top=98, name="Thread_Gpu"))
        self.assertEqual(v["kind"], "cpu")
        self.assertIn("Thread_Gpu", v["text"])

    def test_at_target(self):
        self.assertEqual(verdict(sample(fps=60, top=40))["kind"], "target")

    def test_nothing_saturated(self):
        self.assertEqual(verdict(sample(fps=20, top=40, gpu3d=30))["kind"], "waiting")

    def test_no_frames_yet(self):
        self.assertEqual(verdict(sample(fps=None, top=40))["kind"], "waiting")


class CompileMeterTest(unittest.TestCase):
    def test_sums_phase_times_and_counts_new_shaders(self):
        m = CompileMeter()
        for line in [
            "ShaderRecompiler PS phase end: stage=PS hash=0x4aa5 CFG BuildGraph blocks=1 loops=0 back_edges=0 elapsed_ms=3",
            "ShaderRecompiler PS phase end: stage=PS hash=0x4aa5 SPIR-V EmitProgram words=2310 elapsed_ms=12",
            "Shaders: VS 0 | PS 1 | CS 1 | GS 0 | LS 0 | HS 0 | TES 0",
            "Shaders: VS 1 | PS 1 | CS 1 | GS 0 | LS 0 | HS 0 | TES 0",
            "Initialized: Audio",
        ]:
            m.feed(line)
        self.assertEqual(m.take(), (15, 2))
        self.assertEqual(m.take(), (0, 0))


class PipelineBuildTest(unittest.TestCase):
    def test_counts_driver_pipeline_builds_separately(self):
        m = CompileMeter()
        for line in [
            "PipelineBuild: graphics elapsed_us=41250 VS=12 PS=13",
            "[0mPipelineBuild: compute elapsed_us=1500",
            "ShaderRecompiler PS phase end: stage=PS hash=0x4aa5 SPIR-V EmitProgram words=2310 elapsed_ms=12",
        ]:
            m.feed(line)
        self.assertEqual(m.take_pipelines(), (42.8, 2))
        self.assertEqual(m.take_pipelines(), (0, 0))
        self.assertEqual(m.take(), (12, 0))

    def test_summary_totals_pipeline_time(self):
        a = dict(sample(), pipeline_ms=40.5, pipelines=3)
        b = dict(sample(), pipeline_ms=10.0, pipelines=1)
        s = summarize([dict(a, verdict=verdict(a)), dict(b, verdict=verdict(b))])
        self.assertEqual((s["pipeline_ms"], s["pipelines"]), (50.5, 4))

    def test_driver_time_counts_toward_a_compile_stall(self):
        v = verdict(dict(sample(fps=20), compile_ms=10, compiles=2, pipeline_ms=400.0, pipelines=5))
        self.assertEqual(v["kind"], "compile")


class SummarizeTest(unittest.TestCase):
    def test_shares_and_top_threads(self):
        samples = [dict(sample(gpu3d=97), verdict=verdict(sample(gpu3d=97))) for _ in range(3)]
        samples.append(dict(sample(fps=60), verdict=verdict(sample(fps=60))))
        s = summarize(samples)
        self.assertEqual(s["seconds"], 4)
        self.assertEqual(s["share"]["gpu"], 75)
        self.assertEqual(s["share"]["target"], 25)
        self.assertEqual(s["main"], "gpu")
        self.assertEqual(s["top_threads"][0]["name"], "Thread_Gpu")
        self.assertEqual(s["gpu_avg"]["3D"], 77.8)


if __name__ == "__main__":
    unittest.main()


from kyty_live.perf import frame_stats, parse_frame_times


class FrameTimesTest(unittest.TestCase):
    def test_parses_a_batch(self):
        self.assertEqual(parse_frame_times("FrameTimes: 16.7 16.6 33.4\n"), [16.7, 16.6, 33.4])

    def test_a_second_without_frames_is_an_empty_batch(self):
        self.assertEqual(parse_frame_times("FrameTimes:\r\n"), [])

    def test_ignores_colour_codes_and_other_lines(self):
        self.assertEqual(parse_frame_times("\x1b[0mFrameTimes: 8.3\x1b[0m"), [8.3])
        self.assertIsNone(parse_frame_times("Window 3 exposed"))

    def test_stats_find_hitches_against_the_median(self):
        ms = [16.7] * 96 + [33.4, 50.1, 100.0, 16.6]
        s = frame_stats(ms)
        self.assertEqual(s["frames"], 100)
        self.assertEqual(s["p50_ms"], 16.7)
        self.assertEqual(s["max_ms"], 100.0)
        self.assertEqual(s["hitches"], 3)  # each at least twice the median
        self.assertAlmostEqual(s["fps_avg"], round(1000 * 100 / sum(ms), 1))
        self.assertEqual(s["p99_ms"], 50.1)
        self.assertEqual(s["low1_fps"], round(1000 / 50.1, 1))

    def test_a_frame_rate_change_is_not_a_hitch(self):
        # 60 fps intro, then 30 fps gameplay: steady pacing at both rates, one real stall.
        ms = [16.7] * 200 + [34.0] * 100 + [150.0] + [34.0] * 20
        self.assertEqual(frame_stats(ms)["hitches"], 1)

    def test_stats_of_nothing(self):
        self.assertEqual(frame_stats([]), {"frames": 0})


from kyty_live.perf import hitch_windows, parse_frame_batch


class FrameBatchTest(unittest.TestCase):
    def test_parses_the_age_of_the_newest_frame(self):
        self.assertEqual(parse_frame_batch("FrameTimes: 16.7 33.4 @4.2"), ([16.7, 33.4], 4.2))
        self.assertEqual(parse_frame_batch("FrameTimes: 16.7"), ([16.7], None))
        self.assertEqual(parse_frame_batch("FrameTimes: @950.0"), ([], 950.0))
        self.assertIsNone(parse_frame_batch("Window 3 exposed"))
        self.assertEqual(parse_frame_times("FrameTimes: 16.7 33.4 @4.2"), [16.7, 33.4])

    def test_hitch_windows_place_each_slow_frame_in_time(self):
        # Batch read at t=10.0; its newest frame was presented 100 ms earlier, at 9.9 s.
        # The frames end at 9.9 (the 500 ms hitch: 9.4-9.9) and 9.4 - its predecessors before.
        ms = [16.7] * 40 + [500.0]
        windows = hitch_windows([[10.0, ms, 100.0]])
        self.assertEqual(len(windows), 1)
        start, end = windows[0]
        self.assertAlmostEqual(end, 9.9, places=3)
        self.assertAlmostEqual(start, 9.4, places=3)

    def test_hitch_windows_span_batches(self):
        first = [16.7] * 59 + [16.7]
        second = [16.7] * 20 + [300.0] + [16.7] * 20
        windows = hitch_windows([[1.0, first, 0.0], [2.0, second, 0.0]])
        self.assertEqual(len(windows), 1)
        start, end = windows[0]
        self.assertAlmostEqual(end - start, 0.3, places=3)
        self.assertAlmostEqual(end, 2.0 - 20 * 0.0167, places=3)
