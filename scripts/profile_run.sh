#!/bin/bash
# Orchestrates the 8x profiling replay run with collectors + early stop.
# Usage: setsid nohup bash profile_run.sh <outdir> <early_stop_sec> > <outdir>.orch.log 2>&1 &
# Safety: refuses non-empty outdir; memory watchdog stops the run if MemFree < threshold.
set -u
cd /home/cxd/agent-benchmark

OUTDIR_REL="$1"          # e.g. logs_mix_v3/profile_8x
EARLY_STOP_SEC="${2:-1800}"
MEMFREE_MIN_GB="${MEMFREE_MIN_GB:-15}"
COLLECT="$OUTDIR_REL.collect"
mkdir -p "$OUTDIR_REL.collect"

if [ -n "$(ls -A "$OUTDIR_REL" 2>/dev/null)" ] || [ -e "$OUTDIR_REL" ]; then
    echo "FATAL: $OUTDIR_REL already exists - refusing to overwrite" >&2
    exit 1
fi

export E2B_API_URL=http://127.0.0.1:8000
export E2B_API_KEY_FILE=/data/aenv/secrets/api-key
export AGENT_REPLAY_SANDBOX_METRICS=1

PY=/home/cxd/agent-benchmark/.venv/bin/python
[ -x "$PY" ] || PY=python3

echo "$(date -Is) starting collectors (hcollect + perf) -> $COLLECT"
setsid nohup python3 scripts/hcollect.py "$COLLECT" > "$COLLECT/hcollect.log" 2>&1 < /dev/null &
HCOLLECT_PID=$!
setsid nohup bash scripts/collect_metrics.sh 3600 "$COLLECT" > "$COLLECT/collect_metrics.log" 2>&1 < /dev/null &
PERF_PID=$!
echo "hcollect_pid=$HCOLLECT_PID perf_pid=$PERF_PID"

echo "$(date -Is) pre-replay idle baseline window (150s: full latency sweep + idle counters)"
sleep 150

BENCH_START=$(date +%s)
echo "$(date -Is) launching bench: 8x mult=2 -> $OUTDIR_REL"
setsid nohup "$PY" -m agent_replay.runner bench \
  --mode continuous \
  --base-count 384 \
  --trajectory-dir ./artifacts/traces/replays \
  --image-map ./artifacts/traces/image-map.json \
  --api-key-file /data/aenv/secrets/api-key \
  --ratios "${BENCH_RATIO:-8}" \
  --total-multiplier "${BENCH_MULT:-2}" \
  --command-timeout-continue \
  --command-timeout-sec 300 \
  --launch-interval-sec 0.02 \
  --output-dir "./$OUTDIR_REL" ${BENCH_EXTRA_ARGS:-} > "$OUTDIR_REL.bench.log" 2>&1 < /dev/null &
BENCH_PID=$!
echo "bench_pid=$BENCH_PID"

STOP_REASON=""
while kill -0 "$BENCH_PID" 2>/dev/null; do
    sleep 30
    ELAPSED=$(( $(date +%s) - BENCH_START ))
    MEMFREE_KB=$(awk '/^MemFree:/ {print $2}' /proc/meminfo)
    MEMFREE_GB=$(( MEMFREE_KB / 1024 / 1024 ))
    NFIRE=$(pgrep -c firecracker || true)
    echo "$(date -Is) watchdog t=${ELAPSED}s memfree=${MEMFREE_GB}GB fc=${NFIRE}"
    if [ "$MEMFREE_GB" -lt "$MEMFREE_MIN_GB" ]; then
        STOP_REASON="memfree_low(${MEMFREE_GB}GB)"
        break
    fi
    if [ "$ELAPSED" -ge "$EARLY_STOP_SEC" ]; then
        STOP_REASON="early_stop(${EARLY_STOP_SEC}s)"
        break
    fi
done

if [ -n "$STOP_REASON" ] && kill -0 "$BENCH_PID" 2>/dev/null; then
    echo "$(date -Is) stopping bench: $STOP_REASON (SIGINT)"
    kill -INT "$BENCH_PID"
fi

# wait for graceful quiesce (up to 20 min), then escalate
WAITED=0
while kill -0 "$BENCH_PID" 2>/dev/null && [ "$WAITED" -lt 1200 ]; do
    sleep 20; WAITED=$((WAITED+20))
    if [ $((WAITED % 300)) -eq 0 ]; then
        echo "$(date -Is) still quiescing after ${WAITED}s; fc=$(pgrep -c firecracker || true)"
    fi
done
if kill -0 "$BENCH_PID" 2>/dev/null; then
    echo "$(date -Is) WARN: bench did not exit in 1200s, SIGTERM"
    kill -TERM "$BENCH_PID" 2>/dev/null || true
    sleep 60
    kill -0 "$BENCH_PID" 2>/dev/null && { echo "SIGKILL fallback"; kill -KILL "$BENCH_PID"; }
fi

echo "$(date -Is) bench exited; collectors keep 120s to cover teardown"
sleep 120
kill -TERM "$HCOLLECT_PID" 2>/dev/null || true
kill -TERM "$PERF_PID" 2>/dev/null || true
sleep 15
kill -KILL "$HCOLLECT_PID" 2>/dev/null || true
kill -KILL "$PERF_PID" 2>/dev/null || true

dmesg | tail -50 > "$COLLECT/dmesg_tail.txt" 2>/dev/null || true
python3 - "$STOP_REASON" "$BENCH_PID" "$OUTDIR_REL" "$COLLECT" << 'EOF'
import json, sys, time
meta = {
    "stop_reason": sys.argv[1] or "bench_finished",
    "bench_pid": int(sys.argv[2]),
    "outdir": sys.argv[3],
    "collect_dir": sys.argv[4],
    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
}
open(sys.argv[4] + "/run-meta.json", "w").write(json.dumps(meta, indent=2))
EOF
echo "$(date -Is) profile_run done: reason=$STOP_REASON"
