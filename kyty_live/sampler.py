"""A sampling profiler for one thread of the emulator (Windows).

About 200 times a second it suspends the thread, walks its call stack with dbghelp (StackWalk64
against the emulator process, using the unwind data of its modules), and resumes it. The
sampler runs in Kyty Live's process, so it never runs code inside the emulator.

Samples are (time, [addresses, innermost first]). `profile_report` turns them into the functions
the thread spent its time in, optionally only inside given time windows (the hitches).
"""

import struct
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path


# ----- turning samples into a report (pure; tested) -----

def preferred_image_base(exe_path):
    """The ImageBase a PE file was linked at (what llvm-symbolizer expects addresses relative to)."""
    with open(exe_path, "rb") as f:
        head = f.read(4096)
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    magic = struct.unpack_from("<H", head, pe + 24)[0]
    if magic == 0x20B:  # PE32+
        return struct.unpack_from("<Q", head, pe + 24 + 24)[0]
    return struct.unpack_from("<I", head, pe + 24 + 28)[0]


def frame_label(address, modules, names, exe_name):
    """A function name for addresses in the emulator, "module.dll" for anything else."""
    for base, size, name in modules:
        if base <= address < base + size:
            if name.lower() == exe_name.lower():
                return names.get(address) or f"{name}+0x{address - base:x}"
            return name
    return "?"


def profile_report(samples, modules, names, exe_name="kyty_emulator.exe", windows=None, top=40):
    """Self and inclusive counts per function. `names` maps address -> function (emulator only).
    With `windows` ([(start, end)] in sample time), only samples inside them count."""
    self_counts, inclusive, emulator = Counter(), Counter(), Counter()
    total = 0
    for t, frames in samples:
        if windows is not None and not any(a <= t <= b for a, b in windows):
            continue
        if not frames:
            continue
        total += 1
        labels = [frame_label(a, modules, names, exe_name) for a in frames]
        self_counts[labels[0]] += 1
        for label in dict.fromkeys(labels):  # each function once per sample
            inclusive[label] += 1
        # Time spent in system or driver code is charged to the emulator function that called it.
        own = next((label for a, label in zip(frames, labels) if _in_exe(a, modules, exe_name)), None)
        if own:
            emulator[own] += 1

    def rows(counter):
        return [{"function": f, "samples": n, "percent": round(100 * n / total, 1)}
                for f, n in counter.most_common(top)]

    # Thread-start frames sit under every sample and are never on top: they say nothing.
    for label, n in list(inclusive.items()):
        if n == total and label not in self_counts:
            del inclusive[label]
    return {"samples": total, "self": rows(self_counts), "inclusive": rows(inclusive),
            "emulator": rows(emulator)}


def _in_exe(address, modules, exe_name):
    return any(base <= address < base + size and name.lower() == exe_name.lower()
               for base, size, name in modules)


def addresses_to_symbolize(samples, modules, exe_name="kyty_emulator.exe"):
    """Distinct emulator addresses in the samples."""
    exe = next(((b, s) for b, s, n in modules if n.lower() == exe_name.lower()), None)
    if exe is None:
        return []
    seen = set()
    for _, frames in samples:
        for a in frames:
            if exe[0] <= a < exe[0] + exe[1]:
                seen.add(a)
    return sorted(seen)


