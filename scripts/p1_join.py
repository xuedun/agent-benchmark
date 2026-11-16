#!/usr/bin/env python3
"""P1: join step aggregates across cells, categorize actions, quantify 1x->8x slowdown.

Reads analysis/step_agg_*.csv, writes:
  category_summary.csv  per category: copies, mean cmd per cell, ratio, DeltaT total, share
  top_steps.csv         top steps by DeltaT contribution
  timeline.csv          60s bins of cumulative DeltaT share + cumulative exec share (8_2)
  overhead.csv          per-cell mean command_start_sec / command_wait_sec (control plane)
Prints compact tables.
"""
import csv
import re
import sys
from pathlib import Path
from collections import defaultdict

OUT = Path("/home/cxd/agent-benchmark/logs_mix_v3/analysis")
CELLS = ["1_1", "1_2", "8_1", "8_2"]

CAT_RULES = [
    ("maven", re.compile(r"\bmvn\b")),
    ("gradle", re.compile(r"\bgradle\b")),
    ("npm/node", re.compile(r"\b(npm|npx|yarn|pnpm|node)\b")),
    ("go", re.compile(r"\bgo (build|test|mod|run|vet|clean|install|generate|work)\b")),
    ("pip", re.compile(r"\b(pip3?|python3? -m pip)\b")),
    ("python/pytest", re.compile(r"\b(python3?|pytest|unittest)\b")),
    ("compile(c/c++/rust)", re.compile(r"\b(gcc|g\+\+|clang|rustc|cargo|make)\b")),
    ("git", re.compile(r"\bgit\b")),
    ("network(curl/wget)", re.compile(r"\b(curl|wget)\b")),
    ("sleep", re.compile(r"^sleep\b")),
    ("fs-scan", re.compile(r"^(find|ls|grep|rg|wc|head|tail|cat|du|stat|file)\b")),
    ("fs-write", re.compile(r"^(touch|mkdir|cp|mv|rm|ln|tar|zip|unzip|chmod|chown|sed|awk|echo|tee|dd)\b")),
]


def categorize(action):
    a = action or ""
    for name, rx in CAT_RULES:
        if rx.search(a):
            return name
    return "shell-other"


def load(cell):
    rows = {}
    with open(OUT / f"step_agg_{cell}.csv") as f:
        for r in csv.DictReader(f):
            key = (r["trajectory"], int(r["step_index"]))
            rows[key] = {
                "n": int(r["n"]), "n_ok": int(r["n_ok"]),
                "n_fail": int(r["n_fail"]), "n_timeout": int(r["n_timeout"]),
                "cmd_sum": float(r["cmd_sum"]), "cmd_sum_ok": float(r["cmd_sum_ok"]),
                "start_sum": float(r["start_sum"]), "wait_sum": float(r["wait_sum"]),
                "rel_start": float(r["rel_start_mean"]) if r["rel_start_mean"] else None,
                "action": r["action"],
            }
    return rows


def fmt(x, nd=2):
    return f"{x:.{nd}f}"


