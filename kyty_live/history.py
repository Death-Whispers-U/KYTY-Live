"""Past sweeps: listing, loading (any format Kyty Live has written), comparing and reporting."""

import json
import re
import time
from pathlib import Path

SEVERITIES = ("critical", "error", "warning", "info")
_SWEEP_ID = re.compile(r"^\d{8}-\d{6}$")


def normalize_row(row, sweep_id, folder=None):
    """One game's result in the current shape. Rows from the first command-line sweeps used
    alive_at_end/exit/screenshot and per-kind counts. `folder` (the sweep's folder) lets a
    snapshot that never got written be dropped instead of linked."""
    r = dict(row)
    if "alive_at_end" in row:
        r["crashed"] = not row["alive_at_end"]
        r["outcome"] = "ran the full time" if row["alive_at_end"] else (row.get("exit") or "stopped")
        exists = folder is None or (Path(folder) / row["id"] / "end.png").exists()
        r["snapshot"] = (f"/api/sweepfiles/{sweep_id}/{row['id']}/end.png"
                         if row.get("screenshot") and exists else None)
    # A run the sweep stopped early (the sweep was cancelled) is not a result either way.
    r["incomplete"] = bool(row.get("cancelled")) or row.get("outcome") == "stopped from Kyty Live"
    counts = row.get("counts") or {}
    r["counts"] = {s: (sum(counts[s].values()) if isinstance(counts.get(s), dict) else int(counts.get(s) or 0))
                   for s in SEVERITIES}
    r.setdefault("title", row.get("id"))
    r.setdefault("critical", [])
    r.setdefault("errors", [])
    return r


def _meta(folder):
    path = folder / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    meta.setdefault("flags", [])
    meta.setdefault("commit", None)
    meta.setdefault("seconds", None)
    return meta


def load_sweep(root, sweep_id):
    if not _SWEEP_ID.match(sweep_id or ""):
        raise ValueError(f"not a sweep id: {sweep_id}")
    folder = Path(root) / sweep_id
    rows = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    return {"id": sweep_id, "meta": _meta(folder), "rows": [normalize_row(r, sweep_id, folder) for r in rows]}


def list_sweeps(root):
    out = []
    root = Path(root)
    if not root.exists():
        return out
    for folder in sorted(root.iterdir(), reverse=True):
        if not folder.is_dir() or not _SWEEP_ID.match(folder.name) or not (folder / "summary.json").exists():
            continue
        try:
            s = load_sweep(root, folder.name)
        except (ValueError, OSError, json.JSONDecodeError):
            continue
        rows = s["rows"]
        out.append({"id": s["id"], "meta": s["meta"], "games": len(rows),
                    "crashed": sum(1 for r in rows if r.get("crashed")),
                    "frames": sum(r.get("max_frame") or 0 for r in rows),
                    "critical": sum(r["counts"]["critical"] for r in rows)})
    return out


def _group(rows):
    """A sweep can run each game several times; gather each game's rows in first-seen order."""
    groups = {}
    for r in rows:
        groups.setdefault(r["id"], []).append(r)
    return groups


