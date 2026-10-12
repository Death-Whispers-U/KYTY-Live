"""Turns KytyPS5 log lines into deduplicated issues.

Each issue has a signature (a normalized form of the line) so the same problem seen
thousands of times is reported once with a count, and two runs can be compared.
"""

import re

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_STUBBED = re.compile(r"^Stubbed: \[[0-9a-fA-F]+\] <- [0-9a-fA-F]+, ([A-Za-z0-9+-]{11})\[([^\]]*)\]\[([^\]]*)\]")
_STUB_CALLED = re.compile(r"^Unresolved import stub called: (.*)$")
_HOST_EXC = re.compile(r"^Unhandled host exception: (.*)$")
_MISSING_FILE = re.compile(r"^Can't open (.*)$")
_DLSYM = re.compile(r"^unresolved dlsym: handle = \d+, module = (.*), symbol = (.*)$")
_DEVICE_LOST = re.compile(r"^(DeviceLost|VulkanFailure): (\S+) failed with (\S+)$")
_DEVICE_FAULT = re.compile(r"^DeviceFault: (.*)$")
_GPU_CHECKPOINT = re.compile(r"^GpuCheckpoint: (.*)$")
_SHADER_TRACE = re.compile(r"^ShaderTrace: (.*)$")
_SHADER_NAME = re.compile(r"\b(?:vs|hs|ds|gs|es|ls|ps|cs|ms)_[0-9a-f]{16}\b")
_VALIDATION = re.compile(r"Validation Error|VUID-")
_BLOCK_TITLE = re.compile(r"^--- (Fatal Error|Error) ---$")
_LOCATION = re.compile(r"(?:^|\s)in (\S+:\d+)\s*$")
_NID = re.compile(r"\b([A-Za-z0-9+-]{11})(?=\[)")

_NOISE = (
    re.compile(r"^\[\d+\]\[\d\d:\d\d:\d\d\.\d+\] "),  # per-call HLE trace header
    re.compile(r"^[A-Za-z_][\w ]*?\s*= "),  # parameter dump: "arg0 = 0x10"
    re.compile(r"^Equeue wait"),
    re.compile(r"^Mouse motion:"),
    re.compile(r"^\[Ok\]$"),
    re.compile(r"^Vulkan available extension:"),
)

SEVERITY = {
    "fatal": "critical",
    "host_exception": "critical",
    "stub_called": "critical",
    "device_lost": "critical",
    "vulkan_failure": "critical",
    "device_fault": "critical",
    "gpu_checkpoint": "critical",
    "list_cycle": "critical",
    "shader_trace": "info",
    "vulkan_validation": "error",
    "stall": "error",
    "missing_import": "warning",
    "unresolved_dlsym": "warning",
    "missing_file": "info",
}


def strip_ansi(s):
    return _ANSI.sub("", s)


def _normalize(s):
    s = re.sub(r"0x[0-9a-fA-F]+", "#", s)
    return re.sub(r"\b\d+\b", "N", s)


