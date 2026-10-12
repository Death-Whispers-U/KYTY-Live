"""Choices made in the page that every run should follow (settings.json beside run.py, not
committed). Unlike config.json, Kyty Live writes this file itself."""

import json
import threading
from pathlib import Path


class Settings:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self._values = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self._values, dict):
                self._values = {}
        except (OSError, ValueError):
            self._values = {}

    def get(self, key, default=None):
        with self._lock:
            return self._values.get(key, default)

    def set(self, key, value):
        with self._lock:
            if value is None:
                self._values.pop(key, None)
            else:
                self._values[key] = value
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._values, indent=1), encoding="utf-8")
            tmp.replace(self.path)
