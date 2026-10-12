"""Reads the emulator's on/off options from its own --help, so the page's checkboxes always match
the build being tested."""

import re
import subprocess

_OPTION = re.compile(r"^\s{2}(--[a-z0-9-]+)(?:\s+<([^>]*)>)?\s+(\S.*)$")
_CONTINUATION = re.compile(r"^\s{20,}(\S.*)$")
_BOOL_ARGS = {"true|false", "t|f"}


def parse_help(text):
    """Returns the options that are switches (no value) or true/false, in --help order."""
    options = []
    current = None
    for line in text.splitlines():
        m = _OPTION.match(line)
        if m:
            flag, arg, desc = m.groups()
            current = None
            if arg is None:
                current = {"flag": flag, "kind": "switch", "help": desc.strip()}
            elif arg in _BOOL_ARGS:
                current = {"flag": flag, "kind": "bool", "help": desc.strip()}
            if current:
                options.append(current)
            continue
        c = _CONTINUATION.match(line)
        if c and current:
            current["help"] += " " + c.group(1).strip()
        else:
            current = None
    return options


def build_args(options, selected):
    """Turns the checked flags into command-line arguments, in --help order."""
    by_flag = {o["flag"]: o for o in options}
    unknown = [f for f in selected if f not in by_flag]
    if unknown:
        raise ValueError(f"not an on/off option of this emulator: {', '.join(unknown)}")
    args = []
    for o in options:
        if o["flag"] in selected:
            args += [o["flag"]] if o["kind"] == "switch" else [o["flag"], "true"]
    return args


def read_options(exe):
    try:
        out = subprocess.run([str(exe), "--help"], capture_output=True, text=True, timeout=30,
                             errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_help(out)


_DISPLAY = re.compile(r"^Display (\d+): (.*) (\d+)x(\d+) at (-?\d+),(-?\d+)( primary)?\s*$")


def parse_displays(text):
    """The displays from the emulator's --list-displays, in the order --display numbers them."""
    out = []
    for line in text.splitlines():
        m = _DISPLAY.match(line.strip())
        if m:
            index, name, w, h, x, y, primary = m.groups()
            out.append({"index": int(index), "name": name, "width": int(w), "height": int(h),
                        "x": int(x), "y": int(y), "primary": primary is not None})
    return out


def read_displays(exe):
    """[] when the build has no --list-displays (older builds) or it fails."""
    try:
        out = subprocess.run([str(exe), "--list-displays"], capture_output=True, text=True, timeout=30,
                             errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_displays(out)


def display_args(displays, index):
    """Arguments that open the game on display `index`; none for the default (primary) display."""
    if index is None:
        return []
    if not any(d["index"] == index for d in displays):
        raise ValueError(f"display {index} is not available to this emulator build")
    return ["--display", str(index)]
