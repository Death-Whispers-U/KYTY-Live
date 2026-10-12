"""Runs every installed game for a fixed time and records what fails.

Usage: python tools/sweep.py [seconds=60] [out dir]

Hot HLE and shader overrides stay off, so the emulator behaves like upstream. For each game it
writes <out>/<title id>/output.log, issues.json and end.png, and <out>/summary.json for all.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kyty_live.classifier import Classifier  # noqa: E402
from kyty_live.server import EXIT_CODES, load_config, scan_games  # noqa: E402
from kyty_live.window import capture_png, find_window, parse_title  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def run_game(cfg, nids, game, seconds, out):
    out.mkdir(parents=True, exist_ok=True)
    exe = Path(cfg["install_dir"]) / "kyty_emulator.exe"
    env = {k: v for k, v in os.environ.items() if k not in ("KYTY_HOT_HLE", "KYTY_SHADER_OVERRIDE")}
    start = time.time()
    proc = subprocess.Popen([str(exe), "--game", game["path"], *cfg["emulator_args"]], cwd=cfg["install_dir"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
    classifier = Classifier(nids)
    lock = threading.Lock()

    def read():
        with open(out / "output.log", "wb") as log:
            for raw in iter(proc.stdout.readline, b""):
                log.write(raw)
                line = raw.decode("utf-8", errors="replace")
                with lock:
                    classifier.feed(line, round(time.time() - start, 2))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    max_frame, last_fps, first_frame_at, hwnd = 0, None, None, None
    last_frame, last_change, stalled = None, time.time(), False
    while proc.poll() is None and time.time() - start < seconds:
        hwnd, title = find_window(proc.pid)
        info = parse_title(title)
        if info["frame"] is not None:
            if info["frame"] > 0 and first_frame_at is None:
                first_frame_at = round(time.time() - start, 1)
            max_frame, last_fps = max(max_frame, info["frame"]), info["fps"]
            if info["frame"] != last_frame:
                last_frame, last_change = info["frame"], time.time()
            elif not stalled and time.time() - last_change >= 10:
                stalled = True
                with lock:
                    classifier.report_stall(info["frame"], 10, round(time.time() - start, 1))
        time.sleep(1)

    alive = proc.poll() is None
    if alive and hwnd:
        png = capture_png(hwnd)
        if png:
            (out / "end.png").write_bytes(png)
    if alive:
        proc.kill()
    rc = proc.wait() & 0xFFFFFFFF
    reader.join(timeout=10)
    with lock:
        issues = classifier.summary()
    (out / "issues.json").write_text(json.dumps(issues, indent=1), encoding="utf-8")
    counts = {}
    for i in issues:
        counts.setdefault(i["severity"], {}).setdefault(i["kind"], 0)
        counts[i["severity"]][i["kind"]] += 1
    return {
        "id": game["id"], "title": game["title"], "seconds": round(time.time() - start, 1),
        "alive_at_end": alive,
        "exit": None if alive else EXIT_CODES.get(rc, f"exit code {rc:#x}"),
        "first_frame_s": first_frame_at, "max_frame": max_frame, "last_fps": last_fps,
        "stalled": stalled, "counts": counts,
        "critical": [i.get("text") or i["sig"] for i in issues if i["severity"] == "critical"][:12],
        "errors": [i.get("text") or i["sig"] for i in issues if i["severity"] == "error"][:8],
        "screenshot": str(out / "end.png") if (out / "end.png").exists() else None,
    }


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "sweeps" / time.strftime("%Y%m%d-%H%M%S")
    cfg = load_config()
    nids = json.loads((ROOT / "data" / "nids.json").read_text(encoding="utf-8"))
    games = scan_games(cfg["game_dirs"])
    print(f"{len(games)} games, {seconds:.0f}s each, emulator {cfg['install_dir']}", flush=True)
    summary = []
    for g in games:
        r = run_game(cfg, nids, g, seconds, out / g["id"])
        summary.append(r)
        (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        state = "alive" if r["alive_at_end"] else r["exit"]
        print(f"{g['id']} {g['title'][:40]:40} {state:30} frame {r['max_frame']:6} fps {r['last_fps']} "
              f"crit {sum(r['counts'].get('critical', {}).values())}", flush=True)
    print(f"done: {out}", flush=True)


if __name__ == "__main__":
    main()
