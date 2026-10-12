"""Finds what limits a running game's speed, once per second.

Per-thread CPU comes from GetThreadTimes and the Windows thread names the emulator sets.
GPU engine use comes from the "GPU Engine" performance counters (the ones Task Manager shows).
Shader compile time comes from the emulator's own "phase end ... elapsed_ms=" log lines.
"""

import ctypes
import math
import re
import sys
from collections import defaultdict

from .classifier import strip_ansi

# A thread at this share of one core is the bottleneck: the work cannot spread further.
CPU_SATURATED = 85.0
GPU_SATURATED = 90.0
COMPILE_STALL_MS = 150
FPS_TARGET = 60.0

_PHASE_END = re.compile(r"phase end: stage=\w+ .*elapsed_ms=(\d+)")
_PIPELINE_BUILD = re.compile(r"^PipelineBuild: \w+ elapsed_us=(\d+)")
_SHADER_COUNT = re.compile(r"^Shaders: VS (\d+) \| PS (\d+) \| CS (\d+) \| GS (\d+) \| LS (\d+) \| HS (\d+) \| TES (\d+)")


def verdict(s):
    """What limited this second. Checked in order: a compile stall hides everything else."""
    translate = s.get("compile_ms") or 0
    driver = s.get("pipeline_ms") or 0
    if translate + driver >= COMPILE_STALL_MS:
        text = f"Shader compile stall: {translate} ms translating {s.get('compiles') or 0} new shader(s)"
        if driver:
            text += f", {driver:.0f} ms building {s.get('pipelines') or 0} pipeline(s) in the driver"
        return {"kind": "compile", "text": text}
    gpu = s.get("gpu") or {}
    busiest = max(gpu.items(), key=lambda kv: kv[1], default=(None, 0.0))
    if busiest[1] >= GPU_SATURATED:
        return {"kind": "gpu", "text": f"GPU-bound: {busiest[0]} engine at {busiest[1]:.0f}%"}
    threads = s.get("threads") or []
    if threads and threads[0]["cpu"] >= CPU_SATURATED:
        t = threads[0]
        return {"kind": "cpu", "text": f"CPU-bound: {t['name']} at {t['cpu']:.0f}% of a core"}
    fps = s.get("fps")
    if fps is not None and fps >= FPS_TARGET - 2:
        return {"kind": "target", "text": f"At target ({fps:.0f} fps); nothing limiting"}
    top = f"; busiest thread {threads[0]['name']} at {threads[0]['cpu']:.0f}%" if threads else ""
    return {"kind": "waiting", "text": f"Nothing saturated{top}: time goes to waiting (sync, I/O, timers)"}


class CompileMeter:
    """Shader translation work seen in the log since the last take()."""

    def __init__(self):
        self.ms = 0
        self.compiles = 0
        self._last_total = None
        self.pipeline_us = 0
        self.pipelines = 0

    def feed(self, line):
        if "PipelineBuild:" in line:
            m = _PIPELINE_BUILD.match(strip_ansi(line).strip())
            if m:
                self.pipeline_us += int(m.group(1))
                self.pipelines += 1
            return
        if "phase end:" in line:
            m = _PHASE_END.search(line)
            if m:
                self.ms += int(m.group(1))
        elif line.startswith("Shaders: "):
            m = _SHADER_COUNT.match(line)
            if m:
                total = sum(int(x) for x in m.groups())
                if self._last_total is not None and total > self._last_total:
                    self.compiles += total - self._last_total
                elif self._last_total is None:
                    self.compiles += 1
                self._last_total = total

    def take_pipelines(self):
        """(ms, count) of driver pipeline builds since the last call."""
        out = (round(self.pipeline_us / 1000, 1), self.pipelines)
        self.pipeline_us = self.pipelines = 0
        return out

    def take(self):
        out = (self.ms, self.compiles)
        self.ms = self.compiles = 0
        return out


