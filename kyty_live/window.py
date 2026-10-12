"""Finds the emulator window, reads its title, and sends key presses to it (Windows only)."""

import ctypes
import re
import sys
import time

# Pad-mapped keys from KytyPS5's DefaultKeyboardButton (hostInput.cpp). Space (pause) and
# Esc (quit) are window keys, so they are deliberately absent.
KEYS = {
    "J": (0x4A, "Cross"),
    "L": (0x4C, "Circle"),
    "I": (0x49, "Triangle"),
    "K": (0x4B, "Square"),
    "Enter": (0x0D, "Options"),
    "Up": (0x26, "D-pad up"),
    "Down": (0x28, "D-pad down"),
    "Left": (0x25, "D-pad left"),
    "Right": (0x27, "D-pad right"),
    "Q": (0x51, "L1"),
    "E": (0x45, "R1"),
    "Backspace": (0x08, "Touch pad"),
    "Tab": (0x09, "Touch pad"),
}
_KEY_LOOKUP = {k.lower(): k for k in KEYS}
_EXTENDED = {"Up", "Down", "Left", "Right"}

_TITLE = re.compile(r"frame: (\d+), fps: ([\d.]+)")


def parse_title(title):
    m = _TITLE.search(title or "")
    if not m:
        return {"frame": None, "fps": None}
    return {"frame": int(m.group(1)), "fps": float(m.group(2))}


def parse_macro(text):
    """Parses lines of '<seconds after launch> <key>'. '#' starts a comment."""
    steps = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"line {n}: expected '<seconds> <key>', got '{raw.strip()}'")
        try:
            at = float(parts[0])
        except ValueError:
            raise ValueError(f"line {n}: '{parts[0]}' is not a number of seconds") from None
        key = _KEY_LOOKUP.get(parts[1].lower())
        if key is None:
            raise ValueError(f"line {n}: unknown key '{parts[1]}'. Use one of: {', '.join(KEYS)}")
        steps.append((at, key))
    return steps


def cores_mask(cores, total):
    """Affinity mask for the first `cores` logical cores; None for no restriction."""
    if cores is None or cores == total:
        return None
    if not 1 <= cores <= total:
        raise ValueError(f"cores must be between 1 and {total}")
    return (1 << cores) - 1


def encode_png(bgra, width, height):
    """Encodes 32-bit BGRA pixels in bottom-up row order (as GDI returns them) as an RGB PNG."""
    import struct
    import zlib

    stride = width * 4
    rows = bytearray()
    for y in range(height - 1, -1, -1):
        row = bgra[y * stride:(y + 1) * stride]
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        rows += b"\x00" + rgb

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


if sys.platform == "win32":
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    _KEYEVENTF_EXTENDEDKEY = 0x1
    _KEYEVENTF_KEYUP = 0x2
    _KEYEVENTF_SCANCODE = 0x8
    _VK_MENU = 0x12

    def find_window(pid):
        """Returns (hwnd, title) of the largest visible top-level window owned by pid."""
        found = []

        def cb(hwnd, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                found.append(((rect.right - rect.left) * (rect.bottom - rect.top), hwnd, buf.value))
            return True

        user32.EnumWindows(_EnumProc(cb), 0)
        if not found:
            return None, ""
        _, hwnd, title = max(found)
        return hwnd, title

    def _send(vk, scan, flags):
        inp = _INPUT(type=1)
        inp.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
        return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def focus(hwnd):
        if user32.GetForegroundWindow() == hwnd:
            return True
        # Windows only lets the foreground process change focus; a synthetic Alt tap
        # lifts that restriction for the next SetForegroundWindow call.
        _send(_VK_MENU, 0, 0)
        _send(_VK_MENU, 0, _KEYEVENTF_KEYUP)
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.05)
        return user32.GetForegroundWindow() == hwnd

    def send_key(hwnd, key, hold=0.08):
        """Focuses the window and taps one pad key. Returns False if focus could not be taken."""
        vk = KEYS[key][0]
        if not focus(hwnd):
            return False
        scan = user32.MapVirtualKeyW(vk, 0)
        flags = _KEYEVENTF_SCANCODE | (_KEYEVENTF_EXTENDEDKEY if key in _EXTENDED else 0)
        _send(vk, scan, flags)
        time.sleep(hold)
        _send(vk, scan, flags | _KEYEVENTF_KEYUP)
        return True

    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    # Handles are pointer-sized; the ctypes default (int) would truncate them.
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    def limit_cores(pid, mask):
        """Restricts a process (and every thread it creates) to the cores in `mask`."""
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
        handle = k32.OpenProcess(0x0200 | 0x0400, False, pid)  # SET_INFORMATION | QUERY_INFORMATION
        if not handle:
            return False
        try:
            return bool(k32.SetProcessAffinityMask(handle, mask))
        finally:
            k32.CloseHandle(handle)

    def capture_png(hwnd):
        """Returns the window's client area as PNG bytes, or None. Works while it is covered."""
        rect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None
        src = user32.GetDC(hwnd)
        mem = gdi32.CreateCompatibleDC(src)
        bmp = gdi32.CreateCompatibleBitmap(src, w, h)
        old = gdi32.SelectObject(mem, bmp)
        try:
            # PW_CLIENTONLY | PW_RENDERFULLCONTENT: needed for swapchain-presented (Vulkan) windows.
            if not user32.PrintWindow(hwnd, mem, 0x1 | 0x2):
                return None
            info = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0) != h:
                return None
            return encode_png(buf.raw, w, h)
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(hwnd, src)

else:

    def find_window(pid):
        return None, ""

    def send_key(hwnd, key, hold=0.08):
        return False

    def capture_png(hwnd):
        return None

    def limit_cores(pid, mask):
        return False
