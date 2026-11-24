#!/usr/bin/env python3
"""Dirty-page pause/resume matrix experiment for AgentENV.

Cross-product of memory-dirty sizes (anonymous, per-4K-page touched) and
file-dirty sizes (fsync'ed to the rootfs -> overlaybd upper layer):
per trial: create VM -> inject dirty -> pause (timed) -> record snapshot
size -> clean host page cache -> resume (timed) -> verify alive -> delete.

Usage:
  .venv/bin/python dirty_pause_matrix.py \
      --mem-sizes  0,128,256,512,1024,2048 \
      --file-sizes 0,128,512,1024 \
      --template swb-nats-io-nats-server-5783 \
      --repeats 3 --out /home/cxd/aenv-perf/data/dirty_pause [--cache-clean drop]

Outputs: <out>/results.jsonl (one line per trial) + <out>/summary.txt
"""
import argparse
import logging
import asyncio
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("E2B_API_URL", "http://127.0.0.1:8000")
os.environ.setdefault("E2B_SANDBOX_URL", "http://127.0.0.1:8000")
os.environ.setdefault("E2B_ACCESS_TOKEN", "dummy")
from e2b import AsyncSandbox
from e2b.api.client.client import AuthenticatedClient

AuthenticatedClient.auth_header_name = "X-API-Key"
AuthenticatedClient.prefix = ""

API = "http://127.0.0.1:8000"


def api(method, path, data=None, timeout=600):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"X-API-Key": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        try:
            return json.loads(body) if body else {}
        except ValueError:
            return {}


def wait_state(sid, target, timeout=900, poll=0.1):
    """Poll sandbox state; returns seconds to reach target, else None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = api("GET", f"/sandboxes/{sid}", timeout=15)
        except Exception:
            time.sleep(poll)
            continue
        if str(d.get("state", "")).lower() == target:
            return time.time() - t0
        time.sleep(poll)
    return None


def snapshot_artifact_stats(sid, base="/data/aenv/persisted-sandboxes/artifacts"):
    """Size breakdown of this sandbox's pause artifacts (real path: the
    persisted-sandboxes artifact root, NOT the snapshot-store repository).

    Live pause artifacts: vm_state.bin + mem_overlaybd/overlaybd.commit
    (memory diff layer) + rootfs/snapshot.commit (sealed upper layer).
    Returns (total_bytes, {component: bytes})."""
    root = Path(base) / sid
    comp = {}
    if not root.is_dir():
        return 0, comp
    for p in root.rglob("*"):
        if p.is_file():
            comp[str(p.relative_to(root))] = p.stat().st_size
    return sum(comp.values()), comp


def snapshot_bytes_since(marker, base="/data/aenv/persisted-sandboxes/artifacts"):
    """Legacy helper kept for compatibility; see snapshot_artifact_stats."""
    return 0


def clean_host_cache(mode):
    if mode == "none":
        return
    subprocess.run(["sync"], timeout=120)
    if mode == "drop":
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
    # mode == "sync": only writeback, cache retained (warm-resume arm)


MEM_DIRTY_SH = """nohup python3 -c '
import sys, time
S = int(float(sys.argv[1]) * 1024 * 1024)
buf = bytearray(S)
for i in range(0, S, 4096):
    buf[i] = i & 0xFF