def summarize(samples):
    n = len(samples)
    if not n:
        return {"seconds": 0}
    kinds = defaultdict(int)
    cpu = defaultdict(float)
    gpu = defaultdict(float)
    fps = [s["fps"] for s in samples if s.get("fps") is not None]
    for s in samples:
        kinds[s["verdict"]["kind"]] += 1
        for t in s.get("threads") or []:
            cpu[t["name"]] += t["cpu"]
        for eng, v in (s.get("gpu") or {}).items():
            gpu[eng] += v
    share = {k: round(100 * v / n) for k, v in kinds.items()}
    return {
        "seconds": n,
        "share": share,
        "main": max(share, key=share.get),
        "fps_avg": round(sum(fps) / len(fps), 1) if fps else None,
        "fps_min": min(fps) if fps else None,
        "top_threads": [{"name": k, "cpu": round(v / n, 1)}
                        for k, v in sorted(cpu.items(), key=lambda kv: -kv[1])[:8]],
        "gpu_avg": {k: round(v / n, 1) for k, v in gpu.items()},
        "compile_ms": sum(s.get("compile_ms") or 0 for s in samples),
        "compiles": sum(s.get("compiles") or 0 for s in samples),
        "pipeline_ms": round(sum(s.get("pipeline_ms") or 0 for s in samples), 1),
        "pipelines": sum(s.get("pipelines") or 0 for s in samples),
    }


_FRAME_TIMES = re.compile(r"^FrameTimes:((?: [0-9.]+)*)(?: @([0-9.]+))?\s*$")


def parse_frame_batch(raw):
    """(intervals ms, ms since the newest frame or None) of one "FrameTimes:" line, or None."""
    m = _FRAME_TIMES.match(strip_ansi(raw).strip())
    if not m:
        return None
    return [float(x) for x in m.group(1).split()], (float(m.group(2)) if m.group(2) else None)


def parse_frame_times(raw):
    """The intervals (ms) of one "FrameTimes:" line from --frame-times, or None for other lines."""
    batch = parse_frame_batch(raw)
    return None if batch is None else batch[0]


def hitch_windows(batches):
    """(start, end) times of the hitches. Each batch is [time read, intervals ms, age ms]: its
    newest frame was presented `age` before it was read, and each earlier frame one interval
    before the next."""
    frames = []  # (start, end), oldest first
    for batch in batches:
        t, intervals = batch[0], batch[1]
        age = batch[2] if len(batch) > 2 and batch[2] is not None else 0.0
        end = t - age / 1000
        placed = []
        for ms in reversed(intervals):
            placed.append((end - ms / 1000, end))
            end -= ms / 1000
        frames.extend(reversed(placed))
    flags = hitch_flags([b - a for a, b in frames])
    return [w for w, hitch in zip(frames, flags) if hitch]


def _rank(ordered, q):
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


HITCH_WINDOW = 30  # frames on each side of a frame that set its expected pace


def _median(values):
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def hitch_flags(intervals):
    """True for each frame that took at least twice the pace around it: the median of the frames
    before it and, when enough follow, of the frames after it. A spike stands out on both sides;
    a game moving from 60 to 30 fps does not, so a frame-rate change is not counted as hitches."""
    flags = [False] * len(intervals)
    for i in range(1, len(intervals)):
        pace = _median(intervals[max(0, i - HITCH_WINDOW):i])
        after = intervals[i + 1:i + 1 + HITCH_WINDOW]
        if len(after) >= 10:
            pace = max(pace, _median(after))
        flags[i] = intervals[i] >= 2 * pace
    return flags


def frame_stats(intervals):
    """Pacing over a run; see hitch_flags for what counts as a hitch."""
    if not intervals:
        return {"frames": 0}
    ordered = sorted(intervals)
    median = _rank(ordered, 0.5)
    p99 = _rank(ordered, 0.99)
    return {
        "frames": len(intervals),
        "fps_avg": round(1000 * len(intervals) / sum(intervals), 1),
        "p50_ms": median,
        "p95_ms": _rank(ordered, 0.95),
        "p99_ms": p99,
        "max_ms": ordered[-1],
        "low1_fps": round(1000 / p99, 1) if p99 > 0 else None,
        "hitches": sum(hitch_flags(intervals)),
    }


