import json
import tempfile
import unittest
from pathlib import Path

from kyty_live.settings import Settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_gives_defaults(self):
        self.assertIsNone(Settings(self.path).get("display"))

    def test_values_survive_a_restart(self):
        Settings(self.path).set("display", 1)
        self.assertEqual(Settings(self.path).get("display"), 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"display": 1})

    def test_none_clears_a_value(self):
        s = Settings(self.path)
        s.set("display", 1)
        s.set("display", None)
        self.assertIsNone(Settings(self.path).get("display"))

    def test_a_broken_file_is_ignored(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(Settings(self.path).get("display"))


if __name__ == "__main__":
    unittest.main()