def symbolize_batch(symbolizer, exe_path, runtime_base, addresses, first_is_pc=None):
    """Function names for many emulator addresses with one llvm-symbolizer process.
    Return addresses are looked up at address - 1 (inside the call instruction)."""
    if not addresses or not Path(symbolizer).exists() or not Path(exe_path).exists():
        return {}
    linked = preferred_image_base(exe_path)
    lookups = [linked + (a - runtime_base) - (0 if first_is_pc and a in first_is_pc else 1) for a in addresses]
    try:
        r = subprocess.run([str(symbolizer), f"--obj={exe_path}", "--demangle", "--functions=linkage",
                            "--no-inlines"], input="\n".join(hex(x) for x in lookups) + "\n",
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return {}
    blocks = [b for b in r.stdout.split("\n\n") if b.strip()]
    out = {}
    for a, block in zip(addresses, blocks):
        name = block.strip().splitlines()[0].strip()
        if name and name != "??":
            out[a] = name
    return out


# ----- sampling (Windows) -----

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)

    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.SuspendThread.argtypes = [wintypes.HANDLE]
    _k32.ResumeThread.argtypes = [wintypes.HANDLE]
    _k32.GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]

    # BOOL ReadProcessMemoryProc64(HANDLE, DWORD64 address, PVOID buffer, DWORD size, LPDWORD read)
    _READ_MEMORY = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, ctypes.c_uint64, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _STACK_COPY = 64 * 1024  # bytes of stack copied per sample; deeper frames read live

    class _ADDRESS64(ctypes.Structure):
        _fields_ = [("Offset", ctypes.c_uint64), ("Segment", ctypes.c_uint16), ("Mode", ctypes.c_int)]

    class _KDHELP64(ctypes.Structure):
        _fields_ = [("Thread", ctypes.c_uint64), ("ThCallbackStack", ctypes.c_uint32),
                    ("ThCallbackBStore", ctypes.c_uint32), ("NextCallback", ctypes.c_uint32),
                    ("FramePointer", ctypes.c_uint32), ("KiCallUserMode", ctypes.c_uint64),
                    ("KeUserCallbackDispatcher", ctypes.c_uint64), ("SystemRangeStart", ctypes.c_uint64),
                    ("KiUserExceptionDispatcher", ctypes.c_uint64), ("StackBase", ctypes.c_uint64),
                    ("StackLimit", ctypes.c_uint64), ("BuildVersion", ctypes.c_uint32),
                    ("RetpolineStubFunctionTableSize", ctypes.c_uint32),
                    ("RetpolineStubFunctionTable", ctypes.c_uint64), ("RetpolineStubOffset", ctypes.c_uint32),
                    ("RetpolineStubSize", ctypes.c_uint32), ("Reserved0", ctypes.c_uint64 * 2)]

    class _STACKFRAME64(ctypes.Structure):
        _fields_ = [("AddrPC", _ADDRESS64), ("AddrReturn", _ADDRESS64), ("AddrFrame", _ADDRESS64),
                    ("AddrStack", _ADDRESS64), ("AddrBStore", _ADDRESS64), ("FuncTableEntry", ctypes.c_void_p),
                    ("Params", ctypes.c_uint64 * 4), ("Far", wintypes.BOOL), ("Virtual", wintypes.BOOL),
                    ("Reserved", ctypes.c_uint64 * 3), ("KdHelp", _KDHELP64),
                    # Slack in case this dbghelp's STACKFRAME64 is larger than the header we copied.
                    ("_slack", ctypes.c_uint8 * 256)]

    _dbghelp.SymInitialize.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.BOOL]
    _dbghelp.SymCleanup.argtypes = [wintypes.HANDLE]
    _dbghelp.SymSetOptions.argtypes = [wintypes.DWORD]
    _dbghelp.StackWalk64.argtypes = [wintypes.DWORD, wintypes.HANDLE, wintypes.HANDLE,
                                     ctypes.POINTER(_STACKFRAME64), ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _FUNC_TABLE = ctypes.cast(_dbghelp.SymFunctionTableAccess64, ctypes.c_void_p).value
    _MODULE_BASE = ctypes.cast(_dbghelp.SymGetModuleBase64, ctypes.c_void_p).value

    _CONTEXT_SIZE = 1232
    _CONTEXT_FLAGS = 0x10000B  # CONTEXT_AMD64 | CONTROL | INTEGER | FLOATING_POINT
    _OFF_FLAGS, _OFF_RSP, _OFF_RBP, _OFF_RIP = 0x30, 0x98, 0xA0, 0xF8

    class ThreadSampler:
        """Samples one thread's stack until stop(). Usable once."""

        def __init__(self, pid, tid, hz=200, depth=48):
            self.pid, self.tid, self.period, self.depth = pid, tid, 1.0 / hz, depth
            self.samples = []
            self.error = None
            self.started = None
            self.suspended = 0.0  # seconds the thread was held suspended
            self._stop = threading.Event()
            self._thread = None

        def start(self):
            self.started = time.time()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

        def stop(self):
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=10)
            return self.samples

        def _run(self):
            proc = _k32.OpenProcess(0x0010 | 0x0400, False, self.pid)  # VM_READ | QUERY_INFORMATION
            thread = _k32.OpenThread(0x0002 | 0x0008 | 0x0040, False, self.tid)  # SUSPEND | GET_CONTEXT | QUERY
            if not proc or not thread:
                self.error = f"cannot open the emulator (error {ctypes.get_last_error()})"
                return
            _dbghelp.SymSetOptions(0x4 | 0x2)  # DEFERRED_LOADS | UNDNAME
            if not _dbghelp.SymInitialize(proc, None, True):
                self.error = f"dbghelp could not attach (error {ctypes.get_last_error()})"
                _k32.CloseHandle(thread)
                _k32.CloseHandle(proc)
                return
            raw = (ctypes.c_char * (_CONTEXT_SIZE + 16))()
            ctx = (ctypes.addressof(raw) + 15) & ~15
            frame = _STACKFRAME64()
            self._copy = (ctypes.c_char * _STACK_COPY)()
            self._copy_base, self._copy_len = 0, 0
            self._read_cb = _READ_MEMORY(self._read_memory)  # kept alive while walking
            try:
                while not self._stop.is_set():
                    t = time.time()
                    stack = self._sample(thread, proc, ctx, frame)
                    if stack:
                        self.samples.append((t, stack))
                    rest = self.period - (time.time() - t)
                    if rest > 0:
                        time.sleep(rest)
            finally:
                _dbghelp.SymCleanup(proc)
                _k32.CloseHandle(thread)
                _k32.CloseHandle(proc)

        def _read_memory(self, process, address, buffer, size, read):
            """dbghelp's memory reader: stack memory comes from the copy taken while the thread was
            stopped (it has moved on since); code and unwind data are read live (they do not change)."""
            offset = address - self._copy_base
            if 0 <= offset and offset + size <= self._copy_len:
                ctypes.memmove(buffer, ctypes.addressof(self._copy) + offset, size)
                if read:
                    read[0] = size
                return True
            done = ctypes.c_size_t(0)
            ok = _k32.ReadProcessMemory(process, ctypes.c_void_p(address), buffer, size, ctypes.byref(done))
            if read:
                read[0] = done.value
            return bool(ok)

        def _sample(self, thread, proc, ctx, frame):
            # Hold the thread only to read its registers and copy the top of its stack, so the
            # profile barely slows what it measures; the walk runs after it resumes.
            held = time.perf_counter()
            if _k32.SuspendThread(thread) == 0xFFFFFFFF:
                self._stop.set()  # the thread is gone
                return None
            try:
                ctypes.memset(ctx, 0, _CONTEXT_SIZE)
                ctypes.c_uint32.from_address(ctx + _OFF_FLAGS).value = _CONTEXT_FLAGS
                if not _k32.GetThreadContext(thread, ctx):
                    return None
                rsp = ctypes.c_uint64.from_address(ctx + _OFF_RSP).value
                self._copy_base, self._copy_len = rsp, 0
                done = ctypes.c_size_t(0)
                for size in (_STACK_COPY, 16 * 1024, 4096):  # the stack may end before 64 KB
                    if _k32.ReadProcessMemory(proc, ctypes.c_void_p(rsp), self._copy, size, ctypes.byref(done)):
                        self._copy_len = done.value
                        break
            finally:
                _k32.ResumeThread(thread)
                self.suspended += time.perf_counter() - held
            ctypes.memset(ctypes.addressof(frame), 0, ctypes.sizeof(frame))
            frame.AddrPC.Offset = ctypes.c_uint64.from_address(ctx + _OFF_RIP).value
            frame.AddrFrame.Offset = ctypes.c_uint64.from_address(ctx + _OFF_RBP).value
            frame.AddrStack.Offset = rsp
            frame.AddrPC.Mode = frame.AddrFrame.Mode = frame.AddrStack.Mode = 3  # AddrModeFlat
            stack = []
            for _ in range(self.depth):
                if not _dbghelp.StackWalk64(0x8664, proc, thread, ctypes.byref(frame), ctx, self._read_cb,
                                            _FUNC_TABLE, _MODULE_BASE, None):
                    break
                pc = frame.AddrPC.Offset
                if pc == 0 or (stack and pc == stack[-1]):
                    break
                stack.append(pc)
            return stack

else:

    class ThreadSampler:
        def __init__(self, *args, **kwargs):
            self.samples, self.error, self.started = [], "sampling needs Windows", None

        def start(self):
            pass

        def stop(self):
            return []