if sys.platform == "win32":
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _pdh = ctypes.WinDLL("pdh")

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                    ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.GetThreadDescription.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.LocalFree.argtypes = [ctypes.c_void_p]

    _TH32CS_SNAPTHREAD = 0x4
    _THREAD_QUERY_LIMITED_INFORMATION = 0x0800

    class ThreadSampler:
        """CPU use per thread of one process, as % of one core, since the previous sample."""

        def __init__(self, pid):
            self.pid = pid
            self._prev = {}
            self._prev_time = None

        def _threads(self):
            snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
            te = _THREADENTRY32()
            te.dwSize = ctypes.sizeof(te)
            out = []
            ok = _k32.Thread32First(snap, ctypes.byref(te))
            while ok:
                if te.th32OwnerProcessID == self.pid:
                    out.append(te.th32ThreadID)
                ok = _k32.Thread32Next(snap, ctypes.byref(te))
            _k32.CloseHandle(snap)
            return out

        @staticmethod
        def _read(tid):
            h = _k32.OpenThread(_THREAD_QUERY_LIMITED_INFORMATION, False, tid)
            if not h:
                return None
            try:
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if not _k32.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                    return None
                total = sum((t.dwHighDateTime << 32) | t.dwLowDateTime for t in (k, u))
                p = ctypes.c_wchar_p()
                name = ""
                if _k32.GetThreadDescription(h, ctypes.byref(p)) >= 0 and p.value:
                    name = p.value
                    _k32.LocalFree(p)
                return name, total
            finally:
                _k32.CloseHandle(h)

        def find(self, name):
            """The id of the first thread with this name, or None."""
            for tid in self._threads():
                info = self._read(tid)
                if info and info[0] == name:
                    return tid
            return None

        def sample(self, now):
            cur = {}
            for tid in self._threads():
                r = self._read(tid)
                if r:
                    cur[tid] = r
            out = []
            if self._prev_time is not None and now > self._prev_time:
                wall = (now - self._prev_time) * 1e7  # FILETIME units are 100 ns
                for tid, (name, total) in cur.items():
                    if tid in self._prev:
                        pct = 100.0 * (total - self._prev[tid][1]) / wall
                        if pct >= 0.5:
                            out.append({"name": name or f"thread {tid}", "cpu": round(pct, 1)})
            self._prev, self._prev_time = cur, now
            out.sort(key=lambda t: -t["cpu"])
            return out

    class _FMT_VALUE(ctypes.Structure):
        _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

    class _FMT_ITEM(ctypes.Structure):
        _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _FMT_VALUE)]

    _PDH_FMT_DOUBLE = 0x00000200
    _PDH_MORE_DATA = 0x800007D2

    class GpuSampler:
        """GPU engine use of one process, per engine type (3D, Compute, Copy, ...), in %."""

        def __init__(self, pid):
            self.pid = pid
            self.query = wintypes.HANDLE()
            self.counter = wintypes.HANDLE()
            self.ok = (_pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)) == 0 and
                       _pdh.PdhAddEnglishCounterW(self.query, f"\\GPU Engine(pid_{pid}_*)\\Utilization Percentage",
                                                  0, ctypes.byref(self.counter)) == 0)
            if self.ok:
                _pdh.PdhCollectQueryData(self.query)

        def sample(self):
            if not self.ok or _pdh.PdhCollectQueryData(self.query) != 0:
                return {}
            size, count = wintypes.DWORD(0), wintypes.DWORD(0)
            rc = _pdh.PdhGetFormattedCounterArrayW(self.counter, _PDH_FMT_DOUBLE, ctypes.byref(size),
                                                   ctypes.byref(count), None) & 0xFFFFFFFF
            if rc != _PDH_MORE_DATA or size.value == 0:
                return {}
            buf = ctypes.create_string_buffer(size.value)
            if _pdh.PdhGetFormattedCounterArrayW(self.counter, _PDH_FMT_DOUBLE, ctypes.byref(size),
                                                 ctypes.byref(count), buf) != 0:
                return {}
            items = ctypes.cast(buf, ctypes.POINTER(_FMT_ITEM))
            out = defaultdict(float)
            for i in range(count.value):
                name = items[i].szName or ""
                m = re.search(r"engtype_(\w+)$", name)
                if m and items[i].FmtValue.CStatus in (0, 1):
                    out[m.group(1)] += items[i].FmtValue.doubleValue
            return {k: round(min(v, 100.0), 1) for k, v in out.items() if v > 0 or k in ("3D", "Compute")}

        def close(self):
            if self.ok:
                _pdh.PdhCloseQuery(self.query)
                self.ok = False

else:

    class ThreadSampler:
        def __init__(self, pid):
            pass

        def sample(self, now):
            return []

    class GpuSampler:
        def __init__(self, pid):
            pass

        def sample(self):
            return {}

        def close(self):
            pass
