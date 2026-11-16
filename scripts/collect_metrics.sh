#!/bin/bash
# collect_metrics.sh — replay side-car: IPC + L3 MPKI + memory latency in one run
# Usage:  nohup ./collect_metrics.sh <duration_sec> <out_dir> &    # start before replay
# Outputs (in <out_dir>):
#   counters.csv        per-second cycles,instructions(+miss events), one time axis
#   latency_sweep.csv   pre-replay cache-hierarchy profile (32K..1G ladder, idle)
#   latency_probe.csv   during-replay loaded latency (8M/256M every 30s)
#   psi.csv             per-second PSI (cpu/memory/io some)
#   summary.txt         start epoch, event set, sanity checks, mean IPC/MPKI
set -u
DUR=${1:?usage: collect_metrics.sh <duration_sec> <out_dir>}
OUT=${2:?usage: collect_metrics.sh <duration_sec> <out_dir>}
mkdir -p "$OUT"
LOG="$OUT/collect.log"
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

ARCH=$(uname -m)
HOSTN=$(hostname -s)
START_EPOCH=$(date +%s)

if [[ "$(id -u)" -ne 0 ]]; then
    log "FATAL: needs root for system-wide perf, run with sudo"
    exit 1
fi

probe_events() { perf stat -a -e "$1" -x, -- sleep 0.3 >/dev/null 2>&1; }

if probe_events "cycles,instructions"; then
    BASE="cycles,instructions"
else
    log "FATAL: base perf events unavailable"
    exit 1
fi
MISS=""; MISS_KIND="none"
if [[ "$ARCH" == "x86_64" ]] && probe_events "ls_dmnd_fills_from_sys.dram_io_near,ls_dmnd_fills_from_sys.dram_io_far"; then
    MISS="ls_dmnd_fills_from_sys.dram_io_near,ls_dmnd_fills_from_sys.dram_io_far"
    MISS_KIND="dram_fill"
elif probe_events "LLC-loads,LLC-load-misses"; then
    MISS="LLC-loads,LLC-load-misses"
    MISS_KIND="llc"
else
    log "WARN: no miss event set available, IPC only"
fi
EVENTS="$BASE${MISS:+,$MISS}"
log "host=$HOSTN arch=$ARCH miss=$MISS_KIND events=$EVENTS dur=${DUR}s"

PROBE_SRC="$OUT/lat_probe.c"
cat > "$PROBE_SRC" <<'CEOF'
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}
int main(int argc, char **argv) {
    size_t ws = strtoull(argv[1], 0, 10);
    long iters = argc > 2 ? strtol(argv[2], 0, 10) : 1000000;
    size_t n = ws / sizeof(size_t);
    if (n < 4) n = 4;
    size_t *perm = malloc(n * sizeof(size_t));
    for (size_t i = 0; i < n; i++) perm[i] = i;
    unsigned s = 42;
    for (size_t i = n - 1; i > 0; i--) {
        size_t j = rand_r(&s) % (i + 1);
        size_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
    }
    size_t *chain = malloc(n * sizeof(size_t));
    for (size_t k = 0; k < n; k++) chain[perm[k]] = perm[(k + 1) % n];
    free(perm);
    size_t idx = 0;
    for (size_t k = 0; k < n; k++) idx = chain[idx];
    if (idx == (size_t)-1) putchar('x');
    idx = 0;
    double t0 = now_s();
    for (long k = 0; k < iters; k++) idx = chain[idx];
    double dt = now_s() - t0;
    if (idx == (size_t)-1) putchar('x');
    printf("%.2f\n", dt / iters * 1e9);
    return 0;
}
CEOF
PROBE_BIN="$OUT/lat_probe"
if cc -O2 -o "$PROBE_BIN" "$PROBE_SRC" 2>>"$LOG"; then
    log "latency probe compiled"
else
    log "WARN: no C compiler, latency collection skipped"
    PROBE_BIN=""
fi
PROBE_CPU=0

if [[ -n "$PROBE_BIN" ]]; then
    log "pre-replay latency sweep (idle baseline)"
    : > "$OUT/latency_sweep.csv"
    echo "ws_bytes,iters,ns_per_access" >> "$OUT/latency_sweep.csv"
    for ws in 32768 262144 2097152 8388608 33554432 67108864 134217728 268435456 536870912 1073741824; do
        n=$((ws / 8))
        iters=$((n * 8)); (( iters > 20000000 )) && iters=20000000; (( iters < 500000 )) && iters=500000
        ns=$(taskset -c "$PROBE_CPU" "$PROBE_BIN" "$ws" "$iters" 2>/dev/null | tail -1)
        echo "$ws,$iters,$ns" >> "$OUT/latency_sweep.csv"
        log "  ws=$((ws/1024))K ns=$ns"
    done
    log "sweep done"
fi

log "starting main collection for ${DUR}s"
perf stat -a -e "$EVENTS" -I 1000 -x, -o "$OUT/counters.csv" -- sleep "$DUR" &
PERF_PID=$!

