#!/usr/bin/env python3
"""P1: extract per-step command-timing aggregates from replay-result.json for 1_1/1_2/8_1/8_2.

Output per cell: analysis/step_agg_<cell>.csv
  one row per (trajectory, step_index) aggregated across all copies in the cell:
    n / n_ok / n_fail / n_timeout copies
    cmd_sum / cmd_sum_ok  (sum of command_sec, all vs succeeded-only)
    start_sum / wait_sum  (sum of command_start_sec / command_wait_sec)
    rel_start_sum         (sum of step-start relative to the task's replay start, sec)
    action                (first-seen action text, 400 chars)
Read-only w.r.t. existing run outputs.
"""
import json
import re
import sys
import csv
from pathlib import Path
from collections import defaultdict
from datetime import datetime

BASE = Path("/home/cxd/agent-benchmark/logs_mix_v3")
OUT = BASE / "analysis"
CELLS = ["1_1", "1_2", "8_1", "8_2"]


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for cell in CELLS:
        agg = {}
        n_tasks = 0
        n_parse_err = 0
        for rdir in sorted((BASE / cell).glob("ratio-*")):
            rep = rdir / "repeat-01"
            tasks = rep / "tasks"
            if not tasks.is_dir():
                continue
            for td in sorted(tasks.iterdir()):
                rp = td / "replay-result.json"
                if not rp.is_file():
                    continue
                try:
                    r = json.loads(rp.read_text())
                except Exception as e:
                    n_parse_err += 1
                    print(f"WARN parse {rp}: {e}", file=sys.stderr)
                    continue
                n_tasks += 1
                traj = re.sub(r"^\d+-", "", td.name)
                t0 = parse_ts(r.get("started_at"))
                for s in r.get("steps", []):
                    idx = s.get("index")
                    if idx is None:
                        continue
                    key = (traj, idx)
                    a = agg.get(key)
                    if a is None:
                        a = agg[key] = {
                            "n": 0, "n_ok": 0, "n_fail": 0, "n_timeout": 0,
                            "cmd_sum": 0.0, "cmd_sum_ok": 0.0,
                            "start_sum": 0.0, "wait_sum": 0.0,
                            "rel_start_sum": 0.0, "rel_start_n": 0,
                            "action": (s.get("action") or "")[:400],
                        }
                    cmd = s.get("command_sec")
                    if cmd is None:
                        cmd = s.get("elapsed_sec") or 0.0
                    a["n"] += 1
                    a["cmd_sum"] += cmd or 0.0
                    a["start_sum"] += s.get("command_start_sec") or 0.0
                    a["wait_sum"] += s.get("command_wait_sec") or 0.0
                    if s.get("timed_out"):
                        a["n_timeout"] += 1
                    elif not s.get("succeeded"):
                        a["n_fail"] += 1
                    else:
                        a["n_ok"] += 1
                        a["cmd_sum_ok"] += cmd or 0.0
                    ts = parse_ts(s.get("started_at"))
                    if ts is not None and t0 is not None:
                        a["rel_start_sum"] += max(0.0, (ts - t0).total_seconds())
                        a["rel_start_n"] += 1
        path = OUT / f"step_agg_{cell}.csv"
        cols = ["trajectory", "step_index", "n", "n_ok", "n_fail", "n_timeout",
                "cmd_sum", "cmd_sum_ok", "start_sum", "wait_sum",
                "rel_start_mean", "action"]
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for (traj, idx) in sorted(agg):
                a = agg[(traj, idx)]
                rel = a["rel_start_sum"] / a["rel_start_n"] if a["rel_start_n"] else ""
                w.writerow([traj, idx, a["n"], a["n_ok"], a["n_fail"], a["n_timeout"],
                            f"{a['cmd_sum']:.3f}", f"{a['cmd_sum_ok']:.3f}",
                            f"{a['start_sum']:.3f}", f"{a['wait_sum']:.3f}",
                            (f"{rel:.1f}" if rel != "" else ""),
                            a["action"].replace("\r", " ").replace("\n", " ")])
        print(f"{cell}: tasks={n_tasks} step_rows={len(agg)} parse_err={n_parse_err} -> {path}")


if __name__ == "__main__":
    main()
