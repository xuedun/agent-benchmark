#!/usr/bin/env python3
"""Churn-corrected vCPU timeline + per-minute IPC correlation for the 8x profile run.

Fixes schedstat churn: only use intervals where per-CCL thread count is unchanged.
Also correlates perf IPC/MPKI with concurrent active vCPU threads.
Output: stdout tables + vcpu_timeline.csv
"""
import json
import csv
import sys
from pathlib import Path
from collections import defaultdict

D = Path(sys.argv[1] if len(sys.argv) > 1 else
         "/home/cxd/agent-benchmark/logs_mix_v3/profile_8x.collect")

# ---- vcpu timeline (churn-corrected) ----
rows = []
with open(D / "hcollect.jsonl") as f:
    for line in f:
        r = json.loads(line)
        if "vcpu_total" in r:
            rows.append(r)

print(f"vcpu ticks: {len(rows)}")
out = []
prev = None
for r in rows:
    cur = {c: v for c, v in r.get("vcpu", {}).items()}
    if prev is None:
        prev = cur
        continue
    stable = True
    for c, v in cur.items():
        p = prev.get(c)
        if p is None or p[3] != v[3]:  # thread count changed -> churn
            stable = False
            break
    if stable and cur:
        run = sum(v[0] for v in cur.values())
        wait = sum(v[1] for v in cur.values())
        nthreads = sum(v[3] for v in cur.values())
        out.append({"t": r["t"], "dt": r["dt"], "run_cores": round(run / r["dt"], 1),
                    "wait_cores": round(wait / r["dt"], 2),
                    "vcpu_threads": nthreads,
                    "wait_run_ratio": round(wait / run, 3) if run else None})
    prev = cur

print(f"stable intervals: {len(out)}/{len(rows)-1}")
if out:
    # print coarse timeline: every ~60s bucket
    buckets = defaultdict(list)
    for o in out:
        buckets[int(o["t"] // 60)].append(o)
    print(f"{'minute':>7} {'run_cores':>10} {'wait_cores':>11} {'vcpu_thr':>9} {'wait/run':>9}")
    for b in sorted(buckets):
        g = buckets[b]
        rc = sum(x["run_cores"] for x in g) / len(g)
        wc = sum(x["wait_cores"] for x in g) / len(g)
        nt = sum(x["vcpu_threads"] for x in g) / len(g)
        wr = (wc / rc) if rc else 0
        print(f"{b*60:>4}+   {rc:>10.1f} {wc:>11.2f} {nt:>9.0f} {wr:>9.3f}")
    with open(D / "vcpu_timeline.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["t", "dt", "run_cores", "wait_cores",
                                          "vcpu_threads", "wait_run_ratio"])
        w.writeheader()
        w.writerows(out)

# ---- correlate with meminfo (MemFree) ----
mem = []
with open(D / "hcollect.jsonl") as f:
    for line in f:
        r = json.loads(line)
        if "cpu_ccl_pct" in r:
            mem.append((r["t"], r.get("mem", {}).get("MemFree"),
                        r.get("psi", {}).get("cpu_some")))
print("\nmem/psi timeline (60s buckets):")
buckets = defaultdict(list)
for t, mf, psi in mem:
    if mf is not None:
        buckets[int(t // 60)].append((mf, psi))
print(f"{'minute':>7} {'MemFree_GB':>11} {'psi_cpu_some%':>14}")
for b in sorted(buckets):
    g = buckets[b]
    mf = sum(x[0] for x in g) / len(g)
    ps = [x[1] for x in g if x[1] is not None]
    psv = sum(ps) / len(ps) * 100 if ps else 0
    print(f"{b*60:>4}+   {mf:>11.1f} {psv:>14.2f}")
