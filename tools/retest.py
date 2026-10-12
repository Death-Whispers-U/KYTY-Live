"""End-to-end retest of hot HLE and shader overrides against a running Kyty Live server.

Usage: python tools/retest.py [game id]   (default PPSA16019, Double Dragon Gaiden)

Needs Kyty Live on 127.0.0.1:8790 and an emulator built from feat/hot-hle. Restores
hot/patch.cpp and removes the test override when it finishes, pass or fail.
"""

import http.client
import json
import struct
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHADER = "ps_4aa5db68601dcd19"  # Double Dragon's final full-screen pass
PATCH = ROOT / "hot" / "patch.cpp"
results = []


def call(path, body=None):
    # http.client, not urllib: urllib sends "Connection: close", and on this machine such loopback
    # requests sometimes never reach the server (the connection hangs ~19 s, then resets).
    conn = http.client.HTTPConnection("127.0.0.1", 8790, timeout=120)
    try:
        if body is None:
            conn.request("GET", path)
        else:
            conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        r = conn.getresponse()
        data = r.read()
        if r.status != 200:
            raise RuntimeError(f"{path}: {r.status} {data[:200]!r}")
        return json.loads(data)
    finally:
        conn.close()


def state():
    return call("/api/state")


def wait(cond, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        s = state()
        if cond(s):
            return s
        time.sleep(0.5)
    raise TimeoutError(f"timed out waiting for {what}")


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}", flush=True)


def snapshot_pixel():
    snap = call("/api/snapshot", {})
    data = (ROOT / "runs" / snap["run"] / snap["file"]).read_bytes()
    w, h = struct.unpack(">II", data[16:24])
    i = data.index(b"IDAT")
    raw = zlib.decompress(data[i + 4:i + 4 + struct.unpack(">I", data[i - 4:i])[0]])
    row = raw[(h // 2) * (w * 3 + 1) + 1:][:w * 3]
    return tuple(row[(w // 2) * 3:(w // 2) * 3 + 3]), snap["file"]


def main():
    game = sys.argv[1] if len(sys.argv) > 1 else "PPSA16019"
    original_patch = PATCH.read_text(encoding="utf-8")
    override = None
    try:
        call("/api/run", {"game": game})
        s = wait(lambda s: s["phase"] == "running" and (s["stats"].get("uptime") or 0) >= 20, 90, "20 s of play")
        check("game runs", (s["stats"].get("fps") or 0) >= 30, f"fps {s['stats'].get('fps')}, frame {s['stats'].get('frame')}")
        check("hot patch applied at boot", s["hot"]["generation"] == 1 and len(s["hot"]["patched"]) == 1,
              f"patch #{s['hot']['generation']}, {list(s['hot']['patched'].values())}")
        check("wrapper runs", any("v1:" in l["text"] for l in s["hot"]["lines"]))

        names = [x["name"] for x in s["shaders"]["translated"] if x["name"].startswith(SHADER)]
        check("shaders listed", len(s["shaders"]["translated"]) > 0 and len(names) == 1,
              f"{len(s['shaders']['translated'])} translated")
        name = names[0]
        normal, _ = snapshot_pixel()

        # Shader: apply live
        override = Path(call("/api/shader/override", {"name": name})["path"])
        text = override.read_text(encoding="utf-8")
        store = [l for l in text.splitlines() if l.strip().startswith("OpStore %out_mrt_0 ")][0].strip()
        text = text.replace("    %float_1 = OpConstant %float 1\n",
                            "    %float_1 = OpConstant %float 1\n"
                            "%kl_magenta = OpConstantComposite %v4float %float_1 %float_0 %float_1 %float_1\n", 1)
        override.write_text(text.replace(store, "OpStore %out_mrt_0 %kl_magenta", 1), encoding="utf-8")
        wait(lambda s: s["shaders"]["status"].get(name, {}).get("event") == "applied", 20, "override applied")
        time.sleep(0.5)
        pixel, file = snapshot_pixel()
        check("shader override live apply", pixel == (255, 0, 255), f"center pixel {pixel} in {file}")

        # Shader: interface guard
        override.write_text(override.read_text(encoding="utf-8")
                            .replace("OpDecorate %out_mrt_0 Location 0", "OpDecorate %out_mrt_0 Location 1", 1),
                            encoding="utf-8")
        s = wait(lambda s: s["shaders"]["status"].get(name, {}).get("event") == "rejected", 20, "rejection")
        err = s["shaders"]["status"][name]["error"]
        check("interface guard rejects", "Output Location=0" in err and s["phase"] == "running", err)

        # Shader: revert live
        call("/api/shader/revert", {"name": name})
        override = None
        wait(lambda s: s["shaders"]["status"].get(name, {}).get("event") == "reverted", 20, "revert")
        time.sleep(0.5)
        pixel, file = snapshot_pixel()
        check("shader revert live", pixel == normal, f"center pixel {pixel} (before override {normal})")

        # Hot HLE: swap and restore live
        PATCH.write_text(original_patch.replace('hot::log("v1: ', 'hot::log("v2: '), encoding="utf-8")
        wait(lambda s: s["hot"]["generation"] == 2, 30, "patch #2")
        s = wait(lambda s: any("v2:" in l["text"] for l in s["hot"]["lines"]), 20, "v2 output")
        check("hot patch live swap", True, "v2 running")
        PATCH.write_text('#include "kyty_hot.h"\n', encoding="utf-8")
        s = wait(lambda s: s["hot"]["generation"] == 3, 30, "patch #3")
        check("hot patch restore", len(s["hot"]["patched"]) == 0 and any(l["event"] == "restored" for l in s["hot"]["lines"]))

        s = state()
        check("still running at the end", s["phase"] == "running" and (s["stats"].get("fps") or 0) >= 30,
              f"fps {s['stats'].get('fps')}, frame {s['stats'].get('frame')}")
    except Exception as e:  # noqa: BLE001 - reported as a failure
        check("retest completed", False, repr(e))
    finally:
        PATCH.write_text(original_patch, encoding="utf-8")
        if override is not None and override.exists():
            override.unlink()
        try:
            call("/api/stop", {})
        except OSError:
            pass
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
