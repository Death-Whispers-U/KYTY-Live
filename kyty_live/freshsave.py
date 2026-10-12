"""Runs a game with no save data, then puts the player's save back.

Before the run the game's save folder (<install>/_SaveData/<game id>) is moved into
_SaveData/.kytylive-parked/ and listed in a manifest there. After the run the save the game
created is moved into the run's folder (fresh_save/) and the parked save is moved back. Nothing is
deleted or overwritten: anything in the way is moved aside under a new name. If Kyty Live stops
during a run, recover() at the next start puts parked saves back.
"""

import json
import os
import threading
import time
from pathlib import Path


def _free_path(path):
    """`path`, or `path-2`, `path-3`, ... whichever does not exist yet."""
    if not path.exists():
        return path
    n = 2
    while path.with_name(f"{path.name}-{n}").exists():
        n += 1
    return path.with_name(f"{path.name}-{n}")


class FreshSave:
    def __init__(self, save_root):
        self.save_root = Path(save_root)
        self.parked_root = self.save_root / ".kytylive-parked"
        self.manifest = self.parked_root / "manifest.json"
        self._lock = threading.Lock()

    def _read(self):
        try:
            return json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, entries):
        self.parked_root.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
        tmp.replace(self.manifest)

    def pending(self):
        """Games whose save is parked right now."""
        with self._lock:
            return list(self._read())

    def park(self, game_id):
        """Moves the game's save out of the emulator's sight. Raises if one is already parked."""
        with self._lock:
            entries = self._read()
            if game_id in entries:
                raise RuntimeError(f"the save of {game_id} is already parked; restore it first")
            current = self.save_root / game_id
            parked = None
            if current.exists():
                parked = _free_path(self.parked_root / f"{game_id}-{time.strftime('%Y%m%d-%H%M%S')}")
                parked.parent.mkdir(parents=True, exist_ok=True)
            # Record the intent first, so a stop at any later point can be undone by recover().
            entries[game_id] = {"parked": str(parked) if parked else None, "since": time.time(), "state": "parking"}
            self._write(entries)
            if parked is not None:
                try:
                    # A rename only: never a copy-then-delete, which can leave a save half moved.
                    os.rename(current, parked)
                except OSError as e:
                    del entries[game_id]
                    self._write(entries)
                    raise RuntimeError(f"cannot move the save of {game_id} aside ({e}); "
                                       "is its folder open in another program?") from e
            entries[game_id]["state"] = "parked"
            self._write(entries)

    def restore(self, game_id, keep_dir):
        """Moves the save the run created into keep_dir/fresh_save and puts the parked one back."""
        with self._lock:
            entries = self._read()
            entry = entries.get(game_id)
            if entry is None:
                return False
            current = self.save_root / game_id
            parked = Path(entry["parked"]) if entry["parked"] else None
            if parked is not None and not parked.exists():
                # Parking never happened (stopped before the rename): the folder in place is
                # still the player's own save. Leave it.
                del entries[game_id]
                self._write(entries)
                return True
            if current.exists():
                keep = _free_path(Path(keep_dir) / "fresh_save")
                try:
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(current, keep)
                except OSError:
                    # The run folder is on another drive: keep it beside the parked saves instead.
                    keep = _free_path(self.parked_root / f"fresh-{game_id}-{time.strftime('%Y%m%d-%H%M%S')}")
                    os.rename(current, keep)
            if parked is not None:
                os.rename(parked, current)
            del entries[game_id]
            self._write(entries)
            return True

    def recover(self, keep_root):
        """After Kyty Live stopped mid-run: restores every parked save. A save the interrupted run
        created is kept under keep_root/recovered-<game id>."""
        restored = []
        for game_id in self.pending():
            keep = _free_path(Path(keep_root) / f"recovered-{game_id}")
            if self.restore(game_id, keep):
                restored.append(game_id)
        return restored
