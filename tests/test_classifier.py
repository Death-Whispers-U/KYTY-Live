import unittest

from kyty_live.classifier import Classifier, strip_ansi


NIDS = {
    "NN01qLRhiqU": {"name": "sceKernelFoo", "anyps5": "Done"},
    "Y295ygEccqk": {"name": "_ZN3sce2Np9CppWebApi6Common10LibContextC1Ev",
                    "pretty": "sce::Np::CppWebApi::Common::LibContext::LibContext()"},
}


class StripAnsiTest(unittest.TestCase):
    def test_removes_color_codes(self):
        self.assertEqual(strip_ansi("\x1b[32m\t guest ok\n\x1b[0m"), "\t guest ok\n")


class ClassifierTest(unittest.TestCase):
    def setUp(self):
        self.c = Classifier(NIDS)

    def feed(self, *lines):
        out = []
        for line in lines:
            out.extend(self.c.feed(line, t=1.5))
        return out

    def test_device_lost_is_critical_and_names_the_call(self):
        ev = self.feed("DeviceLost: vkWaitSemaphores failed with ErrorDeviceLost")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["kind"], "device_lost")
        self.assertEqual(ev[0]["severity"], "critical")
        self.assertEqual(ev[0]["call"], "vkWaitSemaphores")
        self.assertEqual(ev[0]["result"], "ErrorDeviceLost")
        self.assertIn("vkWaitSemaphores", ev[0]["text"])

    def test_other_vulkan_failure_is_its_own_kind(self):
        ev = self.feed("VulkanFailure: vkQueueSubmit2 failed with ErrorOutOfDeviceMemory")
        self.assertEqual(ev[0]["kind"], "vulkan_failure")
        self.assertEqual(ev[0]["severity"], "critical")

    def test_device_fault_lines_keep_their_detail(self):
        ev = self.feed(
            "DeviceFault: description=Page fault",
            "DeviceFault: address type=ReadInvalid address=0x00000012abcd0000 precision=0x1000",
            "[31mDeviceFault: vendor code=0x0000000000000003 data=0x0000000000000000 description=MMU fault[0m")
        self.assertEqual([e["kind"] for e in ev], ["device_fault"] * 3)
        self.assertTrue(all(e["severity"] == "critical" for e in ev))
        self.assertEqual(ev[1]["text"], "address type=ReadInvalid address=0x00000012abcd0000 precision=0x1000")
        self.assertEqual(ev[2]["text"], "vendor code=0x0000000000000003 data=0x0000000000000000 description=MMU fault")

    def test_gpu_checkpoint_lines_keep_their_detail(self):
        ev = self.feed(
            "GpuCheckpoint: the GPU reached 2 markers; the CPU recorded up to #5120",
            "GpuCheckpoint: stage=TopOfPipe #5118 draw submit=77 vs_00000000deadbeef ps_4aa5db68601dcd19 count=3 instances=1",
            "GpuCheckpoint: stage=BottomOfPipe #5117 dispatch submit=77 cs_0123456789abcdef groups=8x8x1",
            "GpuCheckpoint: next recorded #5119 emulator pass tiler")
        self.assertEqual([e["kind"] for e in ev], ["gpu_checkpoint"] * 4)
        self.assertTrue(all(e["severity"] == "critical" for e in ev))
        self.assertEqual(ev[1]["text"],
                         "stage=TopOfPipe #5118 draw submit=77 vs_00000000deadbeef ps_4aa5db68601dcd19 count=3 instances=1")
        self.assertEqual(ev[1]["shaders"], ["vs_00000000deadbeef", "ps_4aa5db68601dcd19"])
        self.assertEqual(ev[2]["shaders"], ["cs_0123456789abcdef"])
        self.assertEqual(ev[3]["shaders"], [])

    def test_shader_trace_lines_are_kept_and_a_cycle_is_critical(self):
        ev = self.feed(
            "ShaderTrace: dispatch #1 cs_657ad04626bf9d55 submit=12 groups=4096x1x1",
            "ShaderTrace:     written by draw ps_bbbf6645f5234056 submit=11 (3 writes ago) at 0x000200000000+0x100",
            "ShaderTrace: CYCLE in list buffer[1] at dispatch #7: 1 cycles, first has 2 nodes: 5 -> 9 -> 5")
        self.assertEqual([e["kind"] for e in ev], ["shader_trace", "shader_trace", "list_cycle"])
        self.assertEqual(ev[0]["severity"], "info")
        self.assertEqual(ev[2]["severity"], "critical")
        self.assertEqual(ev[1]["text"], "written by draw ps_bbbf6645f5234056 submit=11 (3 writes ago) at 0x000200000000+0x100")
        self.assertEqual(ev[1]["shaders"], ["ps_bbbf6645f5234056"])

    def test_device_fault_not_available_is_reported(self):
        ev = self.feed("DeviceFault: not available (VK_EXT_device_fault is not enabled on this device)")
        self.assertEqual(ev[0]["kind"], "device_fault")

    def test_stubbed_import_resolves_nid_name(self):
        ev = self.feed(
            "Stubbed: [0000000932527d70] <- 0000000000000000, "
            "NN01qLRhiqU[Posix_v1][libkernel_v1.1][Func], Func, Global,"
        )
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["kind"], "missing_import")
        self.assertEqual(ev[0]["nid"], "NN01qLRhiqU")
        self.assertEqual(ev[0]["name"], "sceKernelFoo")
        self.assertEqual(ev[0]["library"], "Posix_v1")
        self.assertEqual(ev[0]["anyps5"], "Done")

    def test_mangled_name_is_shown_demangled_and_kept_raw(self):
        ev = self.feed("Unresolved import stub called: Y295ygEccqk[NpCppWebApi_v1][NpCppWebApi_v1.1][Func]")
        self.assertEqual(ev[0]["name"], "sce::Np::CppWebApi::Common::LibContext::LibContext()")
        self.assertEqual(ev[0]["symbol"], "_ZN3sce2Np9CppWebApi6Common10LibContextC1Ev")
        self.assertIn("LibContext::LibContext()", ev[0]["text"])

    def test_unknown_nid_keeps_nid_as_name(self):
        ev = self.feed(
            "Stubbed: [0000000950044140] <- 0000000000000000, "
            "uKTDW8hk-ts[NpCommerce_v1][NpCommerce_v1.1][Func], Func, Global,"
        )
        self.assertEqual(ev[0]["name"], "uKTDW8hk-ts")
        self.assertEqual(ev[0]["anyps5"], "")

    def test_same_signature_is_counted_not_repeated(self):
        line = (
            "Stubbed: [0000000932527d70] <- 0000000000000000, "
            "NN01qLRhiqU[Posix_v1][libkernel_v1.1][Func], Func, Global,"
        )
        first = self.feed(line)
        second = self.feed(line.replace("0932527d70", "0932527e70"))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(self.c.issues[first[0]["sig"]]["count"], 2)

    def test_stub_called_is_critical(self):
        ev = self.feed("Unresolved import stub called: NN01qLRhiqU[Posix_v1][libkernel_v1.1]")
        self.assertEqual(ev[0]["kind"], "stub_called")
        self.assertEqual(ev[0]["severity"], "critical")
        self.assertEqual(ev[0]["name"], "sceKernelFoo")
        self.assertEqual(ev[0]["library"], "Posix_v1")

    def test_detailed_stub_call_line_is_not_a_second_issue(self):
        ev = self.feed(
            "Unresolved import stub called [0]: patch_vaddr=0x0000000950048cb0 jmprela_index=2439 "
            "symbol=NN01qLRhiqU[Posix_v1][libkernel_v1.1][Func] type=Func bind=Global program=x.prx",
            "Unresolved import stub called: NN01qLRhiqU[Posix_v1][libkernel_v1.1][Func]",
        )
        self.assertEqual(len(ev), 1)
        self.assertEqual(self.c.issues[ev[0]["sig"]]["count"], 1)

    def test_fatal_block_collects_following_lines(self):
        ev = self.feed(
            "--- Build ---",
            "e51de54e",
            "--- Fatal Error ---",
            "Error: condition (x == nullptr) is true in src/kernel/memory.cpp:120",
            "Initialized: Audio",
        )
        fatal = [e for e in ev if e["kind"] == "fatal"]
        self.assertEqual(len(fatal), 1)
        self.assertIn("x == nullptr", fatal[0]["text"])
        self.assertEqual(fatal[0]["where"], "src/kernel/memory.cpp:120")
        self.assertEqual(fatal[0]["severity"], "critical")

    def test_multiline_error_block_ends_at_location(self):
        ev = self.feed(
            "--- Error ---",
            "Unhandled host exception: type=1 code=5 pc=0x000000000000fffe access=1 address=0x0",
            " in src/loader/runtimeLinker.cpp:708",
        )
        fatal = [e for e in ev if e["kind"] == "fatal"]
        self.assertEqual(len(fatal), 1)
        self.assertIn("pc=0x000000000000fffe", fatal[0]["text"])
        self.assertEqual(fatal[0]["where"], "src/loader/runtimeLinker.cpp:708")

    def test_host_exception(self):
        ev = self.feed(
            "Unhandled host exception: type=1 code=3221225477 pc=0x000000000000fffe "
            "access=1 address=0x0000000000000000"
        )
        self.assertEqual(ev[0]["kind"], "host_exception")
        self.assertEqual(ev[0]["severity"], "critical")

    def test_missing_file_and_dlsym(self):
        ev = self.feed(
            "Can't open F:/Games/x\\sce_sys\\playgo-chunk.dat",
            "unresolved dlsym: handle = 5, module = F:/x/SaveData.prx, symbol = UnityPluginLoad",
        )
        self.assertEqual([e["kind"] for e in ev], ["missing_file", "unresolved_dlsym"])

    def test_dlsym_signature_ignores_handle(self):
        a = self.feed("unresolved dlsym: handle = 5, module = m.prx, symbol = Foo")
        b = self.feed("unresolved dlsym: handle = 6, module = m.prx, symbol = Foo")
        self.assertEqual(len(a), 1)
        self.assertEqual(b, [])

    def test_vulkan_validation(self):
        ev = self.feed("Validation Error: [ VUID-vkCmdDraw-None-08600 ] Object 0: handle = 0x1")
        self.assertEqual(ev[0]["kind"], "vulkan_validation")

    def test_trace_noise_is_not_an_issue(self):
        ev = self.feed(
            "[3][00:00:01.674] libkernel::libkernel::PthreadSetaffinity()",
            "argc = 4",
            "Equeue wait: EOP QUEUE, caller = 0x1, eq = 0x2, ev = 0x3, num = 1, timo = 0, thread_id = 3",
        )
        self.assertEqual(ev, [])

    def test_stall_is_an_error_reported_once_per_frame(self):
        a = self.c.report_stall(2043, 10, t=30)
        b = self.c.report_stall(2043, 10, t=45)
        self.assertEqual(a[0]["kind"], "stall")
        self.assertEqual(a[0]["severity"], "error")
        self.assertEqual(b, [])

    def test_is_noise(self):
        self.assertTrue(self.c.is_noise("[3][00:00:01.674] libkernel::libkernel::PthreadCreate()"))
        self.assertTrue(self.c.is_noise("arg0 = 0x10"))
        self.assertTrue(self.c.is_noise("Mouse motion: time = 1.0, , [1, 2], (3, 4)"))
        self.assertFalse(self.c.is_noise("Initialized: Audio"))

    def test_summary_lists_signatures(self):
        self.feed("Can't open a.dat", "Can't open a.dat", "Can't open b.dat")
        s = self.c.summary()
        self.assertEqual(sum(i["count"] for i in s), 3)
        self.assertEqual(len(s), 2)


class DiffTest(unittest.TestCase):
    def test_diff_reports_new_and_gone(self):
        from kyty_live.classifier import diff_runs

        old = [{"sig": "a", "kind": "missing_file"}, {"sig": "b", "kind": "fatal"}]
        new = [{"sig": "b", "kind": "fatal"}, {"sig": "c", "kind": "stub_called"}]
        d = diff_runs(old, new)
        self.assertEqual([i["sig"] for i in d["new"]], ["c"])
        self.assertEqual([i["sig"] for i in d["gone"]], ["a"])


if __name__ == "__main__":
    unittest.main()
