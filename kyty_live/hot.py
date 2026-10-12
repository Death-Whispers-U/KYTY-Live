"""Compiles hot/patch.cpp into kyty_hot.dll and swaps it in for the emulator to pick up."""

import os
import re
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOT_SRC = ROOT / "hot"
HOT_OUT = HOT_SRC / "build"

_DIAG = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+(?P<kind>error|warning|note):\s+(?P<msg>.*)$")


def parse_diagnostics(output):
    """Reads clang-cl diagnostics ("file(line,col): error: message") into dicts."""
    out = []
    for line in output.splitlines():
        m = _DIAG.match(line.strip())
        if m:
            d = m.groupdict()
            out.append({"file": os.path.basename(d["file"]), "line": int(d["line"]), "col": int(d["col"]),
                        "kind": d["kind"], "msg": d["msg"]})
    return out


class HotCompiler:
    def __init__(self, vcvars, clang_cl, on_event):
        self.vcvars = vcvars
        self.clang_cl = clang_cl
        self.on_event = on_event
        self.auto = True
        self.generation = 0
        self.last = None
        self._env = None
        self._lock = threading.Lock()
        HOT_OUT.mkdir(parents=True, exist_ok=True)

    def sources(self):
        return sorted(p for p in HOT_SRC.glob("*.cpp"))

    def _msvc_env(self):
        # vcvars64.bat takes seconds; run it once and reuse its environment for every compile.
        if self._env is None:
            out = subprocess.run(f'cmd /d /c ""{self.vcvars}" >nul && set"', capture_output=True, text=True,
                                 shell=False, timeout=120).stdout
            env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and not line.startswith("="))
            if "INCLUDE" not in env:
                raise RuntimeError("could not load the Visual Studio environment from vcvars64.bat")
            self._env = env
        return self._env

    def compile(self, reason="manual"):
        with self._lock:
            started = time.time()
            srcs = self.sources()
            if not srcs:
                result = {"ok": False, "seconds": 0, "diagnostics": [], "output": "no .cpp files in hot/", "reason": reason}
            else:
                tmp = HOT_OUT / "kyty_hot.next.dll"
                cmd = [self.clang_cl, "/nologo", "/std:c++20", "/EHsc", "/O2", "/LD", "/MD", f"/I{HOT_SRC}",
                       *map(str, srcs), f"/Fe:{tmp}", f"/Fo:{HOT_OUT}\\"]
                try:
                    p = subprocess.run(cmd, capture_output=True, text=True, env=self._msvc_env(), cwd=HOT_OUT, timeout=120)
                    output = (p.stdout + p.stderr).strip()
                    ok = p.returncode == 0 and tmp.exists()
                except (OSError, subprocess.SubprocessError, RuntimeError) as e:
                    ok, output = False, str(e)
                if ok:
                    # Atomic swap: the emulator only ever sees a complete kyty_hot.dll.
                    os.replace(tmp, HOT_OUT / "kyty_hot.dll")
                    self.generation += 1
                result = {"ok": ok, "seconds": round(time.time() - started, 2), "diagnostics": parse_diagnostics(output),
                          "output": output[-4000:], "reason": reason, "generation": self.generation}
            self.last = result
        self.on_event("hot_compile", result)
        return result

    def watch(self):
        def loop():
            seen = self._stamp()
            while True:
                time.sleep(0.4)
                stamp = self._stamp()
                if stamp != seen:
                    time.sleep(0.2)  # let the editor finish writing
                    seen = self._stamp()
                    if self.auto:
                        self.compile("saved")

        threading.Thread(target=loop, daemon=True).start()

    def _stamp(self):
        files = list(HOT_SRC.glob("*.cpp")) + list(HOT_SRC.glob("*.h"))
        return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in files if p.exists()))
