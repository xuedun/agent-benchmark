#!/usr/bin/env python3
"""P2/P4: analyze a profile run collect dir (hcollect.jsonl + counters.csv +
latency_probe.csv + psi.csv) into USE-table evidence.

Usage: python3 analyze_collect.py <collect_dir> [--bench-start <t0_offset>]
Prints time-weighted stats + writes collect_analysis.csv.
"""
import csv
import json
import sys
from pathlib import Path
from collections import defaultdict


def tw_mean(samples):
    """samples: list of (value, weight)"""
    tw = sum(w for _, w in samples)
    return (sum(v * w for v, w in samples) / tw) if tw else None


def pct(samples, p):
    vs = sorted(v for v, _ in samples)
    if not vs:
        return None
    k = (len(vs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (k - lo)


def main():
    d = Path(sys.argv[1])
    bench_start = None
    if "--bench-start" in sys.argv:
        bench_start = float(sys.argv[sys.argv.index("--bench-start") + 1])

    # ---- hcollect ----
    h5 = []
    hv = []
    with open(d / "hcollect.jsonl") as f:
        for line in f:
            r = json.loads(line)
            if "cpu_ccl_pct" in r:
                h5.append(r)
            elif "vcpu_total" in r:
                hv.append(r)

    print(f"hcollect: {len(h5)} 5s-ticks, {len(hv)} 10s-ticks, "
          f"t=[{h5[0]['t']:.0f}..{h5[-1]['t']:.0f}]s" if h5 else "hcollect: EMPTY")

    def phase(r):
        if bench_start is None:
            return "all"
        if r["t"] < bench_start:
            return "pre"
        return "bench"

    # per-phase stats
    for name in (["all"] if bench_start is None else ["pre", "bench"]):
        sel5 = [r for r in h5 if phase(r) == name]
        selv = [r for r in hv if phase(r) == name]
        if not sel5:
            continue
        print(f"\n== phase {name}: {len(sel5)} ticks ==")
        # cpu per CCL
        ccl_cpu = defaultdict(list)
        ccl_guest = defaultdict(list)
        for r in sel5:
            for c, v in r.get("cpu_ccl_pct", {}).items():
                ccl_cpu[c].append((v, r["dt"]))
            for c, v in r.get("guest_ccl_pct", {}).items():
                ccl_guest[c].append((v, r["dt"]))
        if ccl_cpu:
            means = [tw_mean(v) for v in ccl_cpu.values()]
            print(f"  cpu_ccl_pct: mean={sum(means)/len(means):.1f} "
                  f"min={min(means):.1f} max={max(means):.1f}")
            if ccl_guest:
                gm = [tw_mean(v) for v in ccl_guest.values()]
                print(f"  guest_ccl_pct: mean={sum(gm)/len(gm):.1f} "
                      f"min={min(gm):.1f} max={max(gm):.1f}")
        # psi
        psi = defaultdict(list)
        for r in sel5:
            for k, v in r.get("psi", {}).items():
                psi[k].append((v, r["dt"]))
        for k in sorted(psi):
            m = tw_mean(psi[k])
            p95 = pct(psi[k], 95)
            print(f"  psi_{k}: mean={m*100:.2f}% p95={p95*100:.2f}%")
        # mem
        memf = defaultdict(list)
        for r in sel5:
            for k in ("MemFree", "Cached", "AnonPages", "Dirty", "PageTables", "Slab"):
                if k in r.get("mem", {}):
                    memf[k].append((r["mem"][k], r["dt"]))
        for k in ("MemFree", "Cached", "AnonPages", "Dirty"):
            if k in memf:
                m = tw_mean(memf[k])
                print(f"  mem_{k}: mean={m:.1f}GB min={min(v for v,_ in memf[k]):.1f} "
                      f"max={max(v for v,_ in memf[k]):.1f}")
        # vm deltas (per-tick, ~5s) -> per-second rates
        vm = defaultdict(list)
        for r in sel5:
            for k, v in r.get("vm", {}).items():
                vm[k].append((v / r["dt"], r["dt"]))
        for k in ("pgscan", "pgsteal", "pgrefault", "pswpin", "pgmajfault", "pgdeactivate"):
            for kk in list(vm):
                if kk.startswith(k) and vm.get(kk):
                    m = tw_mean(vm[kk])
                    print(f"  vm_{kk}: mean={m:.1f}/s")
        # disk
        dsk = defaultdict(list)
        for r in sel5:
            for dev, st in r.get("disk", {}).items():
                dsk[(dev, "iops")].append((st["iops"] / r["dt"], r["dt"]))
                dsk[(dev, "util")].append((st["util_pct"], r["dt"]))
                if st["iops"]:
                    dsk[(dev, "await")].append((st["await_ms"], r["dt"]))
        for (dev, k), v in sorted(dsk.items()):
            if k == "util":
                m = tw_mean(v)
                if m and m > 0.01:
                    print(f"  disk {dev} util={m:.1f}% iops={tw_mean(dsk.get((dev,'iops'),[])) or 0:.0f} "
                          f"await={tw_mean(dsk.get((dev,'await'),[])) or 0:.1f}ms")
        # run/blocked
        rr = [(r.get("run", 0), r["dt"]) for r in sel5]
        bb = [(r.get("blocked", 0), r["dt"]) for r in sel5]
        print(f"  procs_running mean={tw_mean(rr):.0f} p95={pct(rr,95):.0f}; "
              f"blocked mean={tw_mean(bb):.0f}")
        # daemons
        srv = [(r["srv_cpu_s"] / r["dt"] * 100, r["dt"]) for r in sel5
               if r.get("srv_cpu_s") is not None]
        ubl = [(r["ublk_cpu_s"] / r["dt"] * 100, r["dt"]) for r in sel5
               if r.get("ublk_cpu_s") is not None]
        if srv:
            print(f"  server_cpu: mean={tw_mean(srv):.1f}% p95={pct(srv,95):.1f}%")
        if ubl:
            print(f"  ublk_cpu: mean={tw_mean(ubl):.1f}% p95={pct(ubl,95):.1f}%")
        # vcpu
        if selv:
            vr = [(r["vcpu_total"]["run_s"] / r["dt"], r["dt"]) for r in selv]
            vw = [(r["vcpu_total"]["wait_s"] / r["dt"], r["dt"]) for r in selv]
            vm_n = [r["vcpu_total"]["vms"] for r in selv]
            print(f"  vcpu run: mean={tw_mean(vr):.1f}cores p95={pct(vr,95):.1f} "
                  f"max={max(v for v,_ in vr):.1f}")
            print(f"  vcpu wait: mean={tw_mean(vw):.2f}cores p95={pct(vw,95):.2f} "
                  f"max={max(v for v,_ in vw):.2f}  <-- queueing/steal")
            print(f"  vms (fc procs): median={sorted(vm_n)[len(vm_n)//2]} max={max(vm_n)}")
            # wait/run ratio
            run_s = sum(v for v, _ in vr) * 0  # not used
            # per-CCL wait
            cw = defaultdict(list)
            for r in selv:
                for c, v in r.get("vcpu", {}).items():
                    cw[c].append((v[1] / r["dt"], r["dt"]))
            if cw:
                wmean = [tw_mean(v) for v in cw.values()]
                print(f"  vcpu wait per CCL: mean={sum(wmean)/len(wmean):.2f} "
                      f"max={max(wmean):.2f} cores")

    # ---- perf counters ----
    cp = d / "counters.csv"
    if cp.exists():
        by_t = defaultdict(dict)
        with open(cp) as f:
            for row in csv.reader(f):
                if len(row) < 5 or row[0].startswith("#"):
                    continue
                try:
                    t = float(row[0])
                    v = float(row[1])
                except ValueError:
                    continue
                by_t[t][row[3]] = v
        print(f"\nperf: {len(by_t)} intervals")
        # per-minute buckets
        buckets = defaultdict(lambda: defaultdict(float))
        n = defaultdict(lambda: defaultdict(int))
        for t, ev in by_t.items():
            b = int(t // 60)
            for k, v in ev.items():
                buckets[b][k] += v
                n[b][k] += 1
        print(f"{'minute':>7} {'IPC':>7} {'cycles/s(G)':>12} {'MPKI':>7} {'LLCmiss%':>9}")
        for b in sorted(buckets):
            c = buckets[b].get("cycles", 0)
            i = buckets[b].get("instructions", 0)
            ll = buckets[b].get("LLC-loads", 0)
            m = buckets[b].get("LLC-load-misses", 0)
            secs = sum(n[b].values()) / len(buckets[b]) if buckets[b] else 1
            if c and i:
                print(f"{b*60:>4}+{''} {i/c:>7.3f} {c/60/1e9:>12.1f} {m/i*1000 if i else 0:>7.2f} "
                      f"{m/ll*100 if ll else 0:>8.1f}%")

    # ---- latency probes ----
    lp = d / "latency_probe.csv"
    if lp.exists():
        rows = list(csv.DictReader(open(lp)))
        if rows:
            print(f"\nloaded-latency probes: {len(rows)} rounds")
            for ws in ("8388608", "268435456"):
                vals = [float(r["ns_per_access"]) for r in rows if r.get("ws") == ws]
                if vals:
                    print(f"  ws={int(ws)//2**20}MB: first={vals[0]:.1f}ns "
                          f"mean={sum(vals)/len(vals):.1f}ns max={max(vals):.1f}ns")
    # ---- psi.csv from collect_metrics ----
    ps = d / "psi.csv"
    if ps.exists():
        rows = list(csv.DictReader(open(ps)))
        if rows:
            cs = [float(r["cpu_some"]) for r in rows if r.get("cpu_some")]
            ms = [float(r["mem_some"]) for r in rows if r.get("mem_some")]
            ios = [float(r["io_some"]) for r in rows if r.get("io_some")]
            print(f"\ncollect_metrics psi: cpu_some mean={sum(cs)/len(cs):.2f}% "
                  f"max={max(cs):.2f}% | mem mean={sum(ms)/len(ms):.2f}% "
                  f"io mean={sum(ios)/len(ios):.2f}%")


if __name__ == "__main__":
    main()