print("MEMDIRTY_OK", S // (1024 * 1024), flush=True)
time.sleep(7200)
' {mb} >/tmp/memdirty.log 2>&1 & echo BG_STARTED"""


async def e2b_call(coro, patience):
    """e2b SDK has no internal timeout on dead/paused sandboxes -- every call
    must be wrapped or the driver hangs forever."""
    return await asyncio.wait_for(coro, timeout=patience)


async def connect_with_patience(sid, total_patience=120.0, attempt_patience=30.0,
                                interval=3.0):
    """Connect once and keep the session (dropped sessions trigger server-side
    auto-pause churn). `timeout` param of SDK connect is the SANDBOX timeout
    to set -- do not pass it; patience is enforced here via wait_for."""
    t0 = time.time()
    last = None
    while time.time() - t0 < total_patience:
        try:
            sb = await e2b_call(AsyncSandbox.connect(sid, api_key=KEY),
                                attempt_patience)
            return sb
        except asyncio.TimeoutError:
            last = "timeout"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        await asyncio.sleep(interval)
    raise RuntimeError(f"connect_with_patience failed after {total_patience}s: {last}")


async def sb_run(sb, cmd, timeout=300):
    """Run a command on a HELD connection; returns (stdout, exit_code)."""
    try:
        r = await e2b_call(sb.commands.run(cmd, user="root", timeout=timeout,
                                           request_timeout=timeout), timeout + 30)
        return r.stdout or "", r.exit_code
    except Exception as e:
        return f"__ERR__{type(e).__name__}", -1


async def one_trial(tpl, mem_mb, file_mb, cache_clean, out_jsonl, trial_id):
    rec = {"trial": trial_id, "template": tpl, "mem_mb": mem_mb,
           "file_mb": file_mb, "t_start": time.time()}
    sid = None

    def prog(step, extra=""):
        dt = time.time() - rec["t_start"]
        print(f"    [t+{dt:5.1f}s] {step}{(' ' + extra) if extra else ''}",
              flush=True)

    try:
        prog("create...")
        # 1. create
        t0 = time.time()
        d = api("POST", "/sandboxes",
                {"templateID": tpl, "timeout": 7200}, timeout=180)
        sid = d.get("sandboxID") or d.get("sandboxId")
        if not sid:
            rec["error"] = f"create failed: {json.dumps(d)[:200]}"
            return rec
        rec["create_ms"] = round((time.time() - t0) * 1000)
        ws = wait_state(sid, "running", timeout=300)
        if ws is None:
            rec["error"] = "never reached running"
            return rec
        prog("connect (envd)...")
        # connect ONCE with patience (envd boot takes seconds; failed
        # connect attempts trigger server-side auto-pause -- be patient)
        try:
            sb = await connect_with_patience(sid)
        except Exception as e:
            rec["error"] = f"connect: {e}"
            return rec
        rec["envd_connect_s"] = round(time.time() - rec["t_start"] - rec["create_ms"] / 1000, 1)
        out, rc = await sb_run(sb, "echo ENVD_OK", timeout=60)
        if "ENVD_OK" not in out:
            rec["error"] = f"envd not ready: {out[:80]}"
            return rec

        # 2. inject dirty
        if file_mb > 0:
            prog(f"file dirty {file_mb}MB...")
            kb = max(1, round(file_mb * 1024))
            t0 = time.time()
            out, rc = await sb_run(
                sb,
                f"dd if=/dev/urandom of=/root/dirtyfile bs=1K count={kb} "
                f"conv=fsync 2>&1 | tail -1 && sync && echo FILE_OK")
            rec["file_dirty_inject_s"] = round(time.time() - t0, 1)
            if "FILE_OK" not in out:
                rec["error"] = f"file dirty failed rc={rc}"
                return rec
        if mem_mb > 0:
            prog(f"mem dirty {mem_mb}MB...")
            t0 = time.time()
            out, rc = await sb_run(
                sb, MEM_DIRTY_SH.format(mb=mem_mb).replace("\n", " "))
            rec["mem_dirty_inject_s"] = round(time.time() - t0, 1)
            if "BG_STARTED" not in out:
                rec["error"] = f"mem dirty failed rc={rc}"
                return rec
            # poll for the touch loop's completion marker (fast, deterministic)
            for _ in range(20):
                out2, _ = await sb_run(sb, "cat /tmp/memdirty.log 2>/dev/null")
                if "MEMDIRTY_OK" in (out2 or ""):
                    break
                await asyncio.sleep(0.5)
        prog("verify meminfo...")
        # verify dirty state
        out, _ = await sb_run(
            sb, "grep -E 'AnonPages|^Dirty' /proc/meminfo")
        for line in out.splitlines():
            k, v = line.split(":", 1)
            rec[f"guest_{k.strip()}"] = int(v.strip().split()[0]) // 1024  # MiB

        # 3. pause (timed): pause takes no request body
        prog("PAUSE (timed)...")
        marker = f"/tmp/dp_marker_{int(time.time())}"
        Path(marker).touch()
        t0 = time.time()
        pause_failed = False
        try:
            api("POST", f"/sandboxes/{sid}/pause", timeout=1200)
            rec["pause_api_ms"] = round((time.time() - t0) * 1000)
        except urllib.error.HTTPError as e:
            rec["pause_api_ms"] = round((time.time() - t0) * 1000)
            rec["pause_http_error"] = e.code
            pause_failed = True
        # if the API call itself was rejected, do not burn the full timeout
        ws = wait_state(sid, "paused", timeout=30 if pause_failed else 1200)
        rec["pause_total_ms"] = round((time.time() - t0) * 1000)
        rec["pause_state_reached"] = ws is not None
        total_b, comp = snapshot_artifact_stats(sid)
        rec["snapshot_bytes"] = total_b
        rec["snapshot_mem_layer_bytes"] = next(
            (v for k, v in comp.items() if "mem_overlaybd" in k), 0)
        rec["snapshot_rootfs_layer_bytes"] = next(
            (v for k, v in comp.items() if k.endswith("rootfs/snapshot.commit")), 0)
        rec["snapshot_vm_state_bytes"] = next(
            (v for k, v in comp.items() if k.endswith("vm_state.bin")), 0)
        try:
            os.unlink(marker)
        except OSError:
            pass

        prog(f"snap={rec.get('snapshot_bytes', 0) / 2**20:.1f}MB; cache clean ({cache_clean})...")
        # 4. clean host page cache (cold-resume arm)
        t0 = time.time()
        clean_host_cache(cache_clean)
        rec["cache_clean_s"] = round(time.time() - t0, 1)

        prog("RESUME (timed)...")
        # 5. resume (timed): resume REQUIRES a JSON body (ResumedSandbox);
        # an empty body is rejected with 415 and the resume never runs.
        # `timeout` is the sandbox TTL after resume (default 15s -> auto
        # eviction mid-trial), so keep it aligned with the create TTL.
        t0 = time.time()
        resume_failed = False
        try:
            api("POST", f"/sandboxes/{sid}/resume", {"timeout": 7200},
                timeout=1200)
            rec["resume_api_ms"] = round((time.time() - t0) * 1000)
        except urllib.error.HTTPError as e:
            rec["resume_api_ms"] = round((time.time() - t0) * 1000)
            rec["resume_http_error"] = e.code
            resume_failed = True
        ws = wait_state(sid, "running", timeout=30 if resume_failed else 1200)
        rec["resume_total_ms"] = round((time.time() - t0) * 1000)
        rec["resume_state_reached"] = ws is not None

        prog("alive check (reconnect)...")
        # 6. verify alive: the pre-pause session is dead after pause/resume;
        # reconnect with patience (a paused->resumed sandbox's envd needs a
        # moment to accept connections again)
        if resume_failed or ws is None:
            rec["alive_after_resume"] = False
            rec["alive_check_error"] = "skipped: resume did not run"
        else:
            try:
                sb2 = await connect_with_patience(sid, total_patience=60.0,
                                                  interval=1.0)
                out, rc = await sb_run(sb2, "echo ALIVE_OK", timeout=120)
                rec["alive_after_resume"] = "ALIVE_OK" in out
                if mem_mb > 0:
                    out2, _ = await sb_run(sb2, "cat /tmp/memdirty.log 2>/dev/null")
                    rec["memdirty_held"] = "MEMDIRTY_OK" in (out2 or "")
            except Exception as e:
                rec["alive_after_resume"] = False
                rec["alive_check_error"] = f"{type(e).__name__}"
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
    finally:
        if sid:
            prog("delete...")
            try:
                api("DELETE", f"/sandboxes/{sid}", timeout=120)
            except Exception:
                pass
            # drain
            for _ in range(8):
                try:
                    d = api("GET", f"/sandboxes/{sid}", timeout=10)
                    if not d or "sandboxID" not in json.dumps(d):
                        break
                except Exception:
                    break
                time.sleep(2)
    rec["t_end"] = time.time()
    with open(out_jsonl, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


async def main():
    global KEY
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mem-sizes", required=True,
                   help="comma list of anonymous dirty MB (0 = baseline)")
    p.add_argument("--file-sizes", required=True,
                   help="comma list of fsync'ed file dirty MB (0 = baseline); "
                        "fractions allowed (0.001 = 1KB)")
    p.add_argument("--paired", action="store_true",
                   help="iterate zip(mem,file) pairs instead of cross product "
                        "(use when the lists correspond 1:1)")
    p.add_argument("--template", default="swb-nats-io-nats-server-5783")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--out", default="/home/cxd/aenv-perf/data/dirty_pause")
    p.add_argument("--cache-clean", choices=("drop", "sync", "none"),
                   default="none",
                   help="none (default, fast): untouched. drop: global sync + "
                        "echo 3 > drop_caches -- SLOW on big shared machines "
                        "(sync flushes the whole host) and slows other tenants; "
                        "use only when cold-resume measurement is required. "
                        "sync: writeback only")
    p.add_argument("--api-key-file", default="/data/aenv/secrets/api-key")
    args = p.parse_args()
    if os.environ.get("E2B_DEBUG_HTTP"):
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")
    KEY = open(args.api_key_file).read().strip()

    mem_sizes = [float(x) for x in args.mem_sizes.split(",") if x.strip()]
    file_sizes = [float(x) for x in args.file_sizes.split(",") if x.strip()]
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    out_jsonl = outdir / "results.jsonl"

    if args.paired and len(mem_sizes) != len(file_sizes):
        print("ERROR: --paired requires equal-length lists", file=sys.stderr)
        return 1
    mode = "pairs" if args.paired else "cross"
    print(f"matrix({mode}): mem={mem_sizes} file={file_sizes} x {args.repeats} reps, "
          f"template={args.template}, cache_clean={args.cache_clean}", flush=True)

    combos = (list(zip(mem_sizes, file_sizes)) if args.paired
              else [(m, f_) for m in mem_sizes for f_ in file_sizes])
    all_recs = []
    trial = 0
    for rep in range(args.repeats):
        for mem_mb, file_mb in combos:
                trial += 1
                print(f"[{time.strftime('%H:%M:%S')}] trial {trial}: "
                      f"mem={mem_mb}MB file={file_mb}MB (rep {rep + 1})",
                      flush=True)
                rec = await one_trial(args.template, mem_mb, file_mb,
                                      args.cache_clean, out_jsonl, trial)
                all_recs.append(rec)
                err = rec.get("error")
                print(f"    pause={rec.get('pause_total_ms')}ms "
                      f"resume={rec.get('resume_total_ms')}ms "
                      f"snap={rec.get('snapshot_bytes', 0)}B "
                      f"alive={rec.get('alive_after_resume')}"
                      + (f" ERROR={err}" if err else ""), flush=True)

    # summary
    from collections import defaultdict
    import statistics as st
    agg = defaultdict(list)
    for r in all_recs:
        if r.get("pause_total_ms") is not None:
            agg[(r["mem_mb"], r["file_mb"])].append(r)
    lines = [f"# dirty-pause matrix: template={args.template} "
              f"cache_clean={args.cache_clean} reps={args.repeats}",
              f"{'mem_mb':>8}{'file_mb':>9}{'n':>3}{'pause_ms':>12}{'resume_ms':>12}"
              f"{'snap_MB':>10}{'memly_MB':>10}{'rootfs_MB':>10}{'err':>5}"]
    for (m, f_), rs in sorted(agg.items()):
        pm = [r["pause_total_ms"] for r in rs]
        rm = [r["resume_total_ms"] for r in rs]
        sn = [r.get("snapshot_bytes", 0) / 2**20 for r in rs]
        ml = [r.get("snapshot_mem_layer_bytes", 0) / 2**20 for r in rs]
        rl = [r.get("snapshot_rootfs_layer_bytes", 0) / 2**20 for r in rs]
        errs = sum(1 for r in rs if r.get("error"))
        lines.append(f"{m:>8}{f_:>9}{len(rs):>3}"
                      f"{st.mean(pm):>12.0f}{st.mean(rm):>12.0f}"
                      f"{st.mean(sn):>10.1f}{st.mean(ml):>10.1f}"
                      f"{st.mean(rl):>10.1f}{errs:>5}")
    (outdir / "summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