(
    if [[ -n "$PROBE_BIN" ]]; then
        : > "$OUT/latency_probe.csv"
        echo "epoch,ws_bytes,ns_per_access" >> "$OUT/latency_probe.csv"
    fi
    : > "$OUT/psi.csv"
    echo "epoch,cpu_some,mem_some,io_some" >> "$OUT/psi.csv"
    PSI_OK=1
    [[ -r /proc/pressure/cpu ]] || PSI_OK=0
    [[ $PSI_OK -eq 0 ]] && echo "$(date '+%F %T') WARN: PSI unavailable on this kernel" >> "$LOG"
    END=$((SECONDS + DUR))
    N=0
    while (( SECONDS < END )); do
        E=$(date +%s)
        C=""; M=""; I=""
        if [[ $PSI_OK -eq 1 ]]; then
            C=$(sed -n 's/^some avg10=\([0-9.]*\).*/\1/p' /proc/pressure/cpu)
            M=$(sed -n 's/^some avg10=\([0-9.]*\).*/\1/p' /proc/pressure/memory)
            I=$(sed -n 's/^some avg10=\([0-9.]*\).*/\1/p' /proc/pressure/io)
        fi
        echo "$E,$C,$M,$I" >> "$OUT/psi.csv"
        N=$((N + 1))
        if [[ -n "$PROBE_BIN" && $(( N % 30 )) -eq 0 ]]; then
            for ws in 8388608 268435456; do
                ns=$(taskset -c "$PROBE_CPU" "$PROBE_BIN" "$ws" 1000000 2>/dev/null | tail -1)
                echo "$E,$ws,$ns" >> "$OUT/latency_probe.csv"
            done
        fi
        sleep 1
    done
) &
SIDE_PID=$!

wait "$PERF_PID" 2>/dev/null
kill "$SIDE_PID" 2>/dev/null
wait "$SIDE_PID" 2>/dev/null

{
    echo "host=$HOSTN arch=$ARCH"
    echo "start_epoch=$START_EPOCH"
    echo "duration_sec=$DUR"
    echo "miss_kind=$MISS_KIND"
    echo "events=$EVENTS"
    if [[ -f "$OUT/counters.csv" ]]; then
        python3 - "$OUT/counters.csv" <<'PYEOF'
import sys

EVENTS = {
    "cycles", "instructions",
    "LLC-loads", "LLC-load-misses",
    "ls_dmnd_fills_from_sys.dram_io_near",
    "ls_dmnd_fills_from_sys.dram_io_far",
}


def parse_line(line):
    parts = [p.strip() for p in line.strip().split(",")]
    evt = next((p for p in parts if p in EVENTS), None)
    if evt is None:
        return None, None, None
    evt_idx = parts.index(evt)
    ts = None
    try:
        ts = float(parts[0])
    except ValueError:
        pass
    val = None
    for p in reversed(parts[:evt_idx]):
        try:
            val = float(p)
            break
        except ValueError:
            continue
    return ts, evt, val


rows = {}
try:
    for line in open(sys.argv[1]):
        if line.startswith("#") or not line.strip():
            continue
        ts, evt, val = parse_line(line)
        if evt is None or val is None:
            continue
        key = ts if ts is not None else 0.0
        rows.setdefault(key, {})[evt] = val
except Exception:
    pass
if rows:
    ipcs, mpkis, cyc_s = [], [], []
    for t, ev in sorted(rows.items()):
        if ev.get("cycles", 0) > 0 and ev.get("instructions") is not None:
            ipcs.append(ev["instructions"] / ev["cycles"])
            m = (ev.get("ls_dmnd_fills_from_sys.dram_io_near") or 0) \
                + (ev.get("ls_dmnd_fills_from_sys.dram_io_far") or 0)
            if m <= 0:
                m = ev.get("LLC-load-misses") or 0
            if m > 0:
                mpkis.append(m / ev["instructions"] * 1000)
            cyc_s.append(ev["cycles"])
    if ipcs:
        print(f"mean_ipc={sum(ipcs)/len(ipcs):.3f} (n={len(ipcs)} intervals)")
        print(f"mean_cycles_per_s={sum(cyc_s)/max(1,len(cyc_s))/1e9:.1f}G"
              "  (sanity: near-idle -> guest exec may not be counted)")
    if mpkis:
        print(f"mean_mpki={sum(mpkis)/len(mpkis):.2f}")
PYEOF
    fi
    echo "--- latency_sweep (idle, ns per access):"
    [[ -f "$OUT/latency_sweep.csv" ]] && tail -n +2 "$OUT/latency_sweep.csv" | awk -F, '{printf "  %8dK  %s\n", $1/1024, $3}'
    echo "--- latency_probe samples: $( [[ -f $OUT/latency_probe.csv ]] && wc -l < $OUT/latency_probe.csv || echo 0 )"
} > "$OUT/summary.txt" 2>&1
log "done -> $OUT/summary.txt"
