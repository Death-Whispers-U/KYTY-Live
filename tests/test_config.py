import unittest
from pathlib import Path

from kyty_live.config import DEFAULTS, resolve_config, visual_studio_tools

ROOT = Path("C:/work/kyty-live")
VS = Path("C:/VS/2022/Community")


def resolve(user=None, env=None, vs=VS):
    return resolve_config(ROOT, user or {}, env or {}, find_vs=lambda: vs)


class ConfigTest(unittest.TestCase):
    def test_defaults_expect_kytyps5_beside_kyty_live(self):
        cfg = resolve()
        self.assertEqual(Path(cfg["src_dir"]), Path("C:/work/KytyPS5"))
        self.assertEqual(Path(cfg["build_dir"]), Path("C:/work/KytyPS5/_Build/kyty-live"))
        self.assertEqual(Path(cfg["install_dir"]), Path("C:/work/KytyPS5/_Build/kyty-live/install"))
        self.assertEqual(cfg["port"], DEFAULTS["port"])

    def test_environment_names_the_source(self):
        cfg = resolve(env={"KYTY_SRC_DIR": "D:/emu/KytyPS5"})
        self.assertEqual(Path(cfg["src_dir"]), Path("D:/emu/KytyPS5"))
        self.assertEqual(Path(cfg["build_dir"]), Path("D:/emu/KytyPS5/_Build/kyty-live"))

    def test_config_file_wins_and_relative_paths_start_at_kyty_live(self):
        cfg = resolve(user={"src_dir": "../engine", "build_dir": "D:/b", "game_dirs": ["games"]},
                      env={"KYTY_SRC_DIR": "D:/ignored"})
        self.assertEqual(Path(cfg["src_dir"]), Path("C:/work/engine"))
        self.assertEqual(Path(cfg["build_dir"]), Path("D:/b"))
        self.assertEqual(Path(cfg["install_dir"]), Path("D:/b/install"))
        self.assertEqual([Path(g) for g in cfg["game_dirs"]], [Path("C:/work/kyty-live/games")])

    def test_tools_come_from_the_visual_studio_install(self):
        cfg = resolve()
        self.assertEqual(Path(cfg["vcvars"]), VS / "VC/Auxiliary/Build/vcvars64.bat")
        self.assertEqual(Path(cfg["symbolizer"]), VS / "VC/Tools/Llvm/x64/bin/llvm-symbolizer.exe")
        self.assertEqual(Path(cfg["clang_cl"]), VS / "VC/Tools/Llvm/x64/bin/clang-cl.exe")
        self.assertEqual(Path(cfg["cxxfilt"]), VS / "VC/Tools/Llvm/x64/bin/llvm-cxxfilt.exe")

    def test_a_tool_set_in_the_config_file_is_kept(self):
        cfg = resolve(user={"symbolizer": "D:/llvm/bin/llvm-symbolizer.exe"})
        self.assertEqual(Path(cfg["symbolizer"]), Path("D:/llvm/bin/llvm-symbolizer.exe"))

    def test_without_visual_studio_tools_are_left_empty(self):
        cfg = resolve(vs=None)
        self.assertEqual(cfg["vcvars"], "")
        self.assertEqual(cfg["symbolizer"], "")

    def test_visual_studio_tool_paths(self):
        tools = visual_studio_tools(VS)
        self.assertEqual(set(tools), {"vcvars", "symbolizer", "clang_cl", "cxxfilt"})


if __name__ == "__main__":
    unittest.main()
