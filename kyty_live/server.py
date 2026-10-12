"""Kyty Live: run a game, watch it fail, rebuild, and run it again.

Serves a local page on 127.0.0.1 and streams the emulator's classified output to it.
"""

import configparser
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as settings
from .freshsave import FreshSave
from .settings import Settings
from .classifier import Classifier, diff_runs, parse_hot_line, parse_shader_line, strip_ansi
from .hot import HOT_OUT, HOT_SRC, HotCompiler
from .history import compare, list_sweeps, load_sweep, report_markdown
from .options import build_args, display_args, read_displays, read_options
from .perf import (CompileMeter, GpuSampler, ThreadSampler, frame_stats, hitch_windows, parse_frame_batch,
                   summarize, verdict)
from . import sampler as stack_sampler
from .diagnostics import (CrashCollector, RunFacts, discover_test_suites, exe_return_addresses, loaded_modules,
                          module_for, parse_suite_output, suite_sources, symbolize)
from .loopcap import cap_loops, find_loops
from .snapshots import AutoSnapshots, snapshot_name
from .window import KEYS, capture_png, cores_mask, find_window, limit_cores, parse_macro, parse_title, send_key

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
RUNS = ROOT / "runs"
MACROS = ROOT / "macros"
SHADERS = ROOT / "shaders"
SWEEPS = ROOT / "sweeps"
TESTS = ROOT / "tests_runs"
SHADER_NAME = re.compile(r"^(cs|ps|vs|ms|ls|hs|ds)_[0-9a-f]{16}_[0-9a-f]{16}$")

# Exit codes worth naming. 321 is EXIT_HALT in src/common/assert.h.
EXIT_CODES = {
    0: "exited normally",
    321: "stopped by a fatal error (EXIT_HALT)",
    3221225477: "access violation (0xC0000005)",
    3221225725: "stack overflow (0xC00000FD)",
    3221226505: "stack buffer overrun (0xC0000409)",
    1: "killed",
}


def load_config():
    """Settings (see kyty_live/config.py and config.example.json)."""
    cfg = settings.load_config(ROOT)
    if not cfg["game_dirs"]:
        cfg["game_dirs"] = _launcher_game_dirs()
    return cfg


def _launcher_game_dirs():
    ini = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Kyty" / "Kyty.ini"
    if not ini.exists():
        return []
    p = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        p.read(ini, encoding="utf-8")
        raw = p.get("Launcher", "game_dirs", fallback="")
    except configparser.Error:
        return []
    return [d.strip() for d in raw.split(",") if d.strip()]


def scan_games(dirs, max_depth=4):
    games = []
    for root in dirs:
        if not os.path.isdir(root):
            continue
        base = root.rstrip("/\\").count(os.sep) + root.rstrip("/\\").count("/")
        for dp, dn, fn in os.walk(root):
            depth = dp.count(os.sep) + dp.count("/") - base
            if "eboot.bin" in fn:
                title_id, title = "", os.path.basename(dp)
                param = os.path.join(dp, "sce_sys", "param.json")
                if os.path.exists(param):
                    try:
                        j = json.loads(Path(param).read_text(encoding="utf-8", errors="replace"))
                        title_id = j.get("titleId", "")
                        title = (j.get("localizedParameters", {}).get("en-US", {}) or {}).get("titleName", title)
                    except (OSError, ValueError):
                        pass
                if title_id:
                    games.append({"id": title_id, "title": title, "path": os.path.normpath(dp)})
                dn[:] = []
            elif depth >= max_depth:
                dn[:] = []
    games.sort(key=lambda g: g["title"].lower())
    return games


