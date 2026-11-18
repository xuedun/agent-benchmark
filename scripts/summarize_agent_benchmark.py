#!/usr/bin/env python3
"""Summarize agent-benchmark (agent_replay suite) run outputs.

Usage:
    python scripts/summarize_agent_benchmark.py <results-dir> [<results-dir> ...] [--out DIR]

Computes pooled avg/P50/P95/P99 of per-VM execution time (sum of per-step
command_sec, the actual command execution cost) and wall time from
`tasks/<task>/replay-result.json` (run layout or `ratio-*/repeat-*` trial
layout). No per-VM rows: writes exec_summary.csv with one row per statistic.

Also plots host CPU and memory utilization over time from
`observability/metrics_1s.jsonl` as two subplots in one PNG per metrics file.

Cases listed in `skiplist.txt` (repo root, or --skiplist PATH) are excluded
from the aggregation: their VMs are dropped from exec_summary.csv /
vm_total.csv / vm memory sections and written to skipped.csv instead.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

STATS = ("avg", 50, 95, 99)
TASK_GLOBS = (
    "tasks/*/replay-result.json",
    "ratio-*/repeat-*/tasks/*/replay-result.json",
    "*/ratio-*/repeat-*/tasks/*/replay-result.json",
)
METRICS_GLOBS = (
    "observability/metrics_1s.jsonl",
    "ratio-*/repeat-*/observability/metrics_1s.jsonl",
    "*/ratio-*/repeat-*/observability/metrics_1s.jsonl",
)
VM_METRICS_GLOBS = (
    "tasks/*/sandbox-metrics.json",
    "ratio-*/repeat-*/tasks/*/sandbox-metrics.json",
    "*/ratio-*/repeat-*/tasks/*/sandbox-metrics.json",
)
COUNTERS_GLOBS = (
    "_collect/counters.csv",
    "*/_collect/counters.csv",
)
GB = 1024.0 ** 3
COUNTER_EVENTS = {
    "cycles", "instructions",
    "LLC-loads", "LLC-load-misses",
    "ls_dmnd_fills_from_sys.dram_io_near",
    "ls_dmnd_fills_from_sys.dram_io_far",
}


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def stat_value(values: list[float], stat) -> float | None:
    if not values:
        return None
    if stat == "avg":
        return sum(values) / len(values)
    return percentile(values, stat)


def discover(root: Path, globs: tuple[str, ...]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in globs:
        for path in sorted(root.glob(pattern)):
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def load_skiplist(path: Path) -> set[str] | None:
    """Load case names to skip. Returns None when the file does not exist
    (backward compatible: no filtering). Entries are trajectory stems; a
    '.replay.json' suffix is tolerated and stripped. '#' comments and blank
    lines are ignored."""
    if not path.is_file():
        return None
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if name.endswith(".replay.json"):
            name = name[: -len(".replay.json")]
        elif name.endswith(".replay"):
            name = name[: -len(".replay")]
        names.add(name)
    return names or None


def traj_name(trajectory: str) -> str:
    """Normalize a trajectory filename/stem to the bare case name."""
    name = Path(str(trajectory or "unknown")).stem
    if name.endswith(".replay"):
        name = name[: -len(".replay")]
    return name


def task_label(path: Path, root: Path) -> str:
    try:
        rel = path.parent.relative_to(root)
    except ValueError:
        return "."
    parts = rel.parts[:-2] if len(rel.parts) >= 2 else ()
    return "/".join(parts) or "."


def load_vm(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  WARN: unreadable {path}: {exc}", file=sys.stderr)
        return None
    steps = data.get("steps") or []
    return {
        "task": path.parent.name,
        "sandbox_id": data.get("sandbox_id") or "",
        "exec_sec": round(sum(s.get("command_sec") or 0.0 for s in steps), 4),
        "delay_sec": round(sum(s.get("delay_wait_sec") or 0.0 for s in steps), 4),
        "wall_sec": data.get("elapsed_sec"),
        "steps": len(steps),
        "succeeded": bool(data.get("succeeded")),
        "trajectory": Path(str(data.get("trajectory_path") or "unknown")).name,
    }


def vm_total_csv(vms: list[dict], out_dir: Path) -> None:
    path = out_dir / "vm_total.csv"
    fields = ["trial", "task", "sandbox_id", "trajectory", "steps",
              "delay_sec", "exec_sec", "wall_sec", "succeeded"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for vm in vms:
            writer.writerow({k: vm.get(k, "") for k in fields})
    print(f"  wrote: {path} ({len(vms)} VMs)")


def exec_summary(vms: list[dict], out_dir: Path) -> None:
    exec_vals = [v["exec_sec"] for v in vms if isinstance(v.get("exec_sec"), (int, float))]
    wall_vals = [v["wall_sec"] for v in vms if isinstance(v.get("wall_sec"), (int, float))]
    step_vals = [float(v["steps"]) for v in vms if v.get("steps")]
    rows = []
    for stat in STATS:
        name = stat if isinstance(stat, str) else f"P{stat}"
        row = {"stat": name, "vms": len(vms)}
        for label, vals in (("exec_sec", exec_vals), ("wall_sec", wall_vals), ("steps", step_vals)):
            v = stat_value(vals, stat)
            row[label] = round(v, 4) if v is not None else ""
        row["succeeded"] = f"{sum(1 for v in vms if v['succeeded'])}/{len(vms)}"
        rows.append(row)
    path = out_dir / "exec_summary.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["stat", "vms", "exec_sec", "wall_sec", "steps", "succeeded"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"  exec stats ({len(vms)} VMs): " + " | ".join(
        f"{r['stat']} exec={r['exec_sec']}s wall={r['wall_sec']}s" for r in rows))
    print(f"  wrote: {path}")


def parse_elapsed(records: list[dict]) -> list[float]:
    times: list[datetime] = []
    for rec in records:
        ts = rec.get("ts")
        if not ts:
            continue
        try:
            times.append(datetime.fromisoformat(ts))
        except ValueError:
            continue
    if not times:
        return []
    t0 = times[0]
    return [(t - t0).total_seconds() for t in times]


def metrics_plot(records: list[dict], label: str, out_path: Path, n_vms: int) -> bool:
    if not records:
        return False
    xs = parse_elapsed(records)
    if not xs:
        print(f"  WARN: no parseable ts in {label}", file=sys.stderr)
        return False

    def series(key: str) -> tuple[list[float], list[float]]:
        pts = [(x, rec[key]) for x, rec in zip(xs, records)
               if isinstance(rec.get(key), (int, float))]
        return ([p[0] for p in pts], [p[1] for p in pts])

    fig, (ax_cpu, ax_mem) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

    for key, label_txt in (("cpu_pct", "cpu_pct"), ("cpu_iowait_pct", "iowait_pct")):
        xs_v, ys_v = series(key)
        if ys_v:
            ax_cpu.plot(xs_v, ys_v, linewidth=1.2, label=label_txt)
    ax_cpu.set_ylabel("CPU (%)")
    ax_cpu.set_ylim(0, 105)
    ax_cpu.set_title(f"Host CPU & memory utilization - {label}" + (f" ({n_vms} VMs)" if n_vms else ""))
    ax_cpu.legend(loc="upper right")
    ax_cpu.grid(alpha=0.3)

    for key, label_txt in (("mem_used_gb", "mem_used_gb"), ("fc_rss_gb", "fc_rss_gb"),
                           ("mem_cached_gb", "mem_cached_gb")):
        xs_v, ys_v = series(key)
        if ys_v:
            ax_mem.plot(xs_v, ys_v, linewidth=1.2, label=label_txt)
    ax_mem.set_ylabel("Memory (GB)")
    ax_mem.set_xlabel("elapsed (s)")
    ax_mem.legend(loc="upper right")
    ax_mem.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def load_vm_mem(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  WARN: unreadable {path}: {exc}", file=sys.stderr)
        return None
    samples = data.get("samples") or []
    pts = [(s["timestampUnix"], s["memUsed"], s["memCache"])
           for s in samples
           if isinstance(s.get("timestampUnix"), (int, float))
           and isinstance(s.get("memUsed"), (int, float))
           and isinstance(s.get("memCache"), (int, float))]
    if not pts:
        return None
    return {
        "task": path.parent.name,
        "sandbox_id": data.get("sandbox_id") or "",
        "points": pts,
    }


def vm_mem_section(vm_mems: list[dict], out_dir: Path, per_vm: bool = False) -> None:
    rows = []
    for vm in vm_mems:
        used = [p[1] / GB for p in vm["points"]]
        cache = [p[2] / GB for p in vm["points"]]
        rows.append({
            "trial": vm.get("trial", ""),
            "task": vm["task"],
            "sandbox_id": vm["sandbox_id"],
            "samples": len(vm["points"]),
            "mem_used_avg_gb": round(sum(used) / len(used), 3),
            "mem_used_peak_gb": round(max(used), 3),
            "mem_cache_avg_gb": round(sum(cache) / len(cache), 3),
            "mem_cache_peak_gb": round(max(cache), 3),
        })
    csv_path = out_dir / "vm_mem_summary.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "trial", "task", "sandbox_id", "samples",
            "mem_used_avg_gb", "mem_used_peak_gb", "mem_cache_avg_gb", "mem_cache_peak_gb"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"  vm mem: {len(vm_mems)} VMs, wrote: {csv_path}")

    t0 = min(p[0] for vm in vm_mems for p in vm["points"])
    fig, (ax_used, ax_cache) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    for vm in vm_mems:
        xs = [p[0] - t0 for p in vm["points"]]
        ax_used.plot(xs, [p[1] / GB for p in vm["points"]], linewidth=0.9, alpha=0.65)
        ax_cache.plot(xs, [p[2] / GB for p in vm["points"]], linewidth=0.9, alpha=0.65)
    ax_used.set_ylabel("VM memUsed (GB)")
    ax_used.set_title(f"Per-VM guest memory over time ({len(vm_mems)} VMs, server-sampled)")
    ax_used.grid(alpha=0.3)
    ax_cache.set_ylabel("VM memCache (GB)")
    ax_cache.set_xlabel("elapsed since first sample (s)")
    ax_cache.grid(alpha=0.3)
    fig.tight_layout()
    png_path = out_dir / "vm_mem_curves.png"
    fig.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote: {png_path}")

    if per_vm:
        plots_dir = out_dir / "vm_mem_plots"
        plots_dir.mkdir(parents=True, exist_ok=True)
        for vm in vm_mems:
            xs = [p[0] - vm["points"][0][0] for p in vm["points"]]
            fig, (ax_used, ax_cache) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
            ax_used.plot(xs, [p[1] / GB for p in vm["points"]], linewidth=1.2)
            ax_used.set_ylabel("memUsed (GB)")
            ax_used.set_title(
                f"VM {vm['task']} sandbox={vm['sandbox_id'] or 'n/a'}"
                + (f" trial={vm.get('trial', '')}" if vm.get("trial", ".") != "." else ""))
            ax_used.grid(alpha=0.3)
            ax_cache.plot(xs, [p[2] / GB for p in vm["points"]], linewidth=1.2, color="tab:orange")
            ax_cache.set_ylabel("memCache (GB)")
            ax_cache.set_xlabel("elapsed since VM first sample (s)")
            ax_cache.grid(alpha=0.3)
            fig.tight_layout()
            trial = vm.get("trial", ".")
            prefix = trial.replace("/", "_") + "_" if trial not in (".", "") else ""
            fig.savefig(plots_dir / f"vm_mem_{prefix}{vm['task']}.png", dpi=130, bbox_inches="tight")
            plt.close(fig)
        print(f"  wrote {len(vm_mems)} per-VM PNGs: {plots_dir}/")


def parse_counters(path: Path) -> list[dict]:
    rows: dict[float, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        evt = next((p for p in parts if p in COUNTER_EVENTS), None)
        if evt is None:
            continue
        evt_idx = parts.index(evt)
        if evt_idx < 3:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        val = None
        for p in reversed(parts[:evt_idx]):
            try:
                val = float(p)
                break
            except ValueError:
                continue
        if val is None:
            continue
        rows.setdefault(ts, {})[evt] = val
    series = []
    for ts, ev in sorted(rows.items()):
        ipc = ev["instructions"] / ev["cycles"] if ev.get("cycles") else None
        miss = (ev.get("ls_dmnd_fills_from_sys.dram_io_near") or 0) \
            + (ev.get("ls_dmnd_fills_from_sys.dram_io_far") or 0)
        if miss <= 0:
            miss = ev.get("LLC-load-misses") or 0
        mpki = miss / ev["instructions"] * 1000 if ev.get("instructions") else None
        freq = ev["cycles"] / 1e9 if ev.get("cycles") else None
        series.append({"ts": ts, "ipc": ipc, "mpki": mpki, "freq_ghz": freq})
    return series


def counters_plot(series: list[dict], label: str, out_path: Path) -> bool:
    pts = [(s["ts"], s["ipc"], s["mpki"], s["freq_ghz"]) for s in series
           if s["ipc"] is not None]
    if len(pts) < 5:
        return False
    xs = [p[0] for p in pts]
    fig, (ax_ipc, ax_mpki) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    ax_ipc.plot(xs, [p[1] for p in pts], linewidth=0.9)
    ax_ipc.set_ylabel("IPC")
    ax_ipc.set_title(f"IPC / MPKI over time - {label} ({len(pts)} samples, 1s interval)")
    ax_ipc.grid(alpha=0.3)
    mp = [(p[0], p[2]) for p in pts if p[2] is not None]
    ax_mpki.plot([m[0] for m in mp], [m[1] for m in mp], linewidth=0.9, color="tab:orange")
    ax_mpki.set_ylabel("MPKI")
    ax_mpki.set_xlabel("elapsed since collector start (s)")
    ax_mpki.grid(alpha=0.3)
    if pts[0][3] and pts[-1][3]:
        ax_ipc.annotate(
            f"cycles: {pts[0][3]:.1f}G/s -> {pts[-1][3]:.1f}G/s per interval",
            xy=(0.02, 0.92), xycoords="axes fraction", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def counters_section(root: Path, out_dir: Path) -> None:
    files = discover(root, COUNTERS_GLOBS)
    if not files:
        return
    for cf in files:
        series = parse_counters(cf)
        if not series:
            continue
        rel = cf.parent.parent.relative_to(root)
        label = str(rel) if str(rel) != "." else "run"
        png = out_dir / f"counters_{label.replace('/', '_')}.png"
        if counters_plot(series, label, png):
            ipcs = [s["ipc"] for s in series if s["ipc"] is not None]
            mpkis = [s["mpki"] for s in series if s["mpki"] is not None]
            print(f"  counters: {len(series)}s, mean_ipc={sum(ipcs)/len(ipcs):.3f}"
                  + (f" mean_mpki={sum(mpkis)/len(mpkis):.2f}" if mpkis else "")
                  + f", wrote: {png}")


def process_root(root: Path, out_dir: Path, per_vm: bool = False,
                 skiplist: set[str] | None = None) -> None:
    task_files = discover(root, TASK_GLOBS)
    vms = []
    skipped = []
    for f in task_files:
        vm = load_vm(f)
        if vm is not None:
            vm["trial"] = task_label(f, root)
            if skiplist and traj_name(vm.get("trajectory")) in skiplist:
                skipped.append(vm)
            else:
                vms.append(vm)
    out_dir.mkdir(parents=True, exist_ok=True)
    if skipped:
        by_case: dict[str, int] = {}
        for vm in skipped:
            by_case[traj_name(vm.get("trajectory"))] = by_case.get(traj_name(vm.get("trajectory")), 0) + 1
        print(f"  skiplist: dropped {len(skipped)} VMs from {len(by_case)} case(s): "
              + ", ".join(f"{k}({v})" for k, v in sorted(by_case.items())))
        path = out_dir / "skipped.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["trial", "task", "sandbox_id", "trajectory"],
                                    extrasaction="ignore")
            writer.writeheader()
            for vm in skipped:
                writer.writerow(vm)
        print(f"  wrote: {path} ({len(skipped)} VMs)")
    if vms:
        print(f"[{root}] {len(vms)} VM task results")
        exec_summary(vms, out_dir)
        vm_total_csv(vms, out_dir)
    else:
        print(f"[{root}] no task results; plotting metrics curves only")

    metrics_files = discover(root, METRICS_GLOBS)
    if not metrics_files:
        print("  no metrics_1s.jsonl found, skipping plots")
    else:
        plotted = 0
        for mf in metrics_files:
            try:
                records = [json.loads(line) for line in mf.read_text(encoding="utf-8").splitlines() if line.strip()]
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  WARN: unreadable {mf}: {exc}", file=sys.stderr)
                continue
            label = task_label(mf, root)
            safe = label.replace("/", "_") if label not in (".", "") else "run"
            png = out_dir / f"metrics_{safe}.png"
            if metrics_plot(records, label, png, len(vms)):
                plotted += 1
                print(f"  wrote: {png} ({len(records)} samples)")
        if plotted == 0:
            print("  WARN: no metrics plots produced")

    vm_mem_files = discover(root, VM_METRICS_GLOBS)
    vm_mems = []
    for f in vm_mem_files:
        vm = load_vm_mem(f)
        if vm is not None:
            vm["trial"] = task_label(f, root)
            # vm-metrics files carry no trajectory field: derive the case name
            # from the task dir name ("NNNNNN-<case>").
            case = f.parent.name.split("-", 1)[1] if "-" in f.parent.name else f.parent.name
            if skiplist and case in skiplist:
                continue
            vm_mems.append(vm)
    if vm_mems:
        vm_mem_section(vm_mems, out_dir, per_vm=per_vm)
    else:
        print("  no per-VM sandbox-metrics.json found, skipping vm mem curves")

    counters_section(root, out_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="+", type=Path, help="run output dir or bench experiment root")
    parser.add_argument("--out", type=Path, default=None, help="output dir (default: <root>/processed)")
    parser.add_argument("--per-vm", action="store_true",
                        help="also write one PNG per VM under <out>/vm_mem_plots/")
    parser.add_argument("--skiplist", type=Path, default=None,
                        help="case skiplist file (default: skiplist.txt at repo root; "
                             "pass an empty path to disable)")
    args = parser.parse_args()
    default_skiplist = Path(__file__).resolve().parent.parent / "skiplist.txt"
    skiplist_path = args.skiplist if args.skiplist is not None else default_skiplist
    skiplist = load_skiplist(skiplist_path)
    if skiplist:
        print(f"skiplist: {len(skiplist)} case(s) from {skiplist_path}")
    else:
        print(f"skiplist: none ({'file not found: ' + str(skiplist_path) if not skiplist_path.is_file() else 'empty'})")
    for root in args.roots:
        root = root.expanduser().resolve()
        if not root.is_dir():
            print(f"ERROR: {root} is not a directory", file=sys.stderr)
            return 1
        out_dir = (args.out or root / "processed").expanduser().resolve()
        process_root(root, out_dir, per_vm=args.per_vm, skiplist=skiplist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