def _summary(rows):
    """One game's runs in a sweep. Cancelled runs are not results, so they are left out of the rate."""
    done = [r for r in rows if not r.get("incomplete")]
    survived = sum(1 for r in done if not r.get("crashed"))
    frames = sorted(r.get("max_frame") or 0 for r in done) or [0]
    if len(rows) == 1:
        outcome = rows[0].get("outcome")
    else:
        outcome = f"{survived}/{len(done)} ran the full time" if done else "incomplete"
    critical = [t for r in rows for t in r.get("critical", [])]
    return {"outcome": outcome, "runs": len(done), "survived": survived,
            "rate": survived / len(done) if done else None,
            "max_frame": frames[len(frames) // 2],  # the median, so one lucky run does not dominate
            "last_fps": rows[-1].get("last_fps"),
            "critical": max(r["counts"]["critical"] for r in rows),
            "critical_texts": list(dict.fromkeys(critical))}


def compare(a_rows, b_rows):
    """Per game, from sweep A (before) to sweep B (after). With repeated runs the survival rate
    decides: fixed (none -> all), improved (rate up), regressed (all -> none), worse (rate down)."""
    a, b = _group(a_rows), _group(b_rows)
    out = []
    for gid, rows in b.items():
        after = _summary(rows)
        before = _summary(a[gid]) if gid in a else None
        if before is None:
            change = "new"
        elif before["rate"] is None or after["rate"] is None:
            change = "incomplete"
        elif before["rate"] == 0 and after["rate"] == 1:
            change = "fixed"
        elif before["rate"] == 1 and after["rate"] == 0:
            change = "regressed"
        elif after["rate"] > before["rate"]:
            change = "improved"
        elif after["rate"] < before["rate"]:
            change = "worse"
        else:
            change = "same"
        new_texts = after["critical_texts"]
        old_texts = before["critical_texts"] if before else []
        out.append({
            "id": gid, "title": rows[0].get("title"), "change": change,
            "before": before and {k: before[k] for k in ("outcome", "runs", "survived", "max_frame", "last_fps", "critical")},
            "after": {k: after[k] for k in ("outcome", "runs", "survived", "max_frame", "last_fps", "critical")},
            "frames_delta": None if before is None else after["max_frame"] - before["max_frame"],
            "critical_delta": None if before is None else after["critical"] - before["critical"],
            "new_critical": [t for t in new_texts if t not in old_texts] if before else list(new_texts),
            "gone_critical": [t for t in old_texts if t not in new_texts],
        })
    for gid, rows in a.items():
        if gid not in b:
            out.append({"id": gid, "title": rows[0].get("title"), "change": "missing", "before": None, "after": None,
                        "frames_delta": None, "critical_delta": None, "new_critical": [], "gone_critical": []})
    return out


def _when(sweep_id):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.strptime(sweep_id, "%Y%m%d-%H%M%S"))
    except ValueError:
        return sweep_id


def _label(s):
    flags = " ".join(s["meta"].get("flags") or []) or "no extra flags"
    commit = s["meta"].get("commit")
    return f"{_when(s['id'])}, {'build ' + commit if commit else 'build not recorded'}, {flags}"


def report_markdown(a, b):
    """A progress report comparing sweep A (before) with sweep B (after), as Markdown."""
    rows = compare(a["rows"], b["rows"])
    counts = {k: sum(1 for r in rows if r["change"] == k)
              for k in ("fixed", "improved", "regressed", "worse", "same", "new", "missing", "incomplete")}
    titles = [r["title"] for r in rows]
    name = lambda r: f"{r['title']} ({r['id']})" if titles.count(r["title"]) > 1 else r["title"]  # noqa: E731
    survived = lambda s: sum(1 for r in s["rows"] if not r.get("crashed"))  # noqa: E731
    lines = [
        "# Kyty Live progress report",
        "",
        f"- **Before:** {_label(a)}: {survived(a)}/{len(a['rows'])} games ran the full time",
        f"- **After:** {_label(b)}: {survived(b)}/{len(b['rows'])} games ran the full time",
        f"- **Changes:** {counts['fixed']} fixed, {counts['improved']} improved, {counts['regressed']} regressed, "
        f"{counts['worse']} worse, {counts['same']} unchanged"
        + (f", {counts['new']} new" if counts["new"] else "") + (f", {counts['missing']} missing" if counts["missing"] else "")
        + (f", {counts['incomplete']} incomplete (sweep stopped early)" if counts["incomplete"] else ""),
        "",
        "| Game | Change | Before | After | Frames | Critical |",
        "|---|---|---|---|---|---|",
    ]
    order = {"regressed": 0, "worse": 1, "fixed": 2, "improved": 3, "new": 4, "same": 5, "incomplete": 6, "missing": 7}
    for r in sorted(rows, key=lambda r: (order[r["change"]], r["title"] or "")):
        before = r["before"]["outcome"] if r["before"] else "–"
        after = r["after"]["outcome"] if r["after"] else "–"
        frames = (f"{r['before']['max_frame'] if r['before'] else '–'} → {r['after']['max_frame'] if r['after'] else '–'}")
        crit = (f"{r['before']['critical'] if r['before'] else '–'} → {r['after']['critical'] if r['after'] else '–'}")
        lines.append(f"| {name(r)} | {r['change']} | {before} | {after} | {frames} | {crit} |")
    fixed_notes = [r for r in rows if r["gone_critical"] and r["change"] != "incomplete"]
    if fixed_notes:
        lines += ["", "## Failures that went away", ""]
        for r in fixed_notes:
            lines += [f"- **{name(r)}:** {t}" for t in r["gone_critical"]]
    new_notes = [r for r in rows if r["new_critical"] and r["change"] not in ("new", "incomplete")]
    if new_notes:
        lines += ["", "## New failures", ""]
        for r in new_notes:
            lines += [f"- **{name(r)}:** {t}" for t in r["new_critical"]]
    return "\n".join(lines) + "\n"
