#!/usr/bin/env python3
"""Host-side USE collector for AgentENV high-oversubscription profiling.

All metrics are kernel-counter deltas (lossless across sampling gaps):
  5s tick:
    /proc/stat      -> per-CCL CPU utilization (CCL groups from topology), procs_running/blocked
    /proc/pressure  -> cpu/mem/io PSI "some"/"full" cumulative us -> stalled fraction
    /proc/meminfo   -> MemFree/Cached/Dirty/AnonPages/Slab/PageTables/Shmem
    /proc/vmstat    -> pgscan/pgsteal/pgrefault/pswpin/pswpout/pgmajfault/allocstall (30s)
    /proc/diskstats -> per-disk IOPS/MBps/await/%util
    /proc/net/dev   -> rx/tx bytes totals
    daemon /proc/PID/stat -> server + ublk-daemon CPU seconds
  10s tick:
    FC vCPU schedstat per CCL: delta run_s / wait_s / timeslices -> queueing (steal) rate
Discovery (30s): firecracker pids, their Cpus_allowed_list -> CCL, vcpu tids.

Output: $HCOLLECT_OUT/hcollect.jsonl  +  summary.json (on TERM/INT).
Run as root, e.g.:  setsid python3 hcollect.py <outdir> &
Stop gracefully:    kill -TERM <pid>
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

TICK = float(os.environ.get("HCOLLECT_INTERVAL", "5"))
VTICK = float(os.environ.get("HCOLLECT_VCPU_INTERVAL", "10"))
DISCOVER = float(os.environ.get("HCOLLECT_DISCOVER_INTERVAL", "30"))
CLK_TCK = os.sysconf("SC_CLK_TCK")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ["HCOLLECT_OUT"])
OUT.mkdir(parents=True, exist_ok=True)

_stop = {"flag": False}
_server_hint = {"pid": None}


def on_term(signum, frame):
    _stop["flag"] = True


signal.signal(signal.SIGTERM, on_term)
signal.signal(signal.SIGINT, on_term)
try:
    os.nice(-10)
except Exception:
    pass


# ---------- topology: CCL groups ----------
def cpu_ccl_map():
    """Map logical cpu -> CCL index using cluster_cpus_list; fallback: cpu//16."""
    groups = {}
    ncpu = os.cpu_count() or 384
    per = {}
    for cpu in range(ncpu):
        try:
            with open(f"/sys/devices/system/cpu/cpu{cpu}/topology/cluster_cpus_list") as f:
                key = tuple(sorted(int(x) for x in
                                   parse_cpu_list(f.read().strip())))
            groups.setdefault(key, len(groups))
            per[cpu] = groups[key]
        except Exception:
            return {c: c // 16 for c in range(ncpu)}
    return per


def parse_cpu_list(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


CCL_MAP = cpu_ccl_map()
NCCL = (max(CCL_MAP.values()) + 1) if CCL_MAP else 1


# ---------- readers ----------
def read_procstat():
    per = {}
    ctx = None
    run = blocked = None
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and line[3].isdigit():
                p = line.split()
                cpu = int(p[0][3:])
                per[cpu] = [int(x) for x in p[1:]]
            elif line.startswith("procs_running"):
                run = int(line.split()[1])
            elif line.startswith("procs_blocked"):
                blocked = int(line.split()[1])
            elif line.startswith("ctxt "):
                ctx = int(line.split()[1])
    return per, run, blocked, ctx


def read_psi(name):
    out = {}
    try:
        with open(f"/proc/pressure/{name}") as f:
            for line in f:
                kind, rest = line.split(" ", 1)
                d = dict(kv.split("=") for kv in rest.strip().split(" "))
                out[kind] = int(d["total"])
    except Exception:
        pass
    return out


MEMINFO_KEYS = ["MemFree", "MemAvailable", "Cached", "Buffers", "SReclaimable",
                "Dirty", "Writeback", "AnonPages", "Slab", "PageTables", "Shmem",
                "SwapCached", "KReclaimable", "MemTotal"]


def read_meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k = line.split(":", 1)[0]
            if k in MEMINFO_KEYS:
                out[k] = int(line.split()[1])  # KiB
    return out


VMSTAT_KEYS = ["pgscan", "pgsteal", "pgrefault", "pswpin", "pswpout", "pgmajfault",
               "allocstall", "allocstall_normal", "compact_stall", "thp_fault_alloc",
               "pgactivate", "pgdeactivate", "pgpromote_success", "pgdemote_kswapd"]


def read_vmstat():
    out = {}
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                k = line.split()[0]
                if k in VMSTAT_KEYS or k.startswith("pgscan") or k.startswith("pgsteal") \
                        or k.startswith("allocstall") or k.startswith("pgdemote") \
                        or k.startswith("pgpromote"):
                    out[k] = int(line.split()[1])
    except Exception:
        pass
    return out


def read_diskstats():
    disks = {}
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            name = p[2]
            if not (name.startswith(("nvme", "sd", "vd", "mmcblk"))) or "ublk" in name:
                continue
            try:
                disks[name] = {
                    "r": int(p[3]), "rm": int(p[6]),
                    "w": int(p[7]), "wm": int(p[11]),
                    "io_ms": int(p[12]),
                }
            except Exception:
                continue
    return disks


def read_netdev():
    rx = tx = 0
    try:
        with open("/proc/net/dev") as f:
            f.readline()
            f.readline()
            for line in f:
                p = line.split(":", 1)
                if len(p) == 2:
                    rx += int(p[1].split()[0])
                    tx += int(p[1].split()[8])
    except Exception:
        pass
    return rx, tx


# ---------- firecracker discovery ----------
def discover_fc():
    """Return {pid: {"ccl": int, "vcpus": [tid, ...]}} for all firecracker processes.
    Also stashes a server pid (ppid of the first firecracker) into _server_hint."""
    fc = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() != "firecracker":
                    continue
            if _server_hint.get("pid") is None:
                try:
                    with open(f"/proc/{pid}/stat") as f:
                        ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                    if ppid != 1:
                        _server_hint["pid"] = ppid
                except Exception:
                    pass
            ccl = None
            try:
                with open(f"/proc/{pid}/status") as f:
                    for line in f:
                        if line.startswith("Cpus_allowed_list:"):
                            cpus = parse_cpu_list(line.split(":", 1)[1].strip())
                            ccls = {CCL_MAP.get(c, -1) for c in cpus}
                            ccl = ccls.pop() if len(ccls) == 1 else -1
                            break
            except Exception:
                pass
            vcpus = []
            for tid_s in os.listdir(f"/proc/{pid}/task"):
                tid = int(tid_s)
                try:
                    with open(f"/proc/{pid}/task/{tid_s}/comm") as f:
                        comm = f.read().strip()
                    if comm.startswith("fc_vcpu"):
                        vcpus.append(tid)
                except Exception:
                    continue
            fc[pid] = {"ccl": ccl, "vcpus": vcpus}
        except Exception:
            continue
    return fc


def read_vcpu_sched(fc):
    """{ccl: [run_ns, wait_ns, slices]} deltas accumulated here as raw values."""
    per = {}
    for pid, info in fc.items():
        ccl = info["ccl"]
        acc = per.setdefault(ccl, [0, 0, 0, 0])
        acc[3] += len(info["vcpus"])
        for tid in info["vcpus"]:
            try:
                with open(f"/proc/{pid}/task/{tid}/schedstat") as f:
                    vals = [int(x) for x in f.read().split()]
                acc[0] += vals[0]
                acc[1] += vals[1]
                acc[2] += vals[2]
            except Exception:
                continue
    return per


def find_daemons():
    server = ublk = None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as f:
                comm = f.read().strip()
        except Exception:
            continue
        if comm == "uvm-ublk-daemon" and ublk is None:
            ublk = int(entry)
    if _server_hint.get("pid"):
        server = _server_hint["pid"]
    return server, ublk


def read_pid_cpu(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            p = f.read().rsplit(")", 1)[1].split()
            return (int(p[11]) + int(p[12])) / CLK_TCK  # utime+stime sec
    except Exception:
        return None


# ---------- main loop ----------
def main():
    t0 = time.monotonic()
    prev_stat, prev_run, prev_blocked, prev_ctx = read_procstat()
    prev_psi = {n: read_psi(n) for n in ("cpu", "mem", "io")}
    prev_mem = read_meminfo()
    prev_vm = read_vmstat()
    prev_disk = read_diskstats()
    prev_net = read_netdev()
    fc = discover_fc()
    prev_vcpu = read_vcpu_sched(fc)
    server, ublk = find_daemons()
    prev_srv = read_pid_cpu(server) if server else 0.0
    prev_ubl = read_pid_cpu(ublk) if ublk else 0.0
    last_v = time.monotonic()
    last_d = time.monotonic()
    n_vm_seen = 0

    out_path = OUT / "hcollect.jsonl"
    print(f"hcollect: ncpu={len(CCL_MAP)} nccl={NCCL} fc={len(fc)} "
          f"vcpus={sum(len(i['vcpus']) for i in fc.values())} server={server} ublk={ublk} "
          f"out={out_path}", flush=True)
    with open(out_path, "w", buffering=1) as mf:
        while not _stop["flag"]:
            time.sleep(0.2)
            now = time.monotonic()
            if now - last_d >= DISCOVER:
                fc = discover_fc()
                n_vm_seen = len(fc)
                last_d = now
            if now - last_v >= VTICK:
                cur = read_vcpu_sched(fc)
                dt = now - last_v
                vline = {"t": round(now - t0, 1), "dt": round(dt, 2), "vcpu": {}}
                for ccl, vals in cur.items():
                    p = prev_vcpu.get(ccl)
                    if p and vals[3]:
                        vline["vcpu"][str(ccl)] = [
                            round((vals[0] - p[0]) / 1e9, 3),
                            round((vals[1] - p[1]) / 1e9, 3),
                            vals[2] - p[2], vals[3],
                        ]
                tot_run = sum(v[0] for v in vline["vcpu"].values())
                tot_wait = sum(v[1] for v in vline["vcpu"].values())
                vline["vcpu_total"] = {
                    "run_s": round(tot_run, 2), "wait_s": round(tot_wait, 2),
                    "vms": n_vm_seen,
                }
                mf.write(json.dumps(vline) + "\n")
                prev_vcpu = cur
                last_v = now
                continue
            # 5s tick
            stat, run, blocked, ctx = read_procstat()
            dt = TICK
            per_ccl = {}
            guest_ccl = {}
            for cpu, cur in stat.items():
                ccl = CCL_MAP.get(cpu)
                if ccl is None:
                    continue
                prev = prev_stat.get(cpu)
                if prev is None or len(cur) < 10 or len(prev) < 10:
                    continue
                d = [cur[i] - prev[i] for i in range(10)]
                d_total = sum(d[:8])          # guest/guest_nice already inside user/nice
                d_idle = d[3] + d[4]
                busy = (d_total - d_idle)
                acc = per_ccl.setdefault(str(ccl), [0, 0])
                acc[0] += busy
                acc[1] += d_total
                guest_ccl[str(ccl)] = guest_ccl.get(str(ccl), 0) + d[8] + d[9]
            cpu_ccl_pct = {k: round(100.0 * v[0] / v[1], 1) if v[1] else 0.0
                           for k, v in per_ccl.items()}
            guest_ccl_pct = {k: round(100.0 * v / per_ccl[k][1], 1) if per_ccl.get(k) else 0.0
                             for k, v in guest_ccl.items()}
            psi = {n: read_psi(n) for n in ("cpu", "mem", "io")}
            psi_frac = {}
            for n, d in psi.items():
                pd = prev_psi.get(n, {})
                for kind, tot in d.items():
                    pt = pd.get(kind)
                    if pt is not None:
                        psi_frac[f"{n}_{kind}"] = round((tot - pt) / 1e6 / dt, 4)
            mem = read_meminfo()
            vm = read_vmstat()
            disk = read_diskstats()
            dline = {}
            for name, cur in disk.items():
                prev = prev_disk.get(name)
                if not prev:
                    continue
                dr = cur["r"] - prev["r"]
                dw = cur["w"] - prev["w"]
                drm = cur["rm"] - prev["rm"]
                dwm = cur["wm"] - prev["wm"]
                dio = cur["io_ms"] - prev["io_ms"]
                ios = dr + dw
                dline[name] = {
                    "iops": ios,
                    "r_mb": round(dr * 512 / 2**20, 2), "w_mb": round(dw * 512 / 2**20, 2),
                    "await_ms": round((drm + dwm) / ios, 2) if ios else 0.0,
                    "util_pct": round(100.0 * dio / 1000.0 / dt, 1),
                }
            rx, tx = read_netdev()
            srv = read_pid_cpu(server) if server else None
            ubl = read_pid_cpu(ublk) if ublk else None
            rec = {
                "t": round(now - t0, 1), "dt": dt,
                "cpu_ccl_pct": cpu_ccl_pct,
                "guest_ccl_pct": guest_ccl_pct,
                "run": run, "blocked": blocked,
                "psi": psi_frac,
                "mem": {k: round(v / 2**20, 3) for k, v in mem.items()},
                "vm": {k: (v - prev_vm.get(k, v)) for k, v in vm.items()},
                "disk": dline,
                "net_rx_mb": round((rx - prev_net[0]) / 2**20, 2),
                "net_tx_mb": round((tx - prev_net[1]) / 2**20, 2),
                "srv_cpu_s": round(srv - prev_srv, 3) if srv is not None else None,
                "ublk_cpu_s": round(ubl - prev_ubl, 3) if ubl is not None else None,
            }
            mf.write(json.dumps(rec) + "\n")
            prev_stat, prev_run, prev_blocked, prev_ctx = stat, run, blocked, ctx
            prev_psi = psi
            prev_mem = mem
            prev_vm = vm
            prev_disk = disk
            prev_net = (rx, tx)
            if srv is not None:
                prev_srv = srv
            if ubl is not None:
                prev_ubl = ubl
            # sleep to next 5s boundary
            target = last_v + VTICK  # not used
            time.sleep(max(0.0, TICK - (time.monotonic() - now)))

    # summary
    print("hcollect: stopping, writing summary", flush=True)
    (OUT / "summary.txt").write_text(
        f"duration_s={time.monotonic() - t0:.0f} nccl={NCCL} last_vms={n_vm_seen}\n")


if __name__ == "__main__":
    main()