def main():
    data = {c: load(c) for c in CELLS}
    keys = sorted(set(data["1_1"]) & set(data["8_1"]) & set(data["1_2"]) & set(data["8_2"]))
    # action consistency check
    mismatch = sum(1 for k in keys
                   if data["1_1"][k]["action"][:80] != data["8_2"][k]["action"][:80])
    print(f"matched steps={len(keys)} action_mismatch={mismatch}")

    # per-step derived stats for pair 1_1->8_1 and 1_2->8_2
    steps = []
    for k in keys:
        a11, a81 = data["1_1"][k], data["8_1"][k]
        a12, a82 = data["1_2"][k], data["8_2"][k]
        cat = categorize(a11["action"])
        m11 = a11["cmd_sum"] / a11["n"]
        m81 = a81["cmd_sum"] / a81["n"]
        m12 = a12["cmd_sum"] / a12["n"]
        m82 = a82["cmd_sum"] / a82["n"]
        steps.append({
            "traj": k[0], "idx": k[1], "cat": cat,
            "n11": a11["n"], "n81": a81["n"], "n12": a12["n"], "n82": a82["n"],
            "m11": m11, "m81": m81, "m12": m12, "m82": m82,
            "d81": (m81 - m11), "d82": (m82 - m12),
            "dt81": (m81 - m11) * a81["n"],          # total exec-time impact at 8_1
            "dt82": (m82 - m12) * a82["n"],          # total exec-time impact at 8_2
            "to81": a81["n_timeout"], "to11": a11["n_timeout"],
            "fail81": a81["n_fail"], "fail11": a11["n_fail"],
            "to82": a82["n_timeout"], "to12": a12["n_timeout"],
            "rel82": a82["rel_start"],
            "cmd82_total": a82["cmd_sum"],
            "action": a11["action"][:150],
            "start11": a11["start_sum"] / a11["n"], "start81": a81["start_sum"] / a81["n"],
            "start12": a12["start_sum"] / a12["n"], "start82": a82["start_sum"] / a82["n"],
            "wait11": a11["wait_sum"] / a11["n"], "wait81": a81["wait_sum"] / a81["n"],
        })

    # ---- category summary (pair 1_1 -> 8_1 primary; also 1_2 -> 8_2) ----
    cat_stats = defaultdict(lambda: {
        "copies81": 0, "steps": 0,
        "cmd11": 0.0, "cmd81": 0.0, "dt81": 0.0,
        "cmd12": 0.0, "cmd82": 0.0, "dt82": 0.0,
        "to11": 0, "to81": 0, "to12": 0, "to82": 0,
        "fail11": 0, "fail81": 0,
    })
    for s in steps:
        c = cat_stats[s["cat"]]
        c["steps"] += 1
        c["copies81"] += s["n81"]
        c["cmd11"] += s["m11"] * s["n81"]
        c["cmd81"] += s["m81"] * s["n81"]
        c["dt81"] += s["dt81"]
        c["cmd12"] += s["m12"] * s["n82"]
        c["cmd82"] += s["m82"] * s["n82"]
        c["dt82"] += s["dt82"]
        c["to11"] += s["to11"]; c["to81"] += s["to81"]
        c["to12"] += s["to12"]; c["to82"] += s["to82"]
        c["fail11"] += s["fail11"]; c["fail81"] += s["fail81"]

    total_dt81 = sum(c["dt81"] for c in cat_stats.values())
    total_dt82 = sum(c["dt82"] for c in cat_stats.values())
    rows = []
    print("\n== category slowdown (pair 1_1 -> 8_1, copies weighted) ==")
    print(f"{'category':<22}{'steps':>6}{'cmd@1x':>10}{'cmd@8x':>10}{'ratio':>7}"
          f"{'ΔT_sum':>10}{'share':>8}{'to@1x':>7}{'to@8x':>7}{'fail@1x':>8}{'fail@8x':>8}")
    for name, c in sorted(cat_stats.items(), key=lambda kv: -kv[1]["dt81"]):
        if c["copies81"] == 0:
            continue
        mean11 = c["cmd11"] / c["copies81"]
        mean81 = c["cmd81"] / c["copies81"]
        ratio = mean81 / mean11 if mean11 > 0 else float("nan")
        share = c["dt81"] / total_dt81 * 100 if total_dt81 else 0
        rows.append([name, c["steps"], fmt(mean11, 3), fmt(mean81, 3), fmt(ratio, 2),
                     fmt(c["dt81"], 0), fmt(share, 1),
                     c["to11"], c["to81"], c["fail11"], c["fail81"]])
        print(f"{name:<22}{c['steps']:>6}{fmt(mean11,3):>10}{fmt(mean81,3):>10}"
              f"{fmt(ratio,2):>7}{fmt(c['dt81'],0):>10}{fmt(share,1):>8}"
              f"{c['to11']:>7}{c['to81']:>7}{c['fail11']:>8}{c['fail81']:>8}")
    print(f"TOTAL ΔT(8_1-1_1) = {total_dt81:.0f}s   ΔT(8_2-1_2) = {total_dt82:.0f}s")

    with open(OUT / "category_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["category", "steps", "mean_cmd_1x", "mean_cmd_8x", "ratio",
                    "dt_total_81", "share_pct", "timeout_1x", "timeout_8x",
                    "fail_1x", "fail_8x", "dt_total_82", "mean_cmd_12", "mean_cmd_82"])
        for r, (name, c) in zip(rows, sorted(cat_stats.items(), key=lambda kv: -kv[1]["dt81"])):
            w.writerow(r + [fmt(c["dt82"], 0), fmt(c["cmd12"] / c["copies81"] if c["copies81"] else 0, 3),
                            fmt(c["cmd82"] / c["copies81"] if c["copies81"] else 0, 3)])

    # ---- top steps by dt81 ----
    with open(OUT / "top_steps.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["traj", "step", "cat", "mean_1x", "mean_8x", "ratio", "dt81", "dt82",
                    "timeout_8x", "action"])
        for s in sorted(steps, key=lambda s: -s["dt81"])[:200]:
            w.writerow([s["traj"], s["idx"], s["cat"], fmt(s["m11"], 3), fmt(s["m81"], 3),
                        fmt(s["m81"] / s["m11"] if s["m11"] > 0 else 0, 2),
                        fmt(s["dt81"], 1), fmt(s["dt82"], 1), s["to81"], s["action"]])

    # ---- timeline for early-stop (use 8_2 rel_start + dt82 contribution) ----
    tl = [s for s in steps if s["rel82"] is not None]
    total_dt_tl = sum(s["dt82"] for s in tl)
    total_cmd82 = sum(s["cmd82_total"] for s in tl)
    bins = defaultdict(lambda: {"dt": 0.0, "cmd": 0.0})
    for s in tl:
        b = int(s["rel82"] // 60)
        bins[b]["dt"] += max(0.0, s["dt82"])
        bins[b]["cmd"] += s["cmd82_total"]
    with open(OUT / "timeline.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bin_min", "dt_in_bin", "cum_dt_share_pct", "cum_cmd_share_pct"])
        cum_dt = cum_cmd = 0.0
        for b in sorted(bins):
            cum_dt += bins[b]["dt"]
            cum_cmd += bins[b]["cmd"]
            w.writerow([b, fmt(bins[b]["dt"], 1),
                        fmt(cum_dt / total_dt_tl * 100 if total_dt_tl else 0, 1),
                        fmt(cum_cmd / total_cmd82 * 100 if total_cmd82 else 0, 1)])
    for mark in (600, 900, 1200, 1500, 1800, 2400):
        b = mark // 60
        done_dt = sum(v["dt"] for k, v in bins.items() if k <= b)
        done_cmd = sum(v["cmd"] for k, v in bins.items() if k <= b)
        print(f"  early-stop@{mark}s: ΔT covered {done_dt/total_dt_tl*100:.1f}% "
              f"exec_time covered {done_cmd/total_cmd82*100:.1f}%")

    # ---- control-plane overhead ----
    st = {c: [] for c in CELLS}
    for s in steps:
        st["1_1"].append(s["start11"]); st["8_1"].append(s["start81"])
        st["1_2"].append(s["start12"]); st["8_2"].append(s["start82"])
    w81 = sum(s["wait81"] for s in steps) / len(steps)
    w11 = sum(s["wait11"] for s in steps) / len(steps)
    with open(OUT / "overhead.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cell", "mean_command_start_sec"])
        for c in CELLS:
            v = [x for x in st[c] if x is not None]
            w.writerow([c, fmt(sum(v) / len(v), 4)])
    print(f"\ncontrol-plane: mean command_start_sec 1_1={sum(st['1_1'])/len(st['1_1']):.4f} "
          f"8_1={sum(st['8_1'])/len(st['8_1']):.4f} 1_2={sum(st['1_2'])/len(st['1_2']):.4f} "
          f"8_2={sum(st['8_2'])/len(st['8_2']):.4f}")
    print(f"mean command_wait_sec 1_1={w11:.4f} 8_1={w81:.4f}")


if __name__ == "__main__":
    main()
