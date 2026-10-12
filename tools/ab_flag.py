"""A/B test of one emulator flag on one game: runs it N times without and N times with the flag.

Usage: python tools/ab_flag.py <title id> "<flag ...>" [runs per side=2] [seconds=60]
Example: python tools/ab_flag.py PPSA03528 "--redzone" 2 60

Runs alternate (off, on, off, on, ...) so drift over time does not favour one side.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kyty_live.server import EXIT_CODES, load_config, scan_games  # noqa: E402
from kyty_live.window import find_window, parse_title  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FAULT = re.compile(rb"Unhandled host exception: (.*)")


def run(cfg, game, extra, seconds, log_path):
    exe = Path(cfg["install_dir"]) / "kyty_emulator.exe"
    env = {k: v for k, v in os.environ.items() if k not in ("KYTY_HOT_HLE", "KYTY_SHADER_OVERRIDE")}
    start = time.time()
    proc = subprocess.Popen([str(exe), "--game", game["path"], *cfg["emulator_args"], *extra], cwd=cfg["install_dir"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
    fault = []

    def read():
        with open(log_path, "wb") as log:
            for raw in iter(proc.stdout.readline, b""):
                log.write(raw)
                m = FAULT.search(raw)
                if m and not fault:
                    fault.append(m.group(1).decode(errors="replace").strip())

    t = threading.Thread(target=read, daemon=True)
    t.start()
    max_frame = 0
    while proc.poll() is None and time.time() - start < seconds:
        _, title = find_window(proc.pid)
        f = parse_title(title)["frame"]
        if f:
            max_frame = max(max_frame, f)
        time.sleep(1)
    alive = proc.poll() is None
    if alive:
        proc.kill()
    rc = proc.wait() & 0xFFFFFFFF
    t.join(timeout=10)
    return {"alive": alive, "seconds": round(time.time() - start, 1), "max_frame": max_frame,
            "exit": None if alive else EXIT_CODES.get(rc, f"{rc:#x}"), "fault": fault[0] if fault else None}


def main():
    gid, flag = sys.argv[1], sys.argv[2].split()
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    seconds = float(sys.argv[4]) if len(sys.argv) > 4 else 60
    cfg = load_config()
    game = next(g for g in scan_games(cfg["game_dirs"]) if g["id"] == gid)
    out = ROOT / "ab" / f"{time.strftime('%Y%m%d-%H%M%S')}_{gid}"
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for i in range(n):
        for side, extra in (("off", []), ("on", flag)):
            r = run(cfg, game, extra, seconds, out / f"{side}{i + 1}.log")
            r["side"] = side
            results.append(r)
            print(f"{side:3} #{i + 1}: {'ALIVE' if r['alive'] else 'DIED '} {r['seconds']:5}s frame {r['max_frame']:5} "
                  f"{r['exit'] or ''} {r['fault'] or ''}", flush=True)
    (out / "results.json").write_text(json.dumps({"game": gid, "flag": flag, "runs": results}, indent=1))
    for side in ("off", "on"):
        rs = [r for r in results if r["side"] == side]
        print(f"{side}: {sum(r['alive'] for r in rs)}/{len(rs)} survived {seconds:.0f}s")


if __name__ == "__main__":
    main()
