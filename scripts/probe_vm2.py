#!/usr/bin/env python3
"""Decisive micro-decomposition: fixed probes in ONE sandbox under varying host load.

Probes (bash one-liners via e2b SDK; host wall + guest internal time):
  compute : python loop 10^7 adds        -> pure vCPU compute
  memory  : python touch 200MB bytearray -> guest memory latency/bw
  iocold  : drop guest caches; cat 200M+ file -> virtio/ublk/overlaybd path
Usage: .venv/bin/python probe_vm2.py <label> [rounds]  -> /tmp/opencode/probe_<label>.json
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("E2B_API_URL", "http://127.0.0.1:8000")
os.environ.setdefault("E2B_SANDBOX_URL", "http://127.0.0.1:8000")
os.environ.setdefault("E2B_ACCESS_TOKEN", "dummy")
from e2b import AsyncSandbox
from e2b.api.client.client import AuthenticatedClient

AuthenticatedClient.auth_header_name = "X-API-Key"
AuthenticatedClient.prefix = ""

TEMPLATE = "swb-aftership-clickhouse-sql-parser-128"
KEY = open("/data/aenv/secrets/api-key").read().strip()
LABEL = sys.argv[1] if len(sys.argv) > 1 else "run"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
OUT = Path("/tmp/opencode") / f"probe_{LABEL}.json"

COMPUTE = "awk 'BEGIN{x=0;for(i=0;i<20000000;i++)x=(x+i)%4294967296;print x}'"
MEMORY_W = "dd if=/dev/zero of=/dev/shm/probe.bin bs=1M count=300 2>&1 | tail -1"
MEMORY_R = "T0=$(date +%s.%N); cat /dev/shm/probe.bin > /dev/null; T1=$(date +%s.%N); echo GUEST_SEC=$(echo \"$T1 $T0\" | awk '{print $1-$2}')"
IO_TMPL = ("sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null; "
           "T0=$(date +%s.%N); cat '{f}' > /dev/null; T1=$(date +%s.%N); "
           "echo GUEST_SEC=$(echo \"$T1 $T0\" | awk '{print $1-$2}')")


async def main():
    t0 = time.monotonic()
    sb = await AsyncSandbox.create(TEMPLATE, api_key=KEY, timeout=300)
    print(f"created in {time.monotonic()-t0:.1f}s id={sb.sandbox_id}", flush=True)
    # locate a big file for the io probe
    r = await sb.commands.run(
        "find / -xdev -type f -size +20M 2>/dev/null | head -3", user="root", timeout=60)
    cands = [l for l in (r.stdout or "").splitlines() if l.strip()]
    bigfile = cands[0] if cands else ""
    print(f"big file: {bigfile!r}", flush=True)
    probes = {"compute": COMPUTE, "memory_w": MEMORY_W, "memory_r": MEMORY_R}
    if bigfile:
        probes["iocold"] = IO_TMPL.replace("{f}", bigfile)
    results = {"label": LABEL, "sandbox": sb.sandbox_id, "ts": time.strftime("%H:%M:%S"),
               "bigfile": bigfile, "probes": {}}
    try:
        for name, cmd in probes.items():
            runs = []
            for i in range(ROUNDS):
                t1 = time.monotonic()
                r = await sb.commands.run(cmd, user="root", timeout=600)
                wall = time.monotonic() - t1
                runs.append({"wall": round(wall, 3), "exit": r.exit_code,
                             "stdout": (r.stdout or "").strip()[-120:]})
                print(f"  {name}[{i}] wall={wall:.2f}s exit={r.exit_code} "
                      f"out={(r.stdout or '')[:60]!r}", flush=True)
            results["probes"][name] = runs
    finally:
        try:
            await sb.kill()
            print("sandbox killed", flush=True)
        except Exception as e:
            print(f"kill failed: {e}", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2))
    print(f"-> {OUT}", flush=True)


asyncio.run(main())