class Hub:
    """Fan-out of events to every connected page."""

    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=5000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind, data):
        msg = (kind, data)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def _git(src, *args):
    try:
        return subprocess.run(["git", "-C", src, *args], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class Session:
    def __init__(self, cfg, hub, nids):
        self.cfg = cfg
        self.hub = hub
        self.nids = nids
        self.lock = threading.RLock()
        self.phase = "idle"
        self.proc = None
        self.run = None
        self.classifier = None
        self.stats = {"frame": None, "fps": None, "uptime": 0}
        self.recent = []
        self.build_lines = []
        self.last_game = None
        self._games = None
        self.hot = HotCompiler(cfg["vcvars"], cfg["clang_cl"], hub.publish)
        self.hot.watch()
        self.hot_state = {"patched": {}, "generation": None, "lines": []}
        self.shader_state = {"translated": {}, "status": {}}
        self.sweep = None
        self.autosnap_settings = {"every": 5, "keep": 12}
        self.autosnap = None
        self.settings = Settings(ROOT / "settings.json")  # choices every run follows
        # Runs with a fresh save park the player's save here and put it back afterwards.
        self.fresh_save = FreshSave(Path(cfg["install_dir"]) / "_SaveData")
        restored = self.fresh_save.recover(RUNS)
        if restored:
            print(f"Put back saves parked by an interrupted run: {', '.join(restored)}", flush=True)
        self.profile = None  # {"sampler", "thread", "run", "started"} while a profile is recording
        self.testing = None

    # ----- state -----

    def games(self, refresh=False):
        if self._games is None or refresh:
            self._games = scan_games(self.cfg["game_dirs"])
        return self._games

    def snapshot(self):
        with self.lock:
            return {
                "phase": self.phase,
                "autosnap": self.autosnap_settings,
                "run": self.run,
                "stats": self.stats,
                **issue_snapshot(self.classifier.summary() if self.classifier else []),
                "recent": self.recent[-400:],
                "build": self.build_lines[-200:],
                "last_game": self.last_game,
                "keys": {k: v[1] for k, v in KEYS.items()},
                "commit": _git(self.cfg["src_dir"], "rev-parse", "--short", "HEAD"),
                "sweep": self.sweep_view(),
                "testing": self.testing,
                "perf": (getattr(self, "perf", None) or [])[-180:] if self.phase == "running" else [],
                "frames": (getattr(self, "frames", None) or [])[-120:] if self.phase == "running" else [],
                "options": self.emulator_options(),
                "displays": self.displays(),
                "display": self.settings.get("display"),
                "fresh_save": bool(self.settings.get("fresh_save")),
                "always_args": self.cfg["emulator_args"],
                "hot": {"enabled": self.cfg["hot_hle"], "auto": self.hot.auto, "last": self.hot.last,
                        "sources": [str(p) for p in self.hot.sources()], "folder": str(HOT_SRC),
                        **self.hot_state},
                "shaders": {"enabled": self.cfg["shader_override"], "folder": str(SHADERS),
                            "translated": list(self.shader_state["translated"].values())[-2000:],
                            "dumped": dumped_shaders(),
                            "status": self.shader_state["status"], "overrides": overrides()},
            }

    def _set_phase(self, phase):
        self.phase = phase
        self.hub.publish("phase", {"phase": phase})

    # ----- running a game -----

    def start(self, game, dev_features=True, extra_args=(), cores=None, fresh_save=None):
        with self.lock:
            if self.phase in ("running", "building"):
                raise RuntimeError(f"already {self.phase}")
            exe = Path(self.cfg["install_dir"]) / "kyty_emulator.exe"
            if not exe.exists():
                raise RuntimeError(f"emulator not found at {exe}. Build and install first.")
            macro_text = self.macro(game["id"])
            steps = parse_macro(macro_text)
            run_id = time.strftime("%Y%m%d-%H%M%S") + "_" + game["id"]
            run_dir = RUNS / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            tool_flags = [f for f in TOOL_FLAGS if any(o["flag"] == f for o in self.emulator_options())]
            args = [str(exe), "--game", game["path"], *self.cfg["emulator_args"], *tool_flags, *extra_args]
            commit = _git(self.cfg["src_dir"], "rev-parse", "--short", "HEAD")
            dirty = len([l for l in _git(self.cfg["src_dir"], "status", "--porcelain", "-uno").splitlines() if l.strip()])
            self.run = {"id": run_id, "game": game, "args": args[1:], "start": time.time(), "end": None,
                        "exit_code": None, "exit_reason": None, "commit": commit, "dirty_files": dirty,
                        "max_frame": 0, "last_fps": None, "first_frame_s": None, "macro": macro_text,
                        "dev_features": dev_features}
            self.classifier = Classifier(self.nids)
            self.stats = {"frame": None, "fps": None, "uptime": 0}
            self.recent = []
            self.last_game = game
            env = dict(os.environ)
            # Third-party Vulkan overlays must never sit between the emulator and the driver during
            # testing. GOG Galaxy's implicit layer honours this off switch (its manifest's
            # disable_environment); the setting applies to this process only.
            env["DISABLE_GALAXY_OVERLAY"] = "1"
            env.pop("ENABLE_GALAXY_OVERLAY", None)
            if self.cfg["hot_hle"] and dev_features:
                env["KYTY_HOT_HLE"] = str(HOT_OUT)
                # Copies the emulator loaded in earlier runs; a running emulator keeps its own locked.
                for old in (HOT_OUT / ".loaded").glob("*.dll"):
                    try:
                        old.unlink()
                    except OSError:
                        pass
            self.hot_state = {"patched": {}, "generation": None, "lines": []}
            self.shader_state = {"translated": {}, "status": {}}
            if self.cfg["shader_override"] and dev_features:
                env["KYTY_SHADER_OVERRIDE"] = str(SHADERS)
            if fresh_save is None:
                fresh_save = bool(self.settings.get("fresh_save"))
            if fresh_save:
                # The game boots as if never played; the player's save comes back when it exits.
                self.fresh_save.park(game["id"])
                self.run["fresh_save"] = True
            try:
                self.proc = subprocess.Popen(args, cwd=self.cfg["install_dir"], stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
            except OSError:
                if fresh_save:
                    self.fresh_save.restore(game["id"], run_dir)
                raise
            self.run["pid"] = self.proc.pid
            # Fewer CPU cores changes how the game's threads interleave: an experiment for races.
            mask = cores_mask(cores, os.cpu_count())
            if mask is not None:
                self.run["cores"] = cores
                if not limit_cores(self.proc.pid, mask):
                    self.hub.publish("notice", {"text": f"Could not limit the game to {cores} cores"})
            self.compile_meter = CompileMeter()
            self.crash = CrashCollector()
            self.facts = RunFacts()
            self.autosnap = AutoSnapshots(**self.autosnap_settings)
            self.modules = []
            self.perf = []
            self.frames = []  # [t, [interval ms, ...]] per second, from --frame-times
            self._set_phase("running")
            self.hub.publish("run", self.run)
        log = open(run_dir / "output.log", "wb")
        threading.Thread(target=self._read, args=(self.proc, log), daemon=True).start()
        threading.Thread(target=self._watch, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._auto_snapshots, args=(self.proc,), daemon=True).start()
        if steps:
            threading.Thread(target=self._replay, args=(self.proc, steps), daemon=True).start()
        return self.run

    def _read(self, proc, log):
        budget, window_start, dropped = self.cfg["log_lines_per_second"], time.time(), 0
        start = self.run["start"]
        for raw in iter(proc.stdout.readline, b""):
            log.write(raw)
            line = raw.decode("utf-8", errors="replace")
            t = round(time.time() - start, 2)
            with self.lock:
                events = self.classifier.feed(line, t)
                self.compile_meter.feed(line.strip())
                self.facts.feed(line)
                had_crash = self.crash.crash is not None
                self.crash.feed(line)
                new_crash = not had_crash and self.crash.crash is not None
            if new_crash:
                threading.Thread(target=self._analyze_crash, args=(dict(self.crash.crash), list(self.modules)),
                                 daemon=True).start()
            for ev in events:
                self.hub.publish("issue", ev)
                if AutoSnapshots.triggers(ev) and self.autosnap.claim_event():
                    threading.Thread(target=self._event_snapshot, args=(proc, ev["kind"]), daemon=True).start()
            frame_batch = parse_frame_batch(line)
            if frame_batch is not None:
                frame_times, age = frame_batch
                batch = [t, frame_times, age]
                with self.lock:
                    self.frames.append(batch)
                self.hub.publish("frames", {"t": t, "ms": frame_times, "age": age})
                continue  # data for the frame-time graph, not log text
            hot = parse_hot_line(line)
            if hot:
                hot["t"] = t
                with self.lock:
                    st = self.hot_state
                    if hot["event"] == "patched":
                        st["patched"][hot["nid"]] = hot["name"]
                    elif hot["event"] == "restored":
                        st["patched"].pop(hot["nid"], None)
                    elif hot["event"] == "active":
                        st["generation"] = hot["generation"]
                    st["lines"] = (st["lines"] + [hot])[-100:]
                self.hub.publish("hot", hot)
                continue  # shown once, as a formatted "hot" event
            shader = parse_shader_line(line)
            if shader:
                shader["t"] = t
                with self.lock:
                    st = self.shader_state
                    if shader["event"] == "translated":
                        st["translated"][shader["name"]] = {"name": shader["name"], "stage": shader["stage"],
                                                            "hash": shader["hash"], "t": t}
                    else:
                        st["status"][shader["name"]] = {"event": shader["event"], "error": shader["error"], "t": t}
                self.hub.publish("shader", shader)
                continue
            if self.classifier.is_noise(line):
                continue
            now = time.time()
            if now - window_start >= 1:
                if dropped:
                    self.hub.publish("log", {"t": t, "text": f"... {dropped} lines not shown (full log on disk)", "dim": True})
                window_start, budget, dropped = now, self.cfg["log_lines_per_second"], 0
            if budget <= 0:
                dropped += 1
                continue
            budget -= 1
            entry = {"t": t, "text": strip_ansi(line).rstrip("\r\n")[:600]}
            with self.lock:
                self.recent.append(entry)
                if len(self.recent) > 2000:
                    del self.recent[:1000]
            self.hub.publish("log", entry)
        log.close()
        rc = proc.wait()
        self._finished(proc, rc)

    def _watch(self, proc):
        stall_after = self.cfg["stall_seconds"]
        last_frame, last_change, stalled = None, time.time(), False
        threads, gpu = ThreadSampler(proc.pid), GpuSampler(proc.pid)
        last_modules = 0.0
        first = True
        while proc.poll() is None:
            hwnd, title = find_window(proc.pid)
            info = parse_title(title)
            with self.lock:
                if self.proc is not proc:
                    return
                self.stats = {**info, "uptime": round(time.time() - self.run["start"], 1), "window": bool(hwnd)}
                if info["frame"] is not None:
                    if info["frame"] > 0 and self.run.get("first_frame_s") is None:
                        self.run["first_frame_s"] = round(time.time() - self.run["start"], 1)
                    self.run["max_frame"] = max(self.run["max_frame"], info["frame"])
                    self.run["last_fps"] = info["fps"]
            self.hub.publish("stats", self.stats)
            self._sample_perf(threads, gpu, info, first)
            if time.time() - last_modules >= 2:  # for crash analysis: which DLL holds a faulting pc
                mods = loaded_modules(proc.pid)
                if mods:
                    with self.lock:
                        self.modules = mods
                last_modules = time.time()
            first = False
            frame = info["frame"]
            if frame is not None:
                if frame != last_frame:
                    last_frame, last_change = frame, time.time()
                    if stalled:
                        stalled = False
                        self.hub.publish("log", {"t": self.stats["uptime"], "text": f"Frames moving again at {frame}", "dim": True})
                elif not stalled and time.time() - last_change >= stall_after:
                    stalled = True
                    with self.lock:
                        evs = self.classifier.report_stall(frame, stall_after, self.stats["uptime"])
                    for ev in evs:
                        self.hub.publish("issue", ev)
            time.sleep(1)
        gpu.close()

    def _sample_perf(self, threads, gpu, info, first):
        """One second of performance data and what limited it."""
        busy = threads.sample(time.time())[:12]
        engines = gpu.sample()
        with self.lock:
            ms, compiles = self.compile_meter.take()
            pipeline_ms, pipelines = self.compile_meter.take_pipelines()
        if first:  # thread CPU needs two readings
            return
        s = {"t": self.stats["uptime"], "fps": info["fps"], "frame": info["frame"], "threads": busy,
             "gpu": engines, "compile_ms": ms, "compiles": compiles,
             "pipeline_ms": pipeline_ms, "pipelines": pipelines}
        s["verdict"] = verdict(s)
        with self.lock:
            self.perf.append(s)
        self.hub.publish("perf", s)

    def _replay(self, proc, steps):
        start = self.run["start"]
        for at, key in steps:
            delay = start + at - time.time()
            if delay > 0:
                time.sleep(delay)
            if proc.poll() is not None or self.proc is not proc:
                return
            hwnd, _ = find_window(proc.pid)
            ok = bool(hwnd) and send_key(hwnd, key)
            self.hub.publish("input", {"t": round(time.time() - start, 2), "key": key, "ok": ok,
                                       "note": "" if ok else ("no window yet" if not hwnd else "could not focus the window")})

    def _finished(self, proc, rc):
        with self.lock:
            if self.proc is not proc:
                return
            if self.run.get("fresh_save"):
                try:
                    self.fresh_save.restore(self.run["game"]["id"], RUNS / self.run["id"])
                    self.hub.publish("notice", {"text": f"Your save of {self.run['game']['title']} is back; "
                                                        "the run's own save is kept with the run"})
                except OSError as e:
                    self.hub.publish("notice", {"text": f"Could not put the save back yet ({e}); "
                                                        "it is restored the next time Kyty Live starts"})
            code = rc & 0xFFFFFFFF
            self.run["end"] = time.time()
            self.run["exit_code"] = code
            self.run["exit_reason"] = self.run.get("stopped_by_user") and "stopped from Kyty Live" or EXIT_CODES.get(code, f"exit code {code:#x}")
            issues = self.classifier.summary()
            run_dir = RUNS / self.run["id"]
            self.run["perf"] = summarize(self.perf)
            self.run["facts"] = dict(self.facts.facts)
            (run_dir / "perf.json").write_text(json.dumps(self.perf), encoding="utf-8")
            if self.frames:
                self.run["frames"] = frame_stats([ms for batch in self.frames for ms in batch[1]])
                (run_dir / "frames.json").write_text(json.dumps(self.frames), encoding="utf-8")
            (run_dir / "issues.json").write_text(json.dumps(issues, indent=1), encoding="utf-8")
            (run_dir / "meta.json").write_text(json.dumps(self.run, indent=1), encoding="utf-8")
            prev = self._previous_run(self.run)
            diff = None
            if prev:
                diff = diff_runs(prev["issues"], issues)
                diff["previous"] = prev["meta"]
            if self.profile is not None:
                threading.Thread(target=self._finish_profile, daemon=True).start()
            self.proc = None
            self._set_phase("exited")
            self.hub.publish("exit", {"run": self.run, "diff": diff})

    def _previous_run(self, run):
        for d in sorted(RUNS.iterdir(), reverse=True):
            if d.name == run["id"] or not d.name.endswith("_" + run["game"]["id"]):
                continue
            meta, iss = d / "meta.json", d / "issues.json"
            if meta.exists() and iss.exists():
                return {"meta": json.loads(meta.read_text(encoding="utf-8")),
                        "issues": json.loads(iss.read_text(encoding="utf-8"))}
        return None

    def stop(self):
        with self.lock:
            proc = self.proc
            if proc is None:
                return False
            self.run["stopped_by_user"] = True
        proc.kill()
        proc.wait(timeout=15)
        # _read finishes the run once the pipe closes.
        for _ in range(50):
            if self.proc is None:
                break
            time.sleep(0.1)
        return True

    # ----- building -----

    def emulator_options(self, refresh=False):
        """On/off options of the installed emulator, read from its --help (cached until a rebuild)."""
        if refresh or getattr(self, "_options", None) is None:
            exe = Path(self.cfg["install_dir"]) / "kyty_emulator.exe"
            always = set(self.cfg["emulator_args"]) | set(TOOL_FLAGS)
            self._options = [dict(o, always=o["flag"] in always) for o in read_options(exe)] if exe.exists() else []
        return self._options

    def display_args(self, index=None):
        """--display for a run: the monitor the request names, else the one chosen in the page."""
        if index not in (None, ""):
            return display_args(self.displays(), int(index))
        saved = self.settings.get("display")
        if saved is None:
            return []
        try:
            return display_args(self.displays(), saved)
        except ValueError:
            self.hub.publish("notice", {"text": f"Monitor {saved} is not connected; using the primary monitor"})
            return []

    def set_display(self, index):
        if index not in (None, ""):
            display_args(self.displays(), int(index))  # validates
        self.settings.set("display", None if index in (None, "") else int(index))
        return {"display": self.settings.get("display")}

    def displays(self, refresh=False):
        """Displays the installed emulator can open on (--list-displays), cached like the options."""
        if refresh or getattr(self, "_displays", None) is None:
            exe = Path(self.cfg["install_dir"]) / "kyty_emulator.exe"
            self._displays = read_displays(exe) if exe.exists() else []
        return self._displays

    def option_args(self, selected):
        """Command-line arguments for the options checked in the page, minus the always-on ones."""
        opts = [o for o in self.emulator_options() if not o["always"]]
        return build_args(opts, [f for f in (selected or []) if f not in self.cfg["emulator_args"]])

    def rebuild(self, rerun, game=None, extra_args=()):
        with self.lock:
            if self.phase == "building":
                raise RuntimeError("a build is already running")
            game = game or self.last_game
        if self.proc is not None:
            self.stop()
        with self.lock:
            self.build_lines = []
            self._set_phase("building")
        threading.Thread(target=self._build, args=(rerun, game, list(extra_args)), daemon=True).start()

    def _build(self, rerun, game, extra_args):
        b = self.cfg["build_dir"]
        # A script file avoids Python re-escaping the quotes in a cmd /c string.
        script = RUNS / "_build.cmd"
        script.write_text(
            "@echo off\r\n"
            f'call "{self.cfg["vcvars"]}" >nul || exit /b 90\r\n'
            f'cmake --build "{b}" --parallel || exit /b 91\r\n'
            f'cmake --install "{b}" --prefix "{self.cfg["install_dir"]}" || exit /b 92\r\n',
            encoding="ascii")
        started = time.time()
        p = subprocess.Popen(["cmd", "/d", "/c", str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL)
        progress = re.compile(r"^\[(\d+)/(\d+)\]")
        errors = 0
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            m = progress.match(line)
            is_error = bool(re.search(r"\berror\b|FAILED:", line))
            errors += is_error
            if m:
                self.hub.publish("build_progress", {"done": int(m.group(1)), "total": int(m.group(2))})
            if is_error or not m and not line.startswith("-- Up-to-date") and not line.startswith("-- Installing"):
                entry = {"text": line[:600], "error": is_error}
                with self.lock:
                    self.build_lines.append(entry)
                self.hub.publish("build", entry)
        rc = p.wait()
        ok = rc == 0
        if ok:
            self.emulator_options(refresh=True)  # the new build may have new options
            self.displays(refresh=True)
        stage = {90: "loading the Visual Studio environment", 91: "compiling", 92: "installing"}.get(rc, "")
        self.hub.publish("build_done", {"ok": ok, "seconds": round(time.time() - started, 1), "errors": errors,
                                        "stage": stage})
        with self.lock:
            self._set_phase("idle" if ok else "build_failed")
        if ok and rerun and game:
            try:
                self.start(game, extra_args=extra_args)
            except Exception as e:  # noqa: BLE001 - reported to the page
                self.hub.publish("notice", {"text": f"Could not relaunch: {e}"})

    # ----- macros -----

    def macro(self, title_id):
        p = MACROS / f"{title_id}.txt"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def save_macro(self, title_id, text):
        parse_macro(text)
        MACROS.mkdir(exist_ok=True)
        (MACROS / f"{title_id}.txt").write_text(text, encoding="utf-8")

    # ----- sweep: every game for a fixed time -----

    def sweep_view(self):
        s = self.sweep
        return None if s is None else {k: v for k, v in s.items() if k != "cancel"}

    def start_sweep(self, seconds, game_ids=None, extra_args=(), repeats=1):
        with self.lock:
            if self.sweep and self.sweep["running"]:
                raise RuntimeError("a sweep is already running")
            if self.phase in ("running", "building"):
                raise RuntimeError(f"stop the current {'game' if self.phase == 'running' else 'build'} first")
            # Re-scan: games added or deleted since the server started must not linger in the list.
            games = [g for g in self.games(refresh=True) if not game_ids or g["id"] in game_ids]
            # Repeats run the whole list again, so runs of one game are spread over time.
            games = games * max(1, int(repeats))
            if not games:
                raise RuntimeError("no games to sweep")
            sweep_id = time.strftime("%Y%m%d-%H%M%S")
            self.sweep = {"id": sweep_id, "running": True, "seconds": seconds, "total": len(games), "index": 0,
                          "current": None, "rows": [], "cancel": False, "started": time.time(),
                          "folder": str(SWEEPS / sweep_id), "flags": list(extra_args),
                          "repeats": max(1, int(repeats)), "games": list(dict.fromkeys(g["id"] for g in games))}
        self.hub.publish("sweep", self.sweep_view())
        threading.Thread(target=self._sweep, args=(games,), daemon=True).start()
        return self.sweep_view()

    def stop_sweep(self):
        if self.sweep and self.sweep["running"]:
            self.sweep["cancel"] = True
            self.stop()

    def _sweep(self, games):
        sw = self.sweep
        for i, game in enumerate(games):
            if sw["cancel"]:
                break
            sw["index"], sw["current"] = i + 1, game["title"]
            self.hub.publish("sweep", self.sweep_view())
            try:
                self.start(game, dev_features=False, extra_args=sw["flags"])
                with self.lock:
                    self.run["sweep"] = sw["id"]
            except Exception as e:  # noqa: BLE001 - recorded in the row
                sw["rows"].append({"id": game["id"], "title": game["title"], "outcome": f"did not start: {e}",
                                   "crashed": True})
                continue
            proc = self.proc
            end = time.time() + sw["seconds"]
            while proc.poll() is None and time.time() < end and not sw["cancel"]:
                time.sleep(0.5)
            snap = None
            if proc.poll() is None:
                try:
                    snap = self.snapshot_window()["url"]
                except RuntimeError:
                    pass
                self.stop()
            for _ in range(60):  # let _read record the exit
                if self.proc is None:
                    break
                time.sleep(0.25)
            sw["rows"].append(self._sweep_row(game, snap))
            SWEEPS.mkdir(exist_ok=True)
            (SWEEPS / sw["id"]).mkdir(exist_ok=True)
            (SWEEPS / sw["id"] / "summary.json").write_text(json.dumps(sw["rows"], indent=1), encoding="utf-8")
            (SWEEPS / sw["id"] / "meta.json").write_text(json.dumps(
                {"seconds": sw["seconds"], "flags": sw["flags"], "started": sw["started"],
                 "repeats": sw["repeats"], "games": sw["games"],
                 "commit": _git(self.cfg["src_dir"], "rev-parse", "--short", "HEAD")}, indent=1), encoding="utf-8")
            self.hub.publish("sweep", self.sweep_view())
        sw["running"], sw["current"] = False, None
        self.hub.publish("sweep", self.sweep_view())

    def _sweep_row(self, game, snap):
        run = self.run or {}
        issues = self.classifier.summary() if self.classifier else []
        counts = {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "error", "warning", "info")}
        ran_full = bool(run.get("stopped_by_user")) and not self.sweep["cancel"]
        crashed = not run.get("stopped_by_user") and run.get("exit_code") not in (None, 0)
        return {
            "id": game["id"], "title": game["title"], "run": run.get("id"),
            "outcome": "ran the full time" if ran_full else (run.get("exit_reason") or "unknown"),
            "crashed": crashed,
            "cancelled": bool(self.sweep["cancel"]),
            "seconds": round((run.get("end") or time.time()) - run.get("start", time.time()), 1),
            "first_frame_s": run.get("first_frame_s"), "max_frame": run.get("max_frame", 0),
            "last_fps": run.get("last_fps"),
            "stalled": any(i["kind"] == "stall" for i in issues),
            "counts": counts,
            "critical": [i.get("text") or i["sig"] for i in issues if i["severity"] == "critical"][:8],
            "errors": [i.get("text") or i["sig"] for i in issues if i["severity"] == "error"][:5],
            "snapshot": snap,
            "perf": run.get("perf"),
        }

    def _analyze_crash(self, crash, modules):
        """Where a host exception happened: the module holding pc, and the emulator functions whose
        return addresses are on the stack (a raw scan, so stale frames can appear)."""
        run = self.run
        where = module_for(modules, crash["pc"])
        exe = Path(self.cfg["install_dir"]) / "kyty_emulator.exe"
        frames = symbolize(self.cfg["symbolizer"], exe, exe_return_addresses(modules, crash["stack"]))
        result = {
            "pc": f"{crash['pc']:#x}", "address": f"{crash['address']:#x}", "access": crash["access"],
            "code": f"{crash['code'] & 0xFFFFFFFF:#x}", "thread": crash["thread"],
            "module": where[0] if where else None, "module_offset": f"{where[1]:#x}" if where else None,
            "frames": [{"address": f"{f['address']:#x}", "function": f["function"]} for f in frames],
            "modules_known": bool(modules),
        }
        with self.lock:
            run["crash"] = result
            if run.get("end") is not None:  # the run already finished: update its saved record
                (RUNS / run["id"] / "meta.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
        self.hub.publish("crash", {"run": run["id"], **result})

    # ----- C++ test suites -----

    def test_suites(self):
        cmake = Path(self.cfg["src_dir"]) / "CMakeLists.txt"
        if not cmake.exists():
            return []
        text = cmake.read_text(encoding="utf-8", errors="replace")
        return discover_test_suites(text, suite_sources(text, self.cfg["src_dir"]))

    def start_tests(self, selection):
        names = {s["name"] for s in self.test_suites()}
        chosen = []
        for item in selection or []:
            name, mode = item.get("name"), item.get("mode") or ""
            if name not in names:
                raise ValueError(f"unknown test suite: {name}")
            if mode and not re.fullmatch(r"--[a-z0-9-]+", mode):
                raise ValueError(f"not a test mode: {mode}")
            chosen.append({"name": name, "mode": mode})
        if not chosen:
            raise ValueError("pick at least one test suite")
        with self.lock:
            if self.phase in ("running", "building", "testing"):
                raise RuntimeError(f"wait until the current {self.phase} finishes")
            test_id = time.strftime("%Y%m%d-%H%M%S")
            self.testing = {"id": test_id, "running": True, "selection": chosen, "results": [], "current": None,
                            "started": time.time(), "commit": _git(self.cfg["src_dir"], "rev-parse", "--short", "HEAD")}
            self._set_phase("testing")
        self.hub.publish("tests", self.testing)
        threading.Thread(target=self._run_tests, args=(chosen,), daemon=True).start()
        return self.testing

    def _run_tests(self, chosen):
        t = self.testing
        folder = TESTS / t["id"]
        folder.mkdir(parents=True, exist_ok=True)
        build = Path(self.cfg["build_dir"])
        try:
            env = self.hot._msvc_env()
            targets = sorted({c["name"] for c in chosen})
            t["current"] = "building " + ", ".join(targets)
            self.hub.publish("tests", t)
            p = subprocess.run(["cmake", "--build", str(build), "--target", *targets], capture_output=True,
                               text=True, env=env, errors="replace")
            (folder / "build.log").write_text(p.stdout + p.stderr, encoding="utf-8")
            if p.returncode != 0:
                errors = [x for x in (p.stdout + p.stderr).splitlines() if "error" in x.lower()][:10]
                t["results"].append({"name": "build", "mode": "", "passed": False, "seconds": 0, "cases_ok": 0,
                                     "failure": "; ".join(errors) or "the build failed", "log": "build.log"})
            else:
                for c in chosen:
                    t["current"] = f"{c['name']} {c['mode']}".strip()
                    self.hub.publish("tests", t)
                    started = time.time()
                    exe = build / f"{c['name']}.exe"
                    try:
                        r = subprocess.run([str(exe), *([c["mode"]] if c["mode"] else [])], cwd=str(build),
                                           capture_output=True, text=True, timeout=1800, errors="replace")
                        out, rc = r.stdout + r.stderr, r.returncode
                    except subprocess.TimeoutExpired as e:
                        out, rc = str(e.stdout or "") + "\n(timed out after 30 minutes)", -1
                    log = f"{c['name']}{'_' + c['mode'].strip('-') if c['mode'] else ''}.log"
                    (folder / log).write_text(out, encoding="utf-8")
                    res = parse_suite_output(out, rc)
                    t["results"].append({**c, **res, "seconds": round(time.time() - started, 1), "log": log})
                    self.hub.publish("tests", t)
        except Exception as e:  # noqa: BLE001 - reported to the page
            t["results"].append({"name": "runner", "mode": "", "passed": False, "seconds": 0, "cases_ok": 0,
                                 "failure": str(e), "log": None})
        t["running"], t["current"] = False, None
        (folder / "summary.json").write_text(json.dumps(t, indent=1), encoding="utf-8")
        with self.lock:
            self._set_phase("idle")
        self.hub.publish("tests", t)

    def snapshot_window(self, reason="", proc=None):
        with self.lock:
            run = self.run
            if proc is None:
                proc = self.proc
            elif proc is not self.proc:
                raise RuntimeError("that run has ended")
        if proc is None:
            raise RuntimeError("no game is running")
        hwnd, _ = find_window(proc.pid)
        png = capture_png(hwnd) if hwnd else None
        if not png:
            raise RuntimeError("could not capture the emulator window")
        t = round(time.time() - run["start"], 1)
        name = snapshot_name(t, reason)
        (RUNS / run["id"] / name).write_bytes(png)
        return {"run": run["id"], "file": name, "url": f"/api/files/{run['id']}/{name}", "t": t, "reason": reason}

    def set_autosnap(self, every, keep):
        AutoSnapshots(every, keep)  # validates
        with self.lock:
            self.autosnap_settings = {"every": every, "keep": keep}
        return self.autosnap_settings

    def _auto_snapshots(self, proc):
        """Takes a periodic picture of the window so the screen before a failure is on record."""
        while proc.poll() is None and self.proc is proc:
            time.sleep(0.5)
            with self.lock:
                if self.proc is not proc:
                    return
                policy, run = self.autosnap, self.run
                due = policy.due(time.time() - run["start"])
            if not due:
                continue
            try:
                shot = self.snapshot_window("auto", proc)
            except (RuntimeError, OSError):
                continue  # the window is not open yet, or the run just ended
            with self.lock:
                drop = policy.taken(shot["t"], shot["file"])
            for name in drop:
                try:
                    (RUNS / run["id"] / name).unlink()
                except OSError:
                    pass

    def _event_snapshot(self, proc, kind):
        """Captures the window the moment a run-ending problem is logged."""
        try:
            shot = self.snapshot_window(kind, proc)
        except (RuntimeError, OSError):
            return
        self.hub.publish("snapshot", shot)

    # ----- thread profiling -----

    def start_profile(self, thread_name="Thread_Gpu"):
        with self.lock:
            proc, run = self.proc, self.run
            if proc is None:
                raise RuntimeError("no game is running")
            if self.profile is not None:
                raise RuntimeError("a profile is already recording")
        tid = ThreadSampler(proc.pid).find(thread_name)
        if tid is None:
            raise RuntimeError(f"the emulator has no thread named {thread_name} yet")
        s = stack_sampler.ThreadSampler(proc.pid, tid)
        s.start()
        with self.lock:
            self.profile = {"sampler": s, "thread": thread_name, "run": run["id"], "pid": proc.pid,
                            "started": round(time.time() - run["start"], 1)}
        self.hub.publish("profile", {"state": "recording", "thread": thread_name,
                                     "started": self.profile["started"]})
        return {"state": "recording", "thread": thread_name}

    def _finish_profile(self):
        try:
            self.stop_profile()
        except (RuntimeError, OSError) as e:
            self.hub.publish("profile", {"state": "failed", "error": str(e)})

    def stop_profile(self):
        with self.lock:
            prof, self.profile = self.profile, None
            frames = list(getattr(self, "frames", []) or [])
            modules = list(self.modules)
            run_start = (self.run or {}).get("start")
        if prof is None:
            raise RuntimeError("no profile is recording")
        samples = prof["sampler"].stop()
        if prof["sampler"].error:
            raise RuntimeError(prof["sampler"].error)
        if not modules:
            modules = loaded_modules(prof["pid"])
        exe = Path(self.cfg["install_dir"]) / "kyty_emulator.exe"
        base = next((b for b, _, n in modules if n.lower() == exe.name.lower()), None)
        names = {}
        if base is not None:
            wanted = stack_sampler.addresses_to_symbolize(samples, modules, exe.name)
            leaves = {st[0] for _, st in samples if st}
            names = stack_sampler.symbolize_batch(self.cfg["symbolizer"], exe, base, wanted, leaves)
        # Frame batches are stamped in run time; samples in wall time.
        windows = [(run_start + a, run_start + b) for a, b in hitch_windows(frames)] if run_start else []
        report = {
            "thread": prof["thread"], "run": prof["run"], "started": prof["started"],
            "seconds": round(samples[-1][0] - samples[0][0], 1) if samples else 0,
            "all": stack_sampler.profile_report(samples, modules, names, exe.name),
            "hitches": stack_sampler.profile_report(samples, modules, names, exe.name, windows=windows),
            "hitch_count": sum(1 for a, b in windows if samples and samples[0][0] <= b and a <= samples[-1][0]),
        }
        wall = samples[-1][0] - samples[0][0] if len(samples) > 1 else 0
        # Share of the time the profiler held the thread stopped: how much it slowed what it measured.
        report["overhead_percent"] = round(100 * prof["sampler"].suspended / wall, 1) if wall else None
        (RUNS / prof["run"] / "profile.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        # Raw data, so the report can be recomputed later (other windows, other views).
        (RUNS / prof["run"] / "profile_samples.json").write_text(json.dumps(
            {"samples": samples, "modules": modules, "names": {hex(a): n for a, n in names.items()},
             "windows": windows}), encoding="utf-8")
        self.hub.publish("profile", {"state": "done", **report})
        return report

    def press(self, key):
        with self.lock:
            proc, run = self.proc, self.run
        if proc is None:
            raise RuntimeError("no game is running")
        hwnd, _ = find_window(proc.pid)
        if not hwnd:
            raise RuntimeError("the emulator window is not open yet")
        ok = send_key(hwnd, key)
        t = round(time.time() - run["start"], 1)
        self.hub.publish("input", {"t": t, "key": key, "ok": ok, "note": "" if ok else "could not focus the window"})
        return {"ok": ok, "t": t}


FLAG_TOKEN = re.compile(r"^(--[a-z0-9][a-z0-9-]*|[A-Za-z0-9_.:=+-]+)$")


def parse_flags(text):
    """Splits extra emulator flags typed in the page. Only plain tokens: no quotes or paths with spaces."""
    tokens = text.split()
    for t in tokens:
        if not FLAG_TOKEN.match(t):
            raise ValueError(f"not a plain flag or value: {t}")
    if tokens and not tokens[0].startswith("--"):
        raise ValueError("flags must start with --")
    return tokens


MAX_MINOR_ISSUES = 400


def issue_snapshot(issues):
    """Every critical and error issue, at most MAX_MINOR_ISSUES warnings and info, and exact
    totals. A game can report thousands of missing imports; sending them all on every state
    request made the response megabytes long."""
    totals = {s: 0 for s in ("critical", "error", "warning", "info")}
    for i in issues:
        totals[i["severity"]] += 1
    major = [i for i in issues if i["severity"] in ("critical", "error")]
    minor = [i for i in issues if i["severity"] in ("warning", "info")][:MAX_MINOR_ISSUES]
    return {"issues": major + minor, "issue_totals": totals}


def list_test_runs():
    out = []
    if TESTS.exists():
        for d in sorted(TESTS.iterdir(), reverse=True)[:50]:
            f = d / "summary.json"
            if f.exists():
                out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


def search_log(path, query, limit=300):
    """Lines of a run's full log containing `query` (case-insensitive), with line numbers."""
    query = (query or "").strip()
    if len(query) < 2:
        raise ValueError("search for at least 2 characters")
    limit = max(1, min(int(limit), 2000))
    needle = query.lower()
    hits, total = [], 0
    with open(path, "rb") as f:
        for n, raw in enumerate(f, 1):
            line = strip_ansi(raw.decode("utf-8", errors="replace"))
            if needle in line.lower():
                total += 1
                if len(hits) < limit:
                    hits.append({"line": n, "text": line.rstrip("\r\n")[:500]})
    return {"query": query, "total": total, "hits": hits}


def dumped_shaders():
    """Names of every shader variant in the dump folder, from this and earlier runs."""
    d = SHADERS / "dump"
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.spvasm") if SHADER_NAME.match(p.stem))


def overrides():
    d = SHADERS / "override"
    if not d.exists():
        return []
    return sorted({p.stem for p in d.iterdir() if p.suffix in (".spvasm", ".spv")})


def shader_override(name, action):
    """Starts, opens or removes an override for one shader variant."""
    if not SHADER_NAME.match(name or ""):
        raise ValueError(f"not a shader name: {name}")
    target = SHADERS / "override" / f"{name}.spvasm"
    if action == "revert":
        removed = []
        for ext in (".spvasm", ".spv"):
            p = SHADERS / "override" / f"{name}{ext}"
            if p.exists():
                p.unlink()
                removed.append(p.name)
        return {"removed": removed}
    if not target.exists() and not target.with_suffix(".spv").exists():
        source = SHADERS / "dump" / f"{name}.spvasm"
        if not source.exists():
            raise RuntimeError(f"{source.name} has not been dumped; run the game until it uses this shader")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    if action == "open" and sys.platform == "win32":
        os.startfile(str(target))  # opens in the editor associated with .spvasm (or asks for one)
    return {"path": str(target)}


LOOPCAP_MARK = "; kyty-live loopcap"


def _dumped(name):
    if not SHADER_NAME.match(name or ""):
        raise ValueError(f"not a shader name: {name}")
    source = SHADERS / "dump" / f"{name}.spvasm"
    if not source.exists():
        raise RuntimeError(f"{source.name} has not been dumped; run the game until it uses this shader")
    return source


def _loopcap_state(name):
    """(loops, limit) of the loop-cap override in place for this shader, or None."""
    target = SHADERS / "override" / f"{name}.spvasm"
    if not target.exists():
        return None
    with open(target, encoding="utf-8", errors="replace") as f:
        for _ in range(8):
            line = f.readline()
            if line.startswith(LOOPCAP_MARK):
                fields = dict(kv.split("=", 1) for kv in line[len(LOOPCAP_MARK):].split())
                return [int(x) for x in fields["loops"].split(",") if x], int(fields["limit"])
    return None


def shader_loops(name):
    loops = find_loops(_dumped(name).read_text(encoding="utf-8", errors="replace"))
    state = _loopcap_state(name)
    return {"name": name, "loops": loops, "capped": state[0] if state else [],
            "limit": state[1] if state else None}


def shader_loopcap(name, loops, limit):
    """Writes an override of the dumped shader with the chosen loops capped (diagnostic only)."""
    source = _dumped(name)
    target = SHADERS / "override" / f"{name}.spvasm"
    hand_made = (target.exists() and _loopcap_state(name) is None) or target.with_suffix(".spv").exists()
    if hand_made:
        raise RuntimeError("this shader already has a hand-edited override; revert it first")
    if not loops:
        if target.exists():
            target.unlink()
        return {"name": name, "capped": [], "limit": None}
    text = cap_loops(source.read_text(encoding="utf-8", errors="replace"), loops, limit)
    lines = text.splitlines(keepends=True)
    # spirv-dis puts its header comments first; the mark goes right after them.
    at = next((i for i, l in enumerate(lines) if not l.startswith(";")), 0)
    lines.insert(at, f"{LOOPCAP_MARK} loops={','.join(str(n) for n in sorted(set(loops)))} limit={int(limit)}\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(lines), encoding="utf-8")
    return {"name": name, "capped": sorted(set(loops)), "limit": int(limit), "path": str(target)}


# Emulator options Kyty Live always passes when the installed build has them: its own
# instrumentation, cheap enough to leave on.
TOOL_FLAGS = ("--frame-times",)


_RUN_CACHE = {}  # run folder -> (meta mtime, summary); issues.json can hold thousands of entries
RUN_ID = re.compile(r"^\d{8}-\d{6}_[A-Z]{4}\d{5}$")


def run_summary(d):
    meta_path = d / "meta.json"
    stamp = meta_path.stat().st_mtime_ns
    cached = _RUN_CACHE.get(d.name)
    if cached and cached[0] == stamp:
        return cached[1]
    m = json.loads(meta_path.read_text(encoding="utf-8"))
    iss = d / "issues.json"
    if iss.exists():
        issues = json.loads(iss.read_text(encoding="utf-8"))
        m["issue_counts"] = {s: sum(1 for i in issues if i["severity"] == s)
                             for s in ("critical", "error", "warning", "info")}
    m["snapshots"] = sorted(p.name for p in d.glob("snap-*.png"))
    m.pop("macro", None)
    _RUN_CACHE[d.name] = (stamp, m)
    return m


def list_runs():
    out = []
    if RUNS.exists():
        for d in sorted(RUNS.iterdir(), reverse=True):
            if d.is_dir() and RUN_ID.match(d.name) and (d / "meta.json").exists():
                out.append(run_summary(d))
    return out


def make_handler(session, hub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path, content_type, download=None):
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _game(self, game_id):
            for g in session.games():
                if g["id"] == game_id or g["path"] == game_id:
                    return g
            raise RuntimeError(f"unknown game {game_id}")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    body = (STATIC / "index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                elif u.path == "/api/games":
                    self._json(session.games(refresh="refresh" in q))
                elif u.path == "/api/state":
                    self._json(session.snapshot())
                elif u.path == "/api/runs":
                    self._json(list_runs())
                elif u.path.startswith("/api/runs/"):
                    run_id = u.path[len("/api/runs/"):]
                    if not RUN_ID.match(run_id):
                        raise ValueError(f"not a run id: {run_id}")
                    d = RUNS / run_id
                    iss = d / "issues.json"
                    log = d / "output.log"
                    frames = d / "frames.json"
                    self._json({"meta": run_summary(d),
                                "frames": json.loads(frames.read_text(encoding="utf-8")) if frames.exists() else [],
                                "issues": json.loads(iss.read_text(encoding="utf-8")) if iss.exists() else [],
                                "log_bytes": log.stat().st_size if log.exists() else 0})
                elif u.path == "/api/tests":
                    self._json({"suites": session.test_suites(), "history": list_test_runs(),
                                "current": session.testing})
                elif u.path.startswith("/api/testfiles/"):
                    parts = u.path[len("/api/testfiles/"):].split("/")
                    if (len(parts) != 2 or not re.fullmatch(r"\d{8}-\d{6}", parts[0])
                            or not re.fullmatch(r"[\w-]+\.log", parts[1])):
                        raise ValueError("bad file path")
                    self._send_file(TESTS / parts[0] / parts[1], "text/plain; charset=utf-8")
                elif u.path.startswith("/api/search/"):
                    run_id = u.path[len("/api/search/"):]
                    if not RUN_ID.match(run_id):
                        raise ValueError(f"not a run id: {run_id}")
                    self._json(search_log(RUNS / run_id / "output.log", q.get("q", [""])[0],
                                          int(q.get("max", ["300"])[0])))
                elif u.path == "/api/sweeps":
                    self._json(list_sweeps(SWEEPS))
                elif u.path.startswith("/api/sweeps/"):
                    self._json(load_sweep(SWEEPS, u.path[len("/api/sweeps/"):]))
                elif u.path == "/api/compare":
                    a, b = load_sweep(SWEEPS, q["a"][0]), load_sweep(SWEEPS, q["b"][0])
                    self._json({"a": a["id"], "b": b["id"], "rows": compare(a["rows"], b["rows"]),
                                "report": report_markdown(a, b)})
                elif u.path.startswith("/api/sweepfiles/"):
                    parts = u.path[len("/api/sweepfiles/"):].split("/")
                    if (len(parts) != 3 or not re.fullmatch(r"\d{8}-\d{6}", parts[0])
                            or not re.fullmatch(r"[A-Z]{4}\d{5}", parts[1]) or parts[2] != "end.png"):
                        raise ValueError("bad file path")
                    self._send_file(SWEEPS / parts[0] / parts[1] / "end.png", "image/png")
                elif u.path == "/api/macro":
                    self._json({"text": session.macro(q["game"][0])})
                elif u.path.startswith("/api/files/"):
                    parts = u.path[len("/api/files/"):].split("/")
                    if len(parts) != 2 or not RUN_ID.match(parts[0]):
                        raise ValueError("bad file path")
                    if re.fullmatch(r"snap-[\d.]+s\.png", parts[1]):
                        self._send_file(RUNS / parts[0] / parts[1], "image/png")
                    elif parts[1] == "output.log":
                        self._send_file(RUNS / parts[0] / "output.log", "text/plain; charset=utf-8",
                                        download=f"{parts[0]}.log")
                    else:
                        raise ValueError("bad file path")
                elif u.path == "/api/events":
                    self._events()
                else:
                    self._json({"error": "not found"}, 404)
            except (RuntimeError, ValueError, KeyError, OSError) as e:
                self._json({"error": str(e)}, 400)

        def do_POST(self):
            u = urlparse(self.path)
            try:
                b = self._body()
                if u.path == "/api/run":
                    cores = int(b["cores"]) if b.get("cores") not in (None, "") else None
                    fresh = None if b.get("fresh_save") is None else bool(b.get("fresh_save"))
                    self._json(session.start(self._game(b["game"]), extra_args=session.option_args(b.get("options")) + session.display_args(b.get("display"))
                                             + parse_flags(b.get("flags") or ""), cores=cores, fresh_save=fresh))
                elif u.path == "/api/stop":
                    self._json({"stopped": session.stop()})
                elif u.path == "/api/rebuild":
                    session.rebuild(bool(b.get("rerun", True)), self._game(b["game"]) if b.get("game") else None,
                                    session.option_args(b.get("options")) + session.display_args(b.get("display")) + parse_flags(b.get("flags") or ""))
                    self._json({"ok": True})
                elif u.path == "/api/macro":
                    session.save_macro(b["game"], b["text"])
                    self._json({"ok": True})
                elif u.path == "/api/hot/compile":
                    self._json(session.hot.compile("manual"))
                elif u.path == "/api/hot/auto":
                    session.hot.auto = bool(b.get("on"))
                    self._json({"auto": session.hot.auto})
                elif u.path == "/api/shader/override":
                    self._json(shader_override(b.get("name"), "open" if b.get("open") else "copy"))
                elif u.path == "/api/shader/revert":
                    self._json(shader_override(b.get("name"), "revert"))
                elif u.path == "/api/sweep":
                    seconds = float(b.get("seconds") or 60)
                    if not 10 <= seconds <= 600:
                        raise ValueError("seconds must be between 10 and 600")
                    repeats = int(b.get("repeats") or 1)
                    if not 1 <= repeats <= 20:
                        raise ValueError("repeats must be between 1 and 20")
                    self._json(session.start_sweep(
                        seconds, b.get("games") or None,
                        session.option_args(b.get("options")) + session.display_args(b.get("display")) + parse_flags(b.get("flags") or ""), repeats))
                elif u.path == "/api/tests/run":
                    self._json(session.start_tests(b.get("suites")))
                elif u.path == "/api/sweep/stop":
                    session.stop_sweep()
                    self._json({"ok": True})
                elif u.path == "/api/shader/loops":
                    self._json(shader_loops(b.get("name")))
                elif u.path == "/api/shader/loopcap":
                    limit = int(b.get("limit") or 262144)
                    self._json(shader_loopcap(b.get("name"), [int(x) for x in b.get("loops") or []], limit))
                elif u.path == "/api/profile/start":
                    self._json(session.start_profile(b.get("thread") or "Thread_Gpu"))
                elif u.path == "/api/profile/stop":
                    self._json(session.stop_profile())
                elif u.path == "/api/fresh-save":
                    session.settings.set("fresh_save", True if b.get("on") else None)
                    self._json({"fresh_save": bool(session.settings.get("fresh_save"))})
                elif u.path == "/api/display":
                    self._json(session.set_display(b.get("display")))
                elif u.path == "/api/autosnap":
                    every, keep = float(b.get("every", 5)), int(b.get("keep", 12))
                    self._json(session.set_autosnap(every, keep))
                elif u.path == "/api/snapshot":
                    self._json(session.snapshot_window())
                elif u.path == "/api/key":
                    if b.get("key") not in KEYS:
                        raise ValueError(f"unknown key {b.get('key')}")
                    self._json(session.press(b["key"]))
                else:
                    self._json({"error": "not found"}, 404)
            except (RuntimeError, ValueError, KeyError, OSError) as e:
                self._json({"error": str(e)}, 400)

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.subscribe()
            try:
                self.wfile.write(b"event: hello\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    batch = []
                    try:
                        batch.append(q.get(timeout=15))
                        while len(batch) < 200:
                            batch.append(q.get_nowait())
                    except queue.Empty:
                        pass
                    if not batch:
                        self.wfile.write(b": ping\n\n")
                    for kind, data in batch:
                        self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
            except (ConnectionError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def main():
    cfg = load_config()
    nids_path = ROOT / "data" / "nids.json"
    nids = json.loads(nids_path.read_text(encoding="utf-8")) if nids_path.exists() else {}
    RUNS.mkdir(exist_ok=True)
    hub = Hub()
    session = Session(cfg, hub, nids)
    server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), make_handler(session, hub))
    server.daemon_threads = True
    print(f"Kyty Live on http://127.0.0.1:{cfg['port']}  (emulator: {cfg['install_dir']})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        session.stop()


if __name__ == "__main__":
    sys.exit(main())
