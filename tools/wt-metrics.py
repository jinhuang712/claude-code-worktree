#!/usr/bin/env python3
"""How do worktree sessions behave? Measured from Claude Code's local transcripts (~/.claude/projects).

Stdlib only. It reads your transcripts on this machine and prints aggregates; nothing leaves it.
Run it before and after upgrading the plugin and compare:

    python3 tools/wt-metrics.py --until 2026-09-29T15:00:00Z     # baseline
    python3 tools/wt-metrics.py --since 2026-10-06               # a week after upgrading

What it counts (Bash tool calls after a session became worktree-isolated, wt.py calls and their results):
landings, `finish` failing with "not rebased", isolation-guard refusals, failures on worktrees the plugin
did not create (the pre-0.2 messages), manual pushes per landing, and how long /wt:land took.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone

WT_SUBS = "plan|start|status|adopt|land|rebase|continue|verify|finish|abandon|list|gc|track"
WT_CALL = re.compile(r'(?:wt\.py|\$WT|\$\{WT\})"?\s+(%s)\b' % WT_SUBS)
GUARD = "isolated in the worktree"


def parse_ts(ts: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else str(c)


def classify(block: dict, ts: datetime | None) -> dict | None:
    """One tool call reduced to what the metrics need, or None for calls they ignore."""
    name, inp = block.get("name"), block.get("input") or {}
    if name in ("EnterWorktree", "ExitWorktree"):
        return {"kind": name, "sub": None, "ts": ts}
    if name == "Skill" and str(inp.get("skill", "")).startswith("wt:"):
        return {"kind": "skill", "sub": inp["skill"], "ts": ts}
    if name != "Bash":
        return None
    cmd = inp.get("command", "")
    m = WT_CALL.search(cmd)
    if m:
        return {"kind": "wt", "sub": m.group(1), "ts": ts}
    if re.search(r"\bgit\s+push\b", cmd):
        return {"kind": "push", "sub": None, "ts": ts}
    return {"kind": "bash", "sub": None, "ts": ts}


def load(path: str) -> list[dict]:
    """The transcript's calls in order, each with its result text once seen."""
    events, by_id = [], {}
    with open(path, errors="replace") as fh:
        for line in fh:
            if "tool_use" not in line and "tool_result" not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            content = (o.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            ts = parse_ts(o.get("timestamp", ""))
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    ev = classify(b, ts)
                    if ev:
                        ev.update(res="", t1=None)
                        events.append(ev)
                        by_id[b["id"]] = ev
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                    ev = by_id[b["tool_use_id"]]
                    ev["res"], ev["t1"] = result_text(b), ts
    return events


def refused(ev: dict) -> bool:
    return GUARD in ev["res"] and "Refusing to run it" in ev["res"]


def landed(ev: dict) -> bool:
    m = re.search(r'"landed":\s*(\d+)', ev["res"])
    return ev["kind"] == "wt" and ev["sub"] == "finish" and '"ok": true' in ev["res"] and bool(m and int(m.group(1)))


def measure(paths: list[str], since: datetime | None, until: datetime | None) -> dict:
    def inside(ev: dict) -> bool:
        return bool(ev["ts"]) and (since is None or ev["ts"] >= since) and (until is None or ev["ts"] <= until)

    counters = ("landings", "finish_calls", "not_rebased", "in_place", "foreign_failures", "adopted",
                "land_calls", "guard_refused", "bash_isolated", "guard_refused_all", "bash_isolated_all",
                "sessions_isolated", "pushes", "sessions")
    c: dict = {k: 0 for k in counters}
    seconds, calls = [], []
    for path in paths:
        events = load(path)
        uses_wt = any(e["kind"] == "wt" and inside(e) for e in events)
        began = next((i for i, e in enumerate(events)
                      if e["kind"] == "EnterWorktree" or (e["kind"] in ("bash", "wt", "push") and refused(e))), None)
        c["sessions"] += uses_wt
        c["sessions_isolated"] += began is not None and any(inside(e) for e in events[began:])
        for i, e in enumerate(events):
            if not inside(e):
                continue
            if began is not None and i >= began and e["kind"] in ("bash", "wt", "push"):
                c["bash_isolated_all"] += 1
                c["guard_refused_all"] += refused(e)
                if uses_wt:
                    c["bash_isolated"] += 1
                    c["guard_refused"] += refused(e)
            if not uses_wt:
                continue
            if e["kind"] == "push":
                c["pushes"] += 1
            if e["kind"] != "wt":
                continue
            c["finish_calls"] += e["sub"] == "finish"
            c["land_calls"] += e["sub"] == "land"
            c["landings"] += landed(e)
            c["not_rebased"] += "is not rebased onto" in e["res"]
            c["in_place"] += '"rebased_during_finish": true' in e["res"]
            c["adopted"] += '"adopted":' in e["res"]
            c["foreign_failures"] += "no wtBase metadata" in e["res"] or "not on a wt-" in e["res"]
        for i, e in enumerate(events):  # /wt:land -> first landed finish, same session
            if not (e["kind"] == "skill" and e["sub"] == "wt:land" and inside(e)):
                continue
            n = 0
            for f in events[i + 1:]:
                n += f["kind"] == "wt"
                if landed(f) and f["t1"] and e["ts"]:
                    dt = (f["t1"] - e["ts"]).total_seconds()
                    if dt < 1800:
                        seconds.append(dt)
                        calls.append(n)
                    break
                if f["kind"] == "skill" and f["sub"] == "wt:land":
                    break
    c["land_seconds_median"] = round(statistics.median(seconds)) if seconds else None
    c["land_wt_calls_median"] = statistics.median(calls) if calls else None
    c["land_samples"] = len(seconds)
    return c


def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def report(c: dict) -> str:
    rows = [
        ("sessions that ran wt.py", c["sessions"], ""),
        ("landings (finish that landed commits)", c["landings"], ""),
        ("finish calls", c["finish_calls"], ""),
        ('  failed with "not rebased onto"', c["not_rebased"], pct(c["not_rebased"], c["finish_calls"])),
        ("  rebased in place instead (0.2+)", c["in_place"], ""),
        ("Bash calls once isolated in a worktree, wt sessions", c["bash_isolated"], ""),
        ("  refused by the isolation guard", c["guard_refused"], pct(c["guard_refused"], c["bash_isolated"])),
        (f"  same, all {c['sessions_isolated']} worktree-isolated sessions", c["bash_isolated_all"], ""),
        ("    refused by the isolation guard", c["guard_refused_all"],
         pct(c["guard_refused_all"], c["bash_isolated_all"])),
        ("failures on a worktree wt did not create (pre-0.2 messages)", c["foreign_failures"], ""),
        ("  adopted instead (0.2+)", c["adopted"], ""),
        ("wt.py land calls (0.2+)", c["land_calls"], ""),
        ("git push commands in those sessions", c["pushes"],
         f"{c['pushes'] / c['landings']:.2f} per landing" if c["landings"] else ""),
        ("/wt:land to landed, median seconds", c["land_seconds_median"], f"n={c['land_samples']}"),
        ("  wt.py calls in between, median", c["land_wt_calls_median"], ""),
    ]
    width = max(len(r[0]) for r in rows)
    return "\n".join(f"{name:<{width}}  {'-' if val is None else val!s:>7}  {note}".rstrip() for name, val, note in rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"), help="transcripts directory")
    ap.add_argument("--since", help="ISO date or time (UTC unless stated); calls before it are ignored")
    ap.add_argument("--until", help="ISO date or time; calls after it are ignored")
    ap.add_argument("--json", action="store_true", help="print the raw numbers as JSON")
    args = ap.parse_args()
    since = parse_ts(args.since) if args.since else None
    until = parse_ts(args.until) if args.until else None
    if (args.since and not since) or (args.until and not until):
        sys.exit("--since/--until must be ISO dates or times, e.g. 2026-10-06 or 2026-10-06T12:00:00Z")
    paths = glob.glob(os.path.join(args.projects, "**", "*.jsonl"), recursive=True)
    result = measure(paths, since, until)
    print(json.dumps(result, indent=2) if args.json else report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
