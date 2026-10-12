import unittest

from kyty_live.options import build_args, parse_help

HELP = """Release, clang-lld_link, ver = 0.3.0
kyty_emulator --game <dir|elf|zar> [options]

Options:
  --game <dir|elf|zar>                 Game directory, ELF, or ZArchive to load.
  --present-mode <value>               Fifo, Mailbox, or Immediate. Default: Mailbox.
  --fullscreen                         Run in borderless desktop fullscreen.
  --no-splash                          Disable the startup splash screen.
  --vulkan-validation <true|false>     Enable Vulkan validation.
  --gpu-assisted-validation <t|f>      Bounds-check shader accesses on the GPU.
                                       Implies --vulkan-validation; very slow.
  --skip-notice-screen <true|false>    Skip startup logos and notices in supported games.
                                      Default: false.
  --redzone                            Protect the guest SysV red zone.
  --readback-linear-images <true|false> Read back writable linear images on submit.
"""


class ParseHelpTest(unittest.TestCase):
    def test_switches_and_booleans_only(self):
        opts = parse_help(HELP)
        self.assertEqual([o["flag"] for o in opts],
                         ["--fullscreen", "--no-splash", "--vulkan-validation", "--gpu-assisted-validation",
                          "--skip-notice-screen", "--redzone", "--readback-linear-images"])
        by = {o["flag"]: o for o in opts}
        self.assertEqual(by["--redzone"]["kind"], "switch")
        self.assertEqual(by["--vulkan-validation"]["kind"], "bool")
        self.assertEqual(by["--gpu-assisted-validation"]["help"],
                         "Bounds-check shader accesses on the GPU. Implies --vulkan-validation; very slow.")
        self.assertEqual(by["--skip-notice-screen"]["help"],
                         "Skip startup logos and notices in supported games. Default: false.")

    def test_build_args(self):
        opts = parse_help(HELP)
        self.assertEqual(build_args(opts, ["--redzone", "--vulkan-validation"]),
                         ["--vulkan-validation", "true", "--redzone"])
        self.assertEqual(build_args(opts, []), [])

    def test_unknown_selection_is_rejected(self):
        with self.assertRaises(ValueError):
            build_args(parse_help(HELP), ["--format-c"])


if __name__ == "__main__":
    unittest.main()


from kyty_live.options import display_args, parse_displays


class DisplaysTest(unittest.TestCase):
    OUT = ("Display 0: LC27G7xT 2560x1440 at 0,0 primary\n"
           "Display 1: PM1562 1920x1080 at 300,1440\n")

    def test_parses_each_display(self):
        self.assertEqual(parse_displays(self.OUT), [
            {"index": 0, "name": "LC27G7xT", "width": 2560, "height": 1440, "x": 0, "y": 0, "primary": True},
            {"index": 1, "name": "PM1562", "width": 1920, "height": 1080, "x": 300, "y": 1440, "primary": False},
        ])

    def test_names_with_spaces_and_negative_positions(self):
        d = parse_displays("Display 2: DELL U2720Q 3840x2160 at -3840,0\n")[0]
        self.assertEqual((d["name"], d["x"]), ("DELL U2720Q", -3840))

    def test_an_old_build_lists_nothing(self):
        self.assertEqual(parse_displays("missing value for --list-displays\nkyty_emulator --game ..."), [])

    def test_args_only_for_a_display_the_build_has(self):
        displays = parse_displays(self.OUT)
        self.assertEqual(display_args(displays, 1), ["--display", "1"])
        self.assertEqual(display_args(displays, None), [])
        with self.assertRaises(ValueError):
            display_args(displays, 5)
        with self.assertRaises(ValueError):
            display_args([], 0)  # the installed build cannot choose a display
