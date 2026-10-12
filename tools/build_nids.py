"""Builds data/nids.json: NID -> name, plus Kyty/AnyPS5 coverage where known.

Sources, in priority order:
  1. The coverage spreadsheet (needs openpyxl): names plus Kyty and AnyPS5 status.
  2. data/symbols/aerolib.csv: "NID name" pairs. Each pair is re-hashed and kept only if it matches.
  3. data/symbols/*.txt: one candidate name per line. Each is hashed to get its NID.

Every name is verified by hashing, so a wrong entry in a list cannot mislabel a NID.
"""

import hashlib
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYMBOLS = ROOT / "data" / "symbols"
SUFFIX = bytes.fromhex("518d64a635ded8c1e6b039b1c3e55230")
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"


def nid(name):
    v = struct.unpack("<Q", hashlib.sha1(name.encode("utf-8", "surrogateescape") + SUFFIX).digest()[:8])[0]
    return "".join(ALPHABET[(v >> (58 - 6 * i)) & 0x3F] for i in range(10)) + ALPHABET[(v & 0xF) << 2]


def from_spreadsheet(path, out):
    from openpyxl import load_workbook

    ws = load_workbook(path, read_only=True)["All Functions"]
    for lib, name, n, source, in_kyty, anyps5, *_ in ws.iter_rows(min_row=2, values_only=True):
        entry = {"name": name, "library": lib, "kyty": in_kyty == "Yes",
                 "anyps5": anyps5 if anyps5 != "Not listed" else ""}
        if "unverified" in source:
            entry["unverified"] = True
        out[n] = entry


def _cxxfilt():
    """llvm-cxxfilt from Kyty Live's settings (config.json, or the Visual Studio install)."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from kyty_live.config import load_config

    return load_config(Path(__file__).resolve().parent.parent)["cxxfilt"]


def demangle(out):
    """Adds a readable "pretty" form for Itanium-mangled names (_Z...), if llvm-cxxfilt exists."""
    import subprocess

    keys = [k for k, v in out.items() if v["name"].startswith("_Z")]
    cxxfilt = _cxxfilt()
    if not keys or not cxxfilt or not Path(cxxfilt).exists():
        print("demangle: skipped")
        return
    text = "\n".join(out[k]["name"] for k in keys) + "\n"
    res = subprocess.run([cxxfilt], input=text.encode("utf-8", "surrogateescape"), capture_output=True, check=True)
    lines = res.stdout.decode("utf-8", "surrogateescape").splitlines()
    if len(lines) != len(keys):
        print(f"demangle: skipped, got {len(lines)} lines for {len(keys)} names")
        return
    done = 0
    for k, pretty in zip(keys, lines):
        if pretty != out[k]["name"]:
            out[k]["pretty"] = pretty
            done += 1
    print(f"demangle: {done} of {len(keys)} C++ names")


def main():
    out = {}
    sheet = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "KytyPS5_vs_AnyPS5_function_coverage.xlsx"
    if sheet.exists():
        from_spreadsheet(sheet, out)
        print(f"spreadsheet: {len(out)} NIDs")

    named = 0
    rejected = 0
    aerolib = SYMBOLS / "aerolib.csv"
    if aerolib.exists():
        for line in aerolib.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) != 2:
                continue
            n, name = parts
            if nid(name) != n:
                rejected += 1
                continue
            entry = out.setdefault(n, {})
            if not entry.get("name") or entry.get("unverified"):
                entry["name"] = name
                entry.pop("unverified", None)
                named += 1
        print(f"aerolib.csv: {named} names added, {rejected} pairs rejected (hash mismatch)")

    for txt in sorted(SYMBOLS.glob("*.txt")):
        added = 0
        for line in txt.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
            name = line.strip()
            if not name or " " in name or len(name) > 512:
                continue
            n = nid(name)
            entry = out.setdefault(n, {})
            if not entry.get("name") or entry.get("unverified"):
                entry["name"] = name
                entry.pop("unverified", None)
                added += 1
        print(f"{txt.name}: {added} names added")

    out = {k: v for k, v in out.items() if v.get("name")}
    demangle(out)
    dest = ROOT / "data" / "nids.json"
    dest.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8", errors="surrogateescape")
    print(f"total: {len(out)} named NIDs -> {dest} ({dest.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
