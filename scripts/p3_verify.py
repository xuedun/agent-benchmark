#!/usr/bin/env python3
"""Verify the profile_8x run reproduces the original 8x slowdown (partial data).

Compares per-step mean command_sec of the profile run vs the original 1_1 and 8_2
cells, by category. Only steps actually executed in the profile run count.
"""
import json
import re
import sys
from pathlib import Path
from collections import defaultdict

BASE = Path("/home/cxd/agent-benchmark/logs_mix_v3")
PROFILE = BASE / "profile_8x"
CELLS_ORIG = {"1_1": "1_1", "8_2": "8_2"}

CAT_RULES = [
    ("maven", re.compile(r"\bmvn\b")),
    ("npm/node", re.compile(r"\b(npm|npx|yarn|pnpm|node)\b")),
    ("go", re.compile(r"\bgo (build|test|mod|run|vet|clean|install|generate|work)\b")),
    ("pip", re.compile(r"\b(pip3?|python3? -m pip)\b")),
    ("python/pytest", re.compile(r"\b(python3?|pytest|unittest)\b")),
    ("compile(c/c++/rust)", re.compile(r"\b(gcc|g\+\+|clang|rustc|cargo|make)\b")),
    ("fs-scan", re.compile(r"^(find|ls|grep|rg|wc|head|tail|cat|du|stat|file)\b")),
    ("fs-write", re.compile(r"^(touch|mkdir|cp|mv|rm|ln|tar|zip|unzip|chmod|chown|sed|awk|echo|tee|dd)\b")),
]


def categorize(action):
    for name, rx in CAT_RULES:
        if rx.search(action or ""):
            return name
    return "shell-other"


def load_orig(cell):
    rows = {}
    with open(BASE / f"analysis/step_agg_{cell}.csv") as f:
        for r in csv_row(f):
            rows[(r["trajectory"], int(r["step_index"]))] = (
                float(r["cmd_sum"]) / int(r["n"]), r["action"])
    return rows


def csv_row(f):
    import csv
    return csv.DictReader(f)


def main():
    # profile run steps
    agg = {}
    n_tasks = 0
    for td in sorted((PROFILE / "ratio-08/repeat-01/tasks").iterdir()):
        rp = td / "replay-result.json"
        if not rp.is_file():
            continue
        try:
            r = json.loads(rp.read_text())
        except Exception:
            continue
        n_tasks += 1
        traj = re.sub(r"^\d+-", "", td.name)
        for s in r.get("steps", []):
            key = (traj, s.get("index"))
            a = agg.setdefault(key, {"n": 0, "sum": 0.0, "action": (s.get("action") or "")[:200]})
            cmd = s.get("command_sec") or s.get("elapsed_sec") or 0.0
            a["n"] += 1
            a["sum"] += cmd
    print(f"profile_8x: completed tasks={n_tasks}, step rows={len(agg)}")

    orig11 = load_orig("1_1")
    orig82 = load_orig("8_2")
    cat = defaultdict(lambda: {"n": 0, "p_sum": 0.0, "o11_sum": 0.0, "o82_sum": 0.0})
    for key, a in agg.items():
        if key not in orig11 or key not in orig82:
            continue
        c = categorize(a["action"])
        m11, _ = orig11[key]
        m82, _ = orig82[key]
        d = cat[c]
        d["n"] += a["n"]
        d["p_sum"] += a["sum"]
        # weight original means by profile-run copy count
        d["o11_sum"] += m11 * a["n"]
        d["o82_sum"] += m82 * a["n"]

    print(f"\n{'category':<22}{'copies':>8}{'1_1_mean':>10}{'8_2_orig':>10}"
          f"{'profile':>10}{'prof/1x':>9}{'prof/8x':>9}")
    tot = {"n": 0, "p": 0.0, "o11": 0.0, "o82": 0.0}
    for name, d in sorted(cat.items(), key=lambda kv: -kv[1]["p_sum"]):
        if d["n"] == 0:
            continue
        pm = d["p_sum"] / d["n"]
        o11 = d["o11_sum"] / d["n"]
        o82 = d["o82_sum"] / d["n"]
        tot["n"] += d["n"]
        tot["p"] += d["p_sum"]
        tot["o11"] += d["o11_sum"]
        tot["o82"] += d["o82_sum"]
        print(f"{name:<22}{d['n']:>8}{o11:>10.3f}{o82:>10.3f}{pm:>10.3f}"
              f"{pm/o11 if o11 else 0:>9.2f}{pm/o82 if o82 else 0:>9.2f}")
    if tot["n"]:
        pm = tot["p"] / tot["n"]
        o11 = tot["o11"] / tot["n"]
        o82 = tot["o82"] / tot["n"]
        print(f"{'TOTAL(weighted)':<22}{tot['n']:>8}{o11:>10.3f}{o82:>10.3f}{pm:>10.3f}"
              f"{pm/o11:>9.2f}{pm/o82:>9.2f}")


if __name__ == "__main__":
    main()
