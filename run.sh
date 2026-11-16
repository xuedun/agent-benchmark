#!/bin/bash
set -u

export E2B_API_URL=http://127.0.0.1:8000
export E2B_API_KEY_FILE=/data/aenv/secrets/api-key
source .venv/bin/activate

RATIOS=(8 1)
MULTIPLIERS=(1 2)
CELLS=(
    "16way 0xFFFF 4.2fre restore"
    "11way 0xFFE0 4.2fre restore"
    "11way 0xFFE0 2.7fre 2700000"
    "16way 0xFFFF 2.7fre 2700000"
)
VERSION=v1
BASE_COUNT=384
TRAJ_DIR=./artifacts/traces/replays
IMAGE_MAP=./artifacts/traces/image-map.json
API_KEY_FILE=/data/aenv/secrets/api-key
COMMAND_TIMEOUT_SEC=300
LAUNCH_INTERVAL=0.02
RUN_LOG=run_all.log
DRY_RUN=${DRY_RUN:-0}
COLLECT_MPKI=${COLLECT_MPKI:-1}
COLLECT_CAP_SEC=43200
COLLECT_PRE_WARM_SEC=60

log() { echo "[$(date '+%F %T')] $*" | tee -a "$RUN_LOG"; }

apply_cell() {
    local way=$1 mask=$2 fre=$3 freq_arg=$4
    pqos -e "llc:0=$MASK" || log "WARN: pqos mask $MASK failed"
    if [[ "$freq_arg" == "restore" ]]; then
        bash scripts/cpu_freq_clamp.sh restore || true
    else
        bash scripts/cpu_freq_clamp.sh limit "$freq_arg" || true
    fi
    local cur
    cur=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq 2>/dev/null)
    log "cell=${way}/${fre} mask=${MASK} cur_max_freq=${cur}"
}

for MULT in "${MULTIPLIERS[@]}"; do
  for RATIO in "${RATIOS[@]}"; do
    for CELL in "${CELLS[@]}"; do
      read -r WAY MASK FRE FREQ_ARG <<<"$CELL"
      OUT="./${RATIO}_${MULT}/${VERSION}/384c_${WAY}_${FRE}"
      log "START ratio=$RATIO mult=$MULT cell=${WAY}_${FRE} -> $OUT"
      apply_cell "$WAY" "$MASK" "$FRE" "$FREQ_ARG"
      COLLECT_PID=""
      if [[ "$COLLECT_MPKI" == "1" && "$DRY_RUN" != "1" ]]; then
          mkdir -p "$OUT"
          setsid bash scripts/collect_metrics.sh "$COLLECT_CAP_SEC" "$OUT/_collect" \
              > "$OUT/_collect.launch.log" 2>&1 < /dev/null &
          COLLECT_PID=$!
          log "collector pid=$COLLECT_PID -> $OUT/_collect (pre-warm ${COLLECT_PRE_WARM_SEC}s)"
          sleep "$COLLECT_PRE_WARM_SEC"
      fi
      if [[ "$DRY_RUN" == "1" ]]; then
          log "DRY: bench ratio=$RATIO mult=$MULT -> $OUT (freq=${FREQ_ARG} collect=${COLLECT_MPKI})"
          continue
      fi
      python -m agent_replay.runner bench \
        --mode continuous \
        --base-count "$BASE_COUNT" \
        --trajectory-dir "$TRAJ_DIR" \
        --image-map "$IMAGE_MAP" \
        --api-key-file "$API_KEY_FILE" \
        --ratios "$RATIO" \
        --total-multiplier "$MULT" \
        --command-timeout-continue \
        --command-timeout-sec "$COMMAND_TIMEOUT_SEC" \
        --launch-interval-sec "$LAUNCH_INTERVAL" \
        --output-dir "$OUT"
      log "END ratio=$RATIO mult=$MULT cell=${WAY}_${FRE} exit=$?"
      if [[ -n "$COLLECT_PID" ]]; then
          kill -TERM "$COLLECT_PID" 2>/dev/null
          wait "$COLLECT_PID" 2>/dev/null
          local_summary=$(grep -m1 -E 'mean_mpki|mean_ipc' "$OUT/_collect/summary.txt" 2>/dev/null || echo "summary未生成")
          log "collector stopped: $local_summary"
      fi
    done
  done
done

pqos -e 'llc:0=0xFFFF' || true
bash scripts/cpu_freq_clamp.sh restore || true
log "ALL DONE, mask=full freq=restored"
