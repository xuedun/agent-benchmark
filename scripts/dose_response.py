#!/usr/bin/env python3
"""Merge perf minute-buckets with churn-corrected vCPU timeline -> dose-response CSV."""
import csv
import json
from pathlib import Path
from collections import defaultdict

D = Path("/home/cxd/agent-benchmark/logs_mix_v3/profile_8x.collect")
BENCH_START = 150

# perf
by_t = defaultdict(dict)
with open(D / "counters.csv") as f:
    for row in csv.reader(f):
        if len(row) < 5 or row[0].startswith("#"):
            continue
        try:
            t = float(row[0])
            v = float(row[1])
        except ValueError:
            continue
        by_t[t][row[3]] = v
perf_min = defaultdict(lambda: defaultdict(float))
cnt = defaultdict(lambda: defaultdict(int))
for t, ev in by_t.items():
    b = int(t // 60)
    for k, v in ev.items():
        perf_min[b][k] += v
        cnt[b][k] += 1

# vcpu stable timeline
vcpu_min = defaultdict(list)
try:
    with open(D / "vcpu_timeline.csv") as f:
        for r in csv.DictReader(f):
            b = int(float(r["t"]) // 60)
            vcpu_min[b].append(r)
except FileNotFoundError:
    pass

# memfree
mem_min = defaultdict(list)
with open(D / "hcollect.jsonl") as f:
    for line in f:
        r = json.loads(line)
        if "cpu_ccl_pct" in r:
            b = int(r["t"] // 60)
            m = r.get("mem", {}).get("MemFree")
            if m is not None:
                mem_min[b].append(m)

out = D / "dose_response.csv"
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["minute", "bench_min", "IPC", "MPKI", "cycles_G_per_s",
                "vcpu_run_cores", "vcpu_wait_cores", "vcpu_threads", "MemFree_GB",
                "regime"])
    for b in sorted(set(perf_min) | set(vcpu_min) | set(mem_min)):
        pm = perf_min.get(b, {})
        c = pm.get("cycles", 0)
        i = pm.get("instructions", 0)
        m = pm.get("LLC-load-misses", 0)
        n_iv = cnt[b].get("cycles", 0) or 1
        ipc = i / c if c else None
        mpki = m / i * 1000 if i else None
        cgs = c / 60 / 1e9 if n_iv > 30 else None
        v = vcpu_min.get(b, [])
        rc = sum(float(x["run_cores"]) for x in v) / len(v) if v else None
        wc = sum(float(x["wait_cores"]) for x in v) / len(v) if v else None
        nt = sum(float(x["vcpu_threads"]) for x in v) / len(v) if v else None
        mf = sum(mem_min.get(b, [])) / len(mem_min[b]) if mem_min.get(b) else None
        bench_min = b - BENCH_START // 60
        regime = ("pre" if b < BENCH_START // 60 else
                  ("A_steady" if (mf is None or mf > 300) else
                   ("B_pressure" if (mf is None or mf > 15) else "C_exhausted")))
        w.writerow([b, bench_min,
                    f"{ipc:.3f}" if ipc else "", f"{mpki:.2f}" if mpki else "",
                    f"{cgs:.1f}" if cgs else "",
                    f"{rc:.1f}" if rc is not None else "",
                    f"{wc:.2f}" if wc is not None else "",
                    f"{nt:.0f}" if nt is not None else "",
                    f"{mf:.1f}" if mf is not None else "", regime])
print(open(out).read())
