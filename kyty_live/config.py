"""Kyty Live's settings: built-in defaults, then the KYTY_SRC_DIR environment variable, then
config.json beside run.py. Nothing machine-specific is built in.

- The emulator source defaults to a KytyPS5 folder next to Kyty Live's own folder.
- Visual Studio tools are found with vswhere (Microsoft's installer locator).
- Relative paths in config.json start at Kyty Live's folder.
"""

import json
import os
import subprocess
from pathlib import Path

DEFAULTS = {
    "port": 8790,
    "src_dir": "",       # KytyPS5 checkout; default: ../KytyPS5 or KYTY_SRC_DIR
    "build_dir": "",     # default: <src_dir>/_Build/kyty-live
    "install_dir": "",   # default: <build_dir>/install
    "vcvars": "",        # default: from the Visual Studio install vswhere reports
    "symbolizer": "",
    "clang_cl": "",
    "cxxfilt": "",
    "game_dirs": [],     # default: the KytyPS5 launcher's list
    # The emulator's whole log (stub reports, HLE traces) is gated on --printf-direction,
    # which defaults to Silent; only fatal reports bypass it.
    "emulator_args": ["--no-splash", "--printf-direction", "Console"],
    "stall_seconds": 10,
    # Sets KYTY_HOT_HLE so a build with src/loader/hotHle.cpp loads hot/build/kyty_hot.dll.
    "hot_hle": True,
    # Sets KYTY_SHADER_OVERRIDE so a build with pipeline/shaderOverride.cpp dumps every shader
    # to shaders/dump and uses edited copies from shaders/override.
    "shader_override": True,
    "log_lines_per_second": 200,
}

PATH_KEYS = ("src_dir", "build_dir", "install_dir", "vcvars", "symbolizer", "clang_cl", "cxxfilt")
_VSWHERE = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / \
    "Microsoft Visual Studio/Installer/vswhere.exe"


def find_visual_studio():
    """The newest Visual Studio install with the C++ tools, or None."""
    if not _VSWHERE.exists():
        return None
    try:
        out = subprocess.run([str(_VSWHERE), "-latest", "-products", "*", "-requires",
                              "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property",
                              "installationPath"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    path = out.strip().splitlines()[0].strip() if out.strip() else ""
    return Path(path) if path else None


def visual_studio_tools(vs):
    llvm = vs / "VC/Tools/Llvm/x64/bin"
    return {
        "vcvars": str(vs / "VC/Auxiliary/Build/vcvars64.bat"),
        "symbolizer": str(llvm / "llvm-symbolizer.exe"),
        "clang_cl": str(llvm / "clang-cl.exe"),
        "cxxfilt": str(llvm / "llvm-cxxfilt.exe"),
    }


def resolve_config(root, user, env, find_vs=find_visual_studio):
    """Settings from the defaults, the environment and the user's config.json (`user`)."""
    root = Path(root)
    cfg = dict(DEFAULTS)
    cfg.update(user)

    def absolute(value):
        p = Path(value)
        return str(p if p.is_absolute() else (root / p).resolve())

    for key in PATH_KEYS:
        if cfg.get(key):
            cfg[key] = absolute(cfg[key])
    cfg["game_dirs"] = [absolute(d) for d in cfg.get("game_dirs") or []]

    if not cfg["src_dir"]:
        cfg["src_dir"] = env.get("KYTY_SRC_DIR") or str(root.parent / "KytyPS5")
    if not cfg["build_dir"]:
        cfg["build_dir"] = str(Path(cfg["src_dir"]) / "_Build" / "kyty-live")
    if not cfg["install_dir"]:
        cfg["install_dir"] = str(Path(cfg["build_dir"]) / "install")

    missing = [k for k in ("vcvars", "symbolizer", "clang_cl", "cxxfilt") if not cfg[k]]
    if missing:
        vs = find_vs()
        tools = visual_studio_tools(vs) if vs else {}
        for key in missing:
            cfg[key] = tools.get(key, "")
    return cfg


def load_config(root):
    path = Path(root) / "config.json"
    user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return resolve_config(root, user, os.environ)
