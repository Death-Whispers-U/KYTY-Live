"""The build an emulator binary was compiled from, read from the binary itself.

Kyty Live records the source tree's git HEAD with every run, but the binary that ran can be a
different build: an A/B test swaps executables in the install folder without touching the
source tree. The emulator embeds "Source build <hash>[-dirty]" in its executable, so the binary
can be asked directly.
"""

import mmap
import re
from pathlib import Path

_STAMP = re.compile(rb"Source build ([0-9a-f]{7,40})(-dirty)?")
_cache = {}


def read_build_stamp(exe):
    """{"build": short hash, "dirty": bool} for the binary at `exe`, or None when it has no stamp."""
    path = Path(exe)
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _cache:
        _cache[key] = _scan(path)
    return _cache[key]


def _scan(path):
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            found = _STAMP.search(data)
            # The match refers into the mapping, so take its groups before the mapping closes.
            if found is None:
                return None
            return {"build": bytes(found.group(1)).decode()[:7], "dirty": found.group(2) is not None}
    except (OSError, ValueError):
        return None
