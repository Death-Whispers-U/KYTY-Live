import unittest

from kyty_live.loopcap import cap_loops, find_loops

SHADER = """\
; SPIR-V
; Version: 1.6
; Bound: 40
               OpCapability Shader
               OpMemoryModel Logical GLSL450
               OpEntryPoint GLCompute %main "main"
       %void = OpTypeVoid
          %3 = OpTypeFunction %void
       %uint = OpTypeInt 32 0
       %bool = OpTypeBool
     %uint_0 = OpConstant %uint 0
     %uint_1 = OpConstant %uint 1
     %uint_9 = OpConstant %uint 9
       %main = OpFunction %void None %3
          %5 = OpLabel
               OpBranch %10
         %10 = OpLabel
         %11 = OpPhi %uint %uint_0 %5 %13 %12
               OpLoopMerge %14 %12 None
               OpBranch %15
         %15 = OpLabel
         %16 = OpUGreaterThanEqual %bool %11 %uint_9
               OpBranchConditional %16 %14 %12
         %12 = OpLabel
         %13 = OpIAdd %uint %11 %uint_1
               OpBranch %10
         %14 = OpLabel
               OpBranch %20
         %20 = OpLabel
         %21 = OpPhi %uint %uint_0 %14 %23 %22
               OpLoopMerge %24 %22 None
               OpBranch %25
         %25 = OpLabel
         %26 = OpULessThan %bool %21 %uint_9
               OpBranchConditional %26 %22 %24
         %22 = OpLabel
         %23 = OpIAdd %uint %21 %uint_1
               OpBranch %20
         %24 = OpLabel
               OpReturn
               OpFunctionEnd
"""


class FindLoopsTest(unittest.TestCase):
    def test_lists_loops_in_order_with_their_exit(self):
        loops = find_loops(SHADER)
        self.assertEqual([l["index"] for l in loops], [0, 1])
        self.assertEqual(loops[0]["merge"], "%14")
        self.assertEqual(loops[0]["exit_when"], "true")
        self.assertEqual(loops[1]["exit_when"], "false")
        self.assertTrue(all(l["cappable"] for l in loops))
        self.assertEqual(loops[0]["line"], 19)


WHILE = SHADER.replace("""               OpBranchConditional %26 %22 %24
         %22 = OpLabel""", """               OpBranchConditional %26 %27 %24
         %27 = OpLabel
               OpBranch %22
         %22 = OpLabel""")


class WhileLoopTest(unittest.TestCase):
    def test_header_test_that_enters_the_body_is_the_exit(self):
        loop = find_loops(WHILE)[1]
        self.assertTrue(loop["cappable"])
        self.assertEqual(loop["exit_when"], "false")

    def test_capped_while_loop_keeps_its_targets(self):
        out = cap_loops(WHILE, [1], 5)
        self.assertIn("OpBranchConditional %kl_cond_1 %27 %24", out)

    def test_a_break_reached_only_on_some_paths_is_not_the_exit_test(self):
        guarded = SHADER.replace("""               OpBranchConditional %16 %14 %12
""", """               OpSelectionMerge %12 None
               OpBranchConditional %16 %31 %12
         %31 = OpLabel
               OpBranchConditional %16 %14 %12
""")
        self.assertFalse(find_loops(guarded)[0]["cappable"])


class CapLoopsTest(unittest.TestCase):
    def test_caps_only_the_chosen_loops(self):
        out = cap_loops(SHADER, [1], 1000)
        self.assertNotIn("%kl_count_0", out)
        self.assertIn("%kl_count_1 = OpVariable %kl_ptr_uint Function %kl_uint_0", out)
        self.assertIn("%kl_limit = OpConstant %uint 1000", out)
        # The exit is taken on false, so the cap is folded in with AND NOT.
        self.assertIn("%kl_stay_1 = OpLogicalNot %bool %kl_capped_1", out)
        self.assertIn("%kl_cond_1 = OpLogicalAnd %bool %26 %kl_stay_1", out)
        self.assertIn("OpBranchConditional %kl_cond_1 %22 %24", out)

    def test_exit_on_true_is_or_with_the_cap(self):
        out = cap_loops(SHADER, [0], 5)
        self.assertIn("%kl_cond_0 = OpLogicalOr %bool %16 %kl_capped_0", out)
        self.assertIn("OpBranchConditional %kl_cond_0 %14 %12", out)

    def test_variables_open_the_first_block(self):
        lines = cap_loops(SHADER, [0, 1], 5).splitlines()
        first = lines.index("          %5 = OpLabel")
        self.assertIn("OpVariable", lines[first + 1])
        self.assertIn("OpVariable", lines[first + 2])

    def test_types_and_constants_precede_the_function(self):
        out = cap_loops(SHADER, [0], 5)
        self.assertLess(out.index("%kl_ptr_uint = OpTypePointer Function %uint"), out.index("%main = OpFunction"))

    def test_reuses_existing_names_for_uint_and_bool(self):
        out = cap_loops(SHADER, [0], 5)
        self.assertNotIn("OpTypeInt 32 0\n", out.replace("       %uint = OpTypeInt 32 0\n", ""))

    def test_rejects_unknown_loop(self):
        with self.assertRaises(ValueError):
            cap_loops(SHADER, [7], 5)

    def test_rejects_bad_limit(self):
        with self.assertRaises(ValueError):
            cap_loops(SHADER, [0], 0)


if __name__ == "__main__":
    unittest.main()
