"""Data Kyty Live derives from the emulator for investigations: test suites, crash analysis
(faulting module and symbolized frames) and run facts."""

import re
import subprocess
import sys
from pathlib import Path

from .classifier import strip_ansi

# ----- test suites -----

_SUITE = re.compile(r"^\s*(add_executable|add_kyty_full_emulator_test)\(\s*(\w+_tests)\b", re.M)
_MODE = re.compile(r'std::strcmp\(argv\[1\],\s*"(--[a-z0-9-]+)"\)')


def discover_test_suites(cmake_text, sources=None):
    """Test executables declared in CMakeLists.txt. Suites built with add_kyty_full_emulator_test
    need the GPU; their focused modes come from the "--...-only" arguments their main() accepts."""
    sources = sources or {}
    out = []
    for kind, name in _SUITE.findall(cmake_text):
        if any(s["name"] == name for s in out):
            continue
        out.append({"name": name, "gpu": kind == "add_kyty_full_emulator_test",
                    "modes": list(dict.fromkeys(_MODE.findall(sources.get(name, ""))))})
    return out


def suite_sources(cmake_text, src_dir):
    """Maps each suite to its main test source, to find its modes."""
    out = {}
    for m in re.finditer(r"\(\s*(\w+_tests)\b[^)]*?\$\{KYTY_TESTS_DIR\}/(\w+\.cpp)", cmake_text, re.S):
        path = Path(src_dir) / "tests" / m.group(2)
        if path.exists():
            out[m.group(1)] = path.read_text(encoding="utf-8", errors="replace")
    return out


_CASE_OK = re.compile(r"^\[\w+\]\s+\S.*\s+ok\s*$")
_FAILED = re.compile(r"^\w+Tests?: .* failed\b.*$", re.M)


def parse_suite_output(output, rc):
    lines = output.splitlines()
    cases_ok = sum(1 for line in lines if _CASE_OK.match(line))
    m = _FAILED.search(output)
    failure = m.group(0) if m else ""
    if rc != 0 and not failure:
        failure = f"exited with code {rc & 0xFFFFFFFF:#x}"
        tail = [line for line in lines[-5:] if line.strip()]
        if tail:
            failure += ": " + tail[-1][:300]
    return {"passed": rc == 0 and not m, "cases_ok": cases_ok, "failure": failure}


# ----- crash analysis -----

_EXCEPTION = re.compile(r"Unhandled host exception: type=(\d+) code=(\d+) pc=0x([0-9a-fA-F]+) "
                        r"access=(\d+) address=0x([0-9a-fA-F]+)")
_HEX = re.compile(r"\b[0-9a-fA-F]{16}\b")


class CrashCollector:
    """The first host exception of a run, with the guest fault context printed before it."""

    def __init__(self):
        self.crash = None
        self._context = None

    def feed(self, line):
        if self.crash is not None:
            return
        s = strip_ansi(line).strip()
        if s == "--- Guest fault context ---":
            self._context = {"thread": None, "stack": [], "in_stack": False}
            return
        ctx = self._context
        if ctx is not None:
            if s.startswith("thread:"):
                ctx["thread"] = s[len("thread:"):].strip()
            elif s == "stack:":
                ctx["in_stack"] = True
            elif ctx["in_stack"] and _HEX.match(s):
                ctx["stack"] += [int(x, 16) for x in _HEX.findall(s)]
            elif s.startswith("---"):
                ctx["in_stack"] = False
        m = _EXCEPTION.search(s)
        if m:
            ctx = self._context or {"thread": None, "stack": []}
            self.crash = {"type": int(m.group(1)), "code": int(m.group(2)), "pc": int(m.group(3), 16),
                          "access": int(m.group(4)), "address": int(m.group(5), 16),
                          "thread": ctx["thread"], "stack": ctx["stack"]}


def module_for(modules, address):
    for base, size, name in modules:
        if base <= address < base + size:
            return name, address - base
    return None


def exe_return_addresses(modules, stack, exe_name="kyty_emulator.exe"):
    """Stack words that point into the emulator's code. A raw stack scan, so stale frames can
    appear; duplicates are dropped, order is kept."""
    exe = next(((b, s) for b, s, n in modules if n.lower() == exe_name), None)
    if exe is None:
        return []
    out = []
    for v in stack:
        if exe[0] <= v < exe[0] + exe[1] and v not in out:
            out.append(v)
    return out


def symbolize(symbolizer, exe, addresses):
    """Function names for return addresses (address - 1 points into the call instruction)."""
    if not addresses or not Path(symbolizer).exists() or not Path(exe).exists():
        return [{"address": a, "function": None} for a in addresses]
    out = []
    for a in addresses:
        try:
            r = subprocess.run([str(symbolizer), f"--obj={exe}", "--demangle", "--functions=linkage", hex(a - 1)],
                               capture_output=True, text=True, timeout=30)
            first = (r.stdout.strip().splitlines() or [""])[0]
            out.append({"address": a, "function": first if first and first != "??" else None})
        except (OSError, subprocess.SubprocessError):
            out.append({"address": a, "function": None})
    return out


# ----- run facts -----

_FACTS = [
    (re.compile(r"^Vulkan device: (.+)$"), "gpu"),
    (re.compile(r"^Vulkan robustness: (.+)$"), "robustness"),
    (re.compile(r"^Source build (\S+)$"), "source_build"),
    (re.compile(r"^Vulkan device fault reporting: (.+)$"), "device_fault"),
    (re.compile(r"^Vulkan GPU checkpoints: (.+)$"), "gpu_checkpoints"),
]


class RunFacts:
    def __init__(self):
        self.facts = {}

    def feed(self, line):
        if len(self.facts) == len(_FACTS):
            return
        s = strip_ansi(line).strip()
        for pattern, key in _FACTS:
            if key not in self.facts:
                m = pattern.match(s)
                if m:
                    self.facts[key] = m.group(1).strip()


# ----- loaded modules (Windows) -----

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    class _MODULEENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD),
                    ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                    ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]

    def loaded_modules(pid):
        """(base, size, name) of every module in the process; [] if it cannot be read."""
        snap = _k32.CreateToolhelp32Snapshot(0x8 | 0x10, pid)  # TH32CS_SNAPMODULE | SNAPMODULE32
        if not snap or snap == wintypes.HANDLE(-1).value:
            return []
        me = _MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(me)
        out = []
        ok = _k32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            out.append((me.modBaseAddr or 0, me.modBaseSize, me.szModule))
            ok = _k32.Module32NextW(snap, ctypes.byref(me))
        _k32.CloseHandle(snap)
        return out
else:
    def loaded_modules(pid):
        return []