class Classifier:
    def __init__(self, nids):
        self.nids = nids
        self.issues = {}
        self._block = None

    def _nid_info(self, nid):
        """Returns (display name, raw symbol, AnyPS5 status). C++ names display demangled."""
        info = self.nids.get(nid) or {}
        symbol = info.get("name") or nid
        return info.get("pretty") or symbol, symbol, info.get("anyps5", "")

    def _emit(self, kind, sig, t, **fields):
        key = kind + ":" + sig
        issue = self.issues.get(key)
        if issue:
            issue["count"] += 1
            issue["last"] = t
            return []
        issue = {"sig": key, "kind": kind, "severity": SEVERITY[kind], "count": 1, "first": t, "last": t}
        issue.update(fields)
        self.issues[key] = issue
        return [dict(issue)]

    def report_stall(self, frame, seconds, t):
        """Records that no new frame was presented for `seconds` while the game was running."""
        return self._emit("stall", f"frame {frame}", t, text=f"No new frame for {seconds}s (stuck at frame {frame})")

    def is_noise(self, line):
        s = line.strip()
        return any(p.search(s) for p in _NOISE)

    def feed(self, raw, t):
        line = strip_ansi(raw).rstrip("\r\n")
        s = line.strip()

        if self._block is not None:
            self._block["lines"].append(s)
            loc = _LOCATION.search(s)
            if loc or len(self._block["lines"]) >= 12:
                text = " ".join(x for x in self._block["lines"] if x)
                where = loc.group(1) if loc else ""
                if loc:
                    text = _LOCATION.sub("", text).strip()
                self._block = None
                return self._emit("fatal", _normalize(text) + "@" + where, t, text=text, where=where)
            return []

        if _BLOCK_TITLE.match(s):
            self._block = {"lines": []}
            return []

        m = _STUBBED.match(s)
        if m:
            nid, lib, module = m.groups()
            name, symbol, anyps5 = self._nid_info(nid)
            return self._emit("missing_import", nid, t, nid=nid, name=name, symbol=symbol, library=lib, module=module,
                              anyps5=anyps5,
                              text=f"{name} ({lib})")

        m = _STUB_CALLED.match(s)
        if m:
            detail = m.group(1)
            nm = _NID.search(detail)
            nid = nm.group(1) if nm else ""
            name, symbol, anyps5 = self._nid_info(nid) if nid else (detail, detail, "")
            lib = re.search(r"\[([^\]]*)\]", detail)
            library = lib.group(1) if lib else ""
            return self._emit("stub_called", nid or detail, t, nid=nid, name=name, symbol=symbol, anyps5=anyps5,
                              library=library,
                              text=f"Game called missing {name} ({library})" if library else f"Game called missing {name}")

        m = _HOST_EXC.match(s)
        if m:
            return self._emit("host_exception", _normalize(m.group(1)), t, text=s)

        m = _DEVICE_LOST.match(s)
        if m:
            what, call, result = m.groups()
            kind = "device_lost" if what == "DeviceLost" else "vulkan_failure"
            text = f"GPU device lost in {call} ({result})" if kind == "device_lost" else f"{call} failed with {result}"
            return self._emit(kind, call + ":" + result, t, call=call, result=result, text=text)

        m = _DEVICE_FAULT.match(s)
        if m:
            return self._emit("device_fault", _normalize(m.group(1)), t, text=m.group(1))

        m = _GPU_CHECKPOINT.match(s)
        if m:
            text = m.group(1)
            # Each line is one marker; keep every one rather than merging look-alikes.
            return self._emit("gpu_checkpoint", text, t, text=text, shaders=_SHADER_NAME.findall(text))

        m = _SHADER_TRACE.match(s)
        if m:
            text = m.group(1).strip()
            kind = "list_cycle" if text.startswith("CYCLE") else "shader_trace"
            return self._emit(kind, text, t, text=text, shaders=_SHADER_NAME.findall(text))

        m = _DLSYM.match(s)
        if m:
            module, symbol = m.groups()
            return self._emit("unresolved_dlsym", module + "!" + symbol, t, text=f"{symbol} in {module}")

        m = _MISSING_FILE.match(s)
        if m:
            return self._emit("missing_file", m.group(1), t, text=m.group(1))

        if _VALIDATION.search(s):
            vuid = re.search(r"VUID-[\w-]+", s)
            return self._emit("vulkan_validation", vuid.group(0) if vuid else _normalize(s)[:160], t, text=s[:400])

        return []

    def summary(self):
        order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
        return sorted(self.issues.values(), key=lambda i: (order[i["severity"]], i["first"]))


def diff_runs(old, new):
    old_sigs = {i["sig"] for i in old}
    new_sigs = {i["sig"] for i in new}
    return {
        "new": [i for i in new if i["sig"] not in old_sigs],
        "gone": [i for i in old if i["sig"] not in new_sigs],
    }


_HOT_SLOTS = re.compile(r"^HotHLE: (patched|restored) ([A-Za-z0-9+-]{11}) (.*) \((\d+) slots\)$")
_HOT_WAITING = re.compile(r"^HotHLE: ([A-Za-z0-9+-]{11}) (.*) is not imported yet")
_HOT_ACTIVE = re.compile(r"^HotHLE: kyty_hot\.dll #(\d+) active with (\d+) functions$")


def parse_hot_line(raw):
    """Parses the emulator's HotHLE lines (src/loader/hotHle.cpp). Returns None for other lines."""
    s = strip_ansi(raw).strip()
    if s.startswith("HotHLE patch: "):
        return {"event": "patch_log", "text": s[len("HotHLE patch: "):]}
    if not s.startswith("HotHLE: "):
        return None
    m = _HOT_SLOTS.match(s)
    if m:
        event, nid, name, slots = m.groups()
        return {"event": event, "nid": nid, "name": name, "slots": int(slots),
                "text": f"{event} {name} ({slots} slots)"}
    m = _HOT_WAITING.match(s)
    if m:
        return {"event": "waiting", "nid": m.group(1), "name": m.group(2),
                "text": f"{m.group(2)} is ready; the game has not imported it yet"}
    m = _HOT_ACTIVE.match(s)
    if m:
        return {"event": "active", "generation": int(m.group(1)), "count": int(m.group(2)),
                "text": f"patch #{m.group(1)} active, {m.group(2)} functions"}
    text = s[len("HotHLE: "):]
    return {"event": "error" if text.startswith("cannot") or "invalid" in text or "does not export" in text else "info",
            "text": text}


_SHADER = re.compile(r"^ShaderOverride: (translated|applied|reverted|rejected) "
                     r"((cs|ps|vs|ms|ls|hs|ds)_([0-9a-f]{16})_[0-9a-f]{16})(?:: (.*))?$")


def parse_shader_line(raw):
    """Parses the emulator's ShaderOverride lines (shaderOverride.cpp, pipelineCache.cpp)."""
    m = _SHADER.match(strip_ansi(raw).strip())
    if not m:
        return None
    event, name, stage, shader_hash, error = m.groups()
    return {"event": event, "name": name, "stage": stage, "hash": shader_hash, "error": error or ""}
