#!/usr/bin/env python3
"""Guest-side random-access latency chase: the missing probe.

Creates one sandbox, compiles a C pointer-chase benchmark inside it (gcc exists
in the image), then times: chase on 8MB (L3-sized) and 128MB (DRAM) working sets
plus a compute loop for reference. Host wall + guest internal time per probe.
Usage: .venv/bin/python probe_vm3.py <label> [rounds] -> /tmp/opencode/chase_<label>.json
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
OUT = Path("/tmp/opencode") / f"chase_{LABEL}.json"

CHASE_C = r"""#include <stdio.h>
#include <stdlib.h>
#include <time.h>
int main(int argc, char **argv) {
    size_t bytes = (size_t)atoll(argv[1]);
    long iters = atol(argv[2]);
    unsigned char *buf = malloc(bytes);
    size_t slots = bytes / 64;
    size_t *perm = malloc(slots * sizeof(size_t));
    for (size_t i = 0; i < slots; i++) perm[i] = i;
    srand(42);
    for (size_t i = slots - 1; i > 0; i--) {
        size_t j = rand() % (i + 1);
        size_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
    }
    void **slot = (void **)buf;
    for (size_t i = 0; i < slots; i++) {
        size_t a = perm[i], b = perm[(i + 1) % slots];
        *(void **)(buf + a * 64) = buf + b * 64;
    }
    volatile void *p = buf + perm[0] * 64;
    struct timespec a, b;
    clock_gettime(CLOCK_MONOTONIC, &a);
    for (long k = 0; k < iters; k++) p = *(void *volatile *)p;
    clock_gettime(CLOCK_MONOTONIC, &b);
    double s = (b.tv_sec - a.tv_sec) + (b.tv_nsec - a.tv_nsec) / 1e9;
    printf("%.1f\n", s / iters * 1e9);
    return 0;
}
"""

SETUP = ("mkdir -p /tmp/chase && cat > /tmp/chase/chase.c << 'CEOFEOF'\n" + CHASE_C +
         "\nCEOFEOF\ngcc -O1 -o /tmp/chase/chase /tmp/chase/chase.c && echo BUILD_OK")
CH8 = "/tmp/chase/chase 8388608 20000000"
CH128 = "/tmp/chase/chase 134217728 20000000"
COMPUTE = "awk 'BEGIN{x=0;for(i=0;i<20000000;i++)x=(x+i)%4294967296;print x}'"


async def timed(sb, name, cmd, results):
    runs = []
    for i in range(ROUNDS):
        t0 = time.monotonic()
        r = await sb.commands.run(cmd, user="root", timeout=600)
        wall = time.monotonic() - t0
        runs.append({"wall": round(wall, 3), "exit": r.exit_code,
                     "stdout": (r.stdout or "").strip()[-60:]})
        print(f"  {name}[{i}] wall={wall:.2f}s exit={r.exit_code} out={(r.stdout or '')[:40]!r}",
              flush=True)
    results[name] = runs


async def main():
    sb = await AsyncSandbox.create(TEMPLATE, api_key=KEY, timeout=300)
    print(f"sandbox={sb.sandbox_id}", flush=True)
    r = await sb.commands.run(SETUP, user="root", timeout=300)
    print(f"setup: exit={r.exit_code} {(r.stdout or '')[-40:]!r}", flush=True)
    results = {"label": LABEL, "sandbox": sb.sandbox_id, "ts": time.strftime("%H:%M:%S"),
               "probes": {}}
    try:
        await timed(sb, "compute", COMPUTE, results["probes"])
        await timed(sb, "chase8M", CH8, results["probes"])
        await timed(sb, "chase128M", CH128, results["probes"])
    finally:
        try:
            await sb.kill()
        except Exception:
            pass
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2))
    print(f"-> {OUT}", flush=True)


asyncio.run(main())
