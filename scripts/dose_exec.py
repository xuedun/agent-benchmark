#!/usr/bin/env python3
"""Dose-response: per-step exec time vs system concurrency phase, from profile_8x.

Buckets executed steps by wall-minute since bench start; reports mean command_sec
for long commands (>1s) and all, plus the concurrent vCPU-thread count per bucket.
"""
import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

BASE = Path("/home/cxd/agent-benchmark/logs_mix_v3")
BENCH_T0 = datetime.fromisoformat("2026-11-16T23:20:37+08:00")

# concurrency per hcollect minute bucket (bench-minute aligned)
con = {}
with open(BASE / "profile_8x.collect/vcpu_timeline.csv") as f:
    import csv
    for r in csv.DictReader(f):
        # hcollect t -> bench minute: bench started at hcollect t=150
        bm = int((float(r["t"]) - 150) // 60)
        con.setdefault(bm, []).append((float(r["vcpu_threads"]), float(r["run_cores"])))

agg = defaultdict(lambda: {"n": 0, "sum": 0.0, "n_long": 0, "sum_long": 0.0})
n_tasks = 0
for td in sorted((BASE / "profile_8x/ratio-08/repeat-01/tasks").iterdir()):
    rp = td / "replay-result.json"
    if not rp.is_file():
        continue
    try:
        r = json.loads(rp.read_text())
    except Exception:
        continue
    n_tasks += 1
    for s in r.get("steps", []):
        ts = s.get("completed_at") or s.get("started_at")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        bm = int((t - BENCH_T0).total_seconds() // 60)
        if bm < 0:
            continue
        cmd = s.get("command_sec") or 0.0
        a = agg[bm]
        a["n"] += 1
        a["sum"] += cmd
        if cmd > 1.0:
            a["n_long"] += 1
            a["sum_long"] += cmd

print(f"tasks={n_tasks}")
print(f"{'bench_min':>9} {'vcpu_thr':>8} {'run_cores':>9} {'n_cmd':>7} "
      f"{'mean_all':>9} {'n>1s':>6} {'mean_long':>10}")
for bm in sorted(agg):
    a = agg[bm]
    c = con.get(bm)
    thr = sum(x[0] for x in c) / len(c) if c else None
    rc = sum(x[1] for x in c) / len(c) if c else None
    ma = a["sum"] / a["n"] if a["n"] else 0
    ml = a["sum_long"] / a["n_long"] if a["n_long"] else 0
    print(f"{bm:>9} {thr:>8.0f} {rc:>9.1f} {a['n']:>7} {ma:>9.3f} "
          f"{a['n_long']:>6} {ml:>10.2f}")
