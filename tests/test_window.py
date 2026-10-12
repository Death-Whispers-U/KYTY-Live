import unittest

from kyty_live.window import KEYS, parse_macro, parse_title


class ParseTitleTest(unittest.TestCase):
    def test_reads_frame_and_fps(self):
        t = "[e51de54e | Release] PPSA16019 Double Dragon [RTX 3090] [Ryzen], frame: 2043, fps: 18"
        self.assertEqual(parse_title(t), {"frame": 2043, "fps": 18.0})

    def test_unknown_title(self):
        self.assertEqual(parse_title("Kyty"), {"frame": None, "fps": None})


class ParseMacroTest(unittest.TestCase):
    def test_lines_with_time_and_key(self):
        text = "# boot\n5 Enter\n\n6.5   J\n7 down\n"
        self.assertEqual(parse_macro(text), [(5.0, "Enter"), (6.5, "J"), (7.0, "Down")])

    def test_rejects_unknown_key(self):
        with self.assertRaises(ValueError) as cm:
            parse_macro("3 Space")
        self.assertIn("line 1", str(cm.exception))

    def test_rejects_bad_time(self):
        with self.assertRaises(ValueError):
            parse_macro("soon J")

    def test_space_and_escape_are_not_pad_keys(self):
        # Space pauses the emulator and Esc quits it; a macro must never send them.
        self.assertNotIn("Space", KEYS)
        self.assertNotIn("Esc", KEYS)



class PngTest(unittest.TestCase):
    def test_encodes_a_valid_png(self):
        import struct
        import zlib

        from kyty_live.window import encode_png

        # 2x1 BGRA pixels, bottom-up rows as GDI returns them: blue, then red.
        bgra = bytes([255, 0, 0, 255, 0, 0, 255, 255])
        png = encode_png(bgra, 2, 1)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        width, height, depth, color = struct.unpack(">IIBB", png[16:26])
        self.assertEqual((width, height, depth, color), (2, 1, 8, 2))
        idat = png.index(b"IDAT")
        size = struct.unpack(">I", png[idat - 4:idat])[0]
        raw = zlib.decompress(png[idat + 4:idat + 4 + size])
        self.assertEqual(raw, bytes([0, 0, 0, 255, 255, 0, 0]))  # filter byte, then RGB RGB


if __name__ == "__main__":
    unittest.main()
