#!/usr/bin/env python3
"""P2: summarize observability/metrics_1s.jsonl per cell -> USE-table inputs.

Per cell: sampling cadence (control-plane health proxy), cpu_pct, iowait,
fc_cores / server_cores / ublk_cores (per-process-group CPU, cores),
memory (used/free/cached, per-NUMA), ublk_devices, sandboxes_running.
Also a phase split: ramp (first 240s) vs steady (rest).
"""
import json
import csv
from pathlib import Path
from datetime import datetime
from statistics import median

BASE = Path("/home/cxd/agent-benchmark/logs_mix_v3")
OUT = BASE / "analysis"
CELLS = ["1_1", "1_2", "8_1", "8_2"]
NCPU = 384.0


def percentile(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def load_cell(cell):
    rows = []
    for mf in sorted((BASE / cell).glob("ratio-*/repeat-01/observability/metrics_1s.jsonl")):
        with open(mf) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def stats(rows, key):
    v = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    if not v:
        return None
    return {"mean": sum(v) / len(v), "med": median(v), "p95": percentile(v, 95),
            "max": max(v), "min": min(v)}


def main():
    result = {}
    for cell in CELLS:
        rows = load_cell(cell)
        if not rows:
            print(f"{cell}: NO METRICS")
            continue
        ts = [datetime.fromisoformat(r["ts"]) for r in rows]
        iv = [(b - a).total_seconds() for a, b in zip(ts, ts[1:]) if (b - a).total_seconds() > 0]
        dur = (ts[-1] - ts[0]).total_seconds()
        ramp = [r for r, t in zip(rows, ts) if (t - ts[0]).total_seconds() <= 240]
        steady = [r for r, t in zip(rows, ts) if (t - ts[0]).total_seconds() > 240]
        d = {
            "n_samples": len(rows), "duration_s": dur,
            "interval_med": median(iv) if iv else None,
            "interval_p95": percentile(iv, 95) if iv else None,
            "interval_max": max(iv) if iv else None,
            "cpu_pct": stats(rows, "cpu_pct"),
            "iowait_pct": stats(rows, "cpu_iowait_pct"),
            "fc_cores": stats(rows, "fc_cores"),
            "server_cores": stats(rows, "server_cores"),
            "ublk_cores": stats(rows, "ublk_cores"),
            "mem_used_gb": stats(rows, "mem_used_gb"),
            "mem_cached_gb": stats(rows, "mem_cached_gb"),
            "mem_free_gb": stats(rows, "mem_free_gb"),
            "mem_dirty_mb": stats(rows, "mem_dirty_mb"),
            "ublk_devices": stats(rows, "ublk_devices"),
            "sandboxes_running": stats(rows, "sandboxes_running"),
            "steady_cpu_pct": stats(steady, "cpu_pct"),
            "steady_iowait_pct": stats(steady, "cpu_iowait_pct"),
            "steady_fc_cores": stats(steady, "fc_cores"),
            "steady_server_cores": stats(steady, "server_cores"),
            "steady_ublk_cores": stats(steady, "ublk_cores"),
            "steady_mem_cached_gb": stats(steady, "mem_cached_gb"),
            "ramp_cpu_pct": stats(ramp, "cpu_pct"),
        }
        result[cell] = d
        print(f"\n== {cell}: n={d['n_samples']} dur={dur:.0f}s "
              f"interval med={d['interval_med']:.1f}s p95={d['interval_p95']:.1f}s max={d['interval_max']:.1f}s")
        for k in ["cpu_pct", "iowait_pct", "fc_cores", "server_cores", "ublk_cores",
                  "mem_used_gb", "mem_cached_gb", "mem_dirty_mb", "ublk_devices", "sandboxes_running"]:
            s = d[k]
            if s:
                print(f"  {k:<20} mean={s['mean']:>9.2f} med={s['med']:>9.2f} "
                      f"p95={s['p95']:>9.2f} max={s['max']:>9.2f}")
        s = d["steady_cpu_pct"]
        if s:
            print(f"  steady_cpu_pct       mean={s['mean']:>9.2f} p95={s['p95']:>9.2f}")
        s = d["steady_iowait_pct"]
        if s:
            print(f"  steady_iowait_pct    mean={s['mean']:>9.2f} p95={s['p95']:>9.2f}")

    # derived: guest demand estimate
    print("\n== derived (steady) ==")
    for cell in CELLS:
        d = result.get(cell)
        if not d or not d.get("steady_cpu_pct"):
            continue
        sc = d["steady_cpu_pct"]["mean"]
        fc = d["steady_fc_cores"]["mean"] if d.get("steady_fc_cores") else float("nan")
        srv = d["steady_server_cores"]["mean"] if d.get("steady_server_cores") else float("nan")
        ub = d["steady_ublk_cores"]["mean"] if d.get("steady_ublk_cores") else float("nan")
        vms = d["sandboxes_running"]["med"] if d.get("sandboxes_running") else float("nan")
        print(f"  {cell}: cpu={sc:.1f}% ({sc/100*NCPU:.0f} cores) fc={fc:.1f}c server={srv:.2f}c "
              f"ublk={ub:.2f}c vms={vms:.0f} "
              f"cores/vm={sc/100*NCPU/vms if vms else float('nan'):.3f}")

    with open(OUT / "metrics_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cell", "metric", "mean", "median", "p95", "max"])
        for cell in CELLS:
            d = result.get(cell)
            if not d:
                continue
            for k, s in d.items():
                if isinstance(s, dict):
                    w.writerow([cell, k, f"{s['mean']:.3f}", f"{s['med']:.3f}",
                                f"{s['p95']:.3f}", f"{s['max']:.3f}"])
    print(f"\n-> {OUT}/metrics_summary.csv")


if __name__ == "__main__":
    main()
