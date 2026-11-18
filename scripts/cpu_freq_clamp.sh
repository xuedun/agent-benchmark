#!/bin/sh
set -eu

BACKUP_DIR=/tmp/freq-clamp-backup
MARKER=$BACKUP_DIR/CLAMPED
DEFAULT_FREQ=2600000

policies() {
    ls -d /sys/devices/system/cpu/cpufreq/policy* 2>/dev/null
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

read_field() {
    sed -n "${2}p" "$1" 2>/dev/null || true
}

cmd_status() {
    driver=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver 2>/dev/null || echo "n/a")
    echo "driver: $driver"
    gboost=$(cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null || echo "n/a")
    echo "global boost: $gboost (0 = disabled)"
    if [ -f "$MARKER" ]; then
        echo "state : CLAMPED to $(cat "$MARKER") kHz (backup: $BACKUP_DIR)"
    else
        echo "state : unclamped"
    fi
    printf '%-8s %-12s %-12s %-12s %-6s %-6s %s\n' policy governor cur max boost cpb epp
    for p in $(policies); do
        n=${p##*/}
        cur=$(cat "$p/scaling_cur_freq" 2>/dev/null || echo "-")
        gov=$(cat "$p/scaling_governor" 2>/dev/null || echo "-")
        mx=$(cat "$p/scaling_max_freq" 2>/dev/null || echo "-")
        bo=$(cat "$p/boost" 2>/dev/null || echo "-")
        cb=$(cat "$p/cpb" 2>/dev/null || echo "-")
        ep=$(cat "$p/energy_performance_preference" 2>/dev/null || echo "-")
        printf '%-8s %-12s %-12s %-12s %-6s %-6s %s\n' "$n" "$gov" "$cur" "$mx" "$bo" "$cb" "$ep"
    done
}

cmd_backup() {
    rm -rf "$BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
    # global boost switch (present on acpi-cpufreq; amd-pstate may lack it)
    if [ -f /sys/devices/system/cpu/cpufreq/boost ]; then
        cat /sys/devices/system/cpu/cpufreq/boost > "$BACKUP_DIR/GLOBAL_BOOST"
    fi
    # amd-pstate mode (active/passive/guided): we may switch to passive at
    # runtime when the cap is ignored; restore the original mode on restore.
    if [ -f /sys/devices/system/cpu/amd_pstate/status ]; then
        cat /sys/devices/system/cpu/amd_pstate/status > "$BACKUP_DIR/AMD_PSTATE_STATUS"
    fi
    for p in $(policies); do
        n=${p##*/}
        {
            cat "$p/scaling_governor"
            cat "$p/scaling_min_freq"
            cat "$p/scaling_max_freq"
            if [ -f "$p/boost" ]; then cat "$p/boost"; fi
            if [ -f "$p/cpb" ]; then cat "$p/cpb"; fi
            if [ -f "$p/energy_performance_preference" ]; then
                cat "$p/energy_performance_preference"
            fi
        } > "$BACKUP_DIR/$n"
    done
}

policy_nominal() {
    p=$1
    if [ -f "$p/nominal_freq" ]; then
        cat "$p/nominal_freq"
        return
    fi
    if [ -f /sys/devices/system/cpu/amd_pstate/nominal_freq ]; then
        cat /sys/devices/system/cpu/amd_pstate/nominal_freq
        return
    fi
    first_cpu=$(set -- $(cat "$p/related_cpus" 2>/dev/null); echo "${1:-}")
    if [ -n "$first_cpu" ] && [ -f "/sys/devices/system/cpu/cpu$first_cpu/acpi_cppc/nominal_perf" ]; then
        cat "/sys/devices/system/cpu/cpu$first_cpu/acpi_cppc/nominal_perf"
        return
    fi
    echo ""
}

normalize_khz() {
    v=${1:-}
    if [ -z "$v" ]; then echo ""; return; fi
    case $v in
        *[!0-9]*) echo ""; return ;;
    esac
    if [ "$v" -lt 1000 ]; then
        echo ""
    elif [ "$v" -lt 100000 ]; then
        echo $((v * 1000))
    else
        echo "$v"
    fi
}

cmd_limit() {
    freq=${1:-$DEFAULT_FREQ}
    case $freq in
        ''|*[!0-9]*) die "freq must be kHz, e.g. 2600000" ;;
    esac
    if [ -f "$MARKER" ]; then
        prev=$(cat "$MARKER")
        if [ "$prev" = "$freq" ]; then
            # Idempotent re-apply: a previous cell may have been interrupted
            # before restore. Re-enforce the same clamp WITHOUT re-backing-up,
            # otherwise the original unclamped state would be overwritten by the
            # already-clamped values and restore could never undo it.
            echo "already clamped to ${prev} kHz; re-enforcing"
            enforce_clamp "$freq" nobackup
            return 0
        fi
        # Different target: undo the previous clamp first (keeps the original
        # backup intact, since restore re-reads the same backup dir).
        echo "re-clamping: ${prev} kHz -> ${freq} kHz"
        cmd_restore || die "restore of previous clamp (${prev} kHz) failed"
    fi
    enforce_clamp "$freq"
}

verify_cap() {
    # $1 = cap in kHz. Returns: 0 OK, 1 could not verify, 2 over cap.
    # Sets global obs_mhz to the observed average frequency when measured.
    freq=$1
    obs_mhz=""
    if ! command -v turbostat >/dev/null 2>&1; then
        echo "NOTE: turbostat not installed; cannot verify real frequency (scaling_cur_freq is a request value on amd-pstate)."
        return 1
    fi
    echo "verifying actual frequency with turbostat (3s under light load)..."
    obs=$(turbostat --quiet --interval 1 --num_iterations 3 --show Bzy_MHz 2>/dev/null \
          | awk 'NR>1 && $1+0>0 {s+=$1; n++} END {if (n) printf "%d", s/n}')
    if [ -z "$obs" ]; then
        echo "  WARN: turbostat produced no Bzy_MHz samples; verify manually" >&2
        return 1
    fi
    obs_mhz=$obs
    limit_mhz=$((freq / 1000))
    if [ "$obs" -gt $((limit_mhz + limit_mhz / 20)) ]; then
        return 2
    fi
    echo "  OK: Bzy_MHz=${obs} within cap ${limit_mhz}"
    return 0
}

apply_policy_writes() {
    # $1 = cap in kHz. Sets global mismatch.
    freq=$1
    mismatch=0
    # Disable boost unconditionally on limit: a clamp is only meaningful if the CPU
    # cannot exceed the requested frequency. Leaving boost on lets the SMU
    # (amd-pstate/PBO on Zen4) run above scaling_max_freq, so the clamp silently
    # fails to fix the frequency.
    if [ -f /sys/devices/system/cpu/cpufreq/boost ]; then
        echo 0 > /sys/devices/system/cpu/cpufreq/boost 2>/dev/null \
            || echo "  WARN: global boost write refused" >&2
    fi
    for p in $(policies); do
        min=$(cat "$p/scaling_min_freq")
        if [ "$min" -gt "$freq" ]; then
            echo "$freq" > "$p/scaling_min_freq"
        fi
        echo "$freq" > "$p/scaling_max_freq"
        if [ -f "$p/boost" ]; then echo 0 > "$p/boost" 2>/dev/null || true; fi
        if [ -f "$p/cpb" ]; then echo 0 > "$p/cpb" 2>/dev/null || true; fi
        nominal=$(normalize_khz "$(policy_nominal "$p")")
        eff=$(cat "$p/scaling_max_freq")
        if [ "$eff" != "$freq" ]; then
            mismatch=$((mismatch + 1))
            if [ "$mismatch" -le 3 ]; then
                echo "  WARN policy ${p##*/}: effective max=${eff} kHz (requested ${freq}; nominal=${nominal:-unknown})" >&2
            fi
        fi
    done
}

enforce_clamp() {
    freq=$1
    do_backup=${2:-backup}
    # Compute BEFORE any output references it: the idempotent re-enforce path
    # (cmd_limit marker branch) reaches this function without passing through
    # the original assignment, which crashed the script with
    # "n_policies: unbound variable" under set -u (fixed 2026-11-18).
    n_policies=$(policies | wc -l)
    if [ "$n_policies" -eq 0 ]; then
        die "no cpufreq policies found (cpufreq disabled?)"
    fi
    if [ "$do_backup" = "backup" ]; then
        cmd_backup
    fi
    apply_policy_writes "$freq"
    echo "$freq" > "$MARKER"
    echo "clamped $n_policies policies to ${freq} kHz; boost DISABLED (governor untouched; backup: $BACKUP_DIR)"
    if [ "$mismatch" -gt 0 ]; then
        echo "NOTE: $mismatch policy(ies) did not land exactly on ${freq} kHz (driver snapped to an available P-state)."
    fi
    # amd-pstate (Zen4) active/guided mode treats scaling_max_freq as a request,
    # not a hard cap. Verify the real observed frequency; if the cap is ignored
    # and the mode is switchable, retry once under passive mode (where the
    # kernel enforces policy->max through schedutil).
    verify_cap "$freq"; vrc=$?
    if [ "$vrc" -eq 2 ]; then
        ap=/sys/devices/system/cpu/amd_pstate/status
        mode=""
        [ -f "$ap" ] && mode=$(cat "$ap" 2>/dev/null)
        if [ -n "$mode" ] && [ "$mode" != "passive" ]; then
            echo "  cap not honored under amd-pstate '${mode}'; switching to passive and re-applying..." >&2
            if echo passive > "$ap" 2>/dev/null && [ "$(cat "$ap" 2>/dev/null)" = "passive" ]; then
                apply_policy_writes "$freq"
                verify_cap "$freq"; vrc=$?
                if [ "$vrc" -eq 0 ]; then
                    echo "  OK: cap enforced after amd-pstate passive switch"
                fi
            else
                echo "  WARN: amd-pstate mode switch to passive refused" >&2
            fi
        fi
        if [ "$vrc" -eq 2 ]; then
            echo "  ERROR: observed Bzy_MHz=${obs_mhz} exceeds cap $((freq / 1000)) (+5% tolerance)." >&2
            echo "         The driver is ignoring scaling_max_freq (amd-pstate active mode)," >&2
            echo "         and switching to passive did not help (or was refused)." >&2
            echo "         Fix: boot with amd_pstate=disable (or amd_pstate=passive)," >&2
            echo "              or hard-lock P-state via wrmsr 0xC0010062." >&2
            echo "         State is left CLAMPED (boost off); run '$0 restore' to undo." >&2
            exit 2
        fi
    fi
}

cmd_restore() {
    if [ ! -f "$MARKER" ]; then
        die "not clamped"
    fi
    # restore global boost first so per-policy caps can be raised again
    if [ -f "$BACKUP_DIR/GLOBAL_BOOST" ] && [ -f /sys/devices/system/cpu/cpufreq/boost ]; then
        echo "$(cat "$BACKUP_DIR/GLOBAL_BOOST")" > /sys/devices/system/cpu/cpufreq/boost \
            2>/dev/null || echo "  WARN: global boost restore refused" >&2
    fi
    for p in $(policies); do
        n=${p##*/}
        f="$BACKUP_DIR/$n"
        if [ ! -f "$f" ]; then
            echo "WARN: no backup for policy $n, skipping" >&2
            continue
        fi
        gov=$(read_field "$f" 1)
        min=$(read_field "$f" 2)
        max=$(read_field "$f" 3)
        boost=$(read_field "$f" 4)
        cpb=$(read_field "$f" 5)
        epp=$(read_field "$f" 6)
        if [ -n "$max" ]; then echo "$max" > "$p/scaling_max_freq"; fi
        if [ -n "$min" ]; then echo "$min" > "$p/scaling_min_freq"; fi
        if [ -n "$gov" ]; then echo "$gov" > "$p/scaling_governor"; fi
        if [ -n "$boost" ] && [ -f "$p/boost" ]; then echo "$boost" > "$p/boost" 2>/dev/null || true; fi
        if [ -n "$cpb" ] && [ -f "$p/cpb" ]; then echo "$cpb" > "$p/cpb" 2>/dev/null || true; fi
        if [ -n "$epp" ] && [ -f "$p/energy_performance_preference" ]; then
            echo "$epp" > "$p/energy_performance_preference" 2>/dev/null || true
        fi
    done
    rm -f "$MARKER"
    # restore amd-pstate mode LAST (policy attrs may need the driver present)
    if [ -f "$BACKUP_DIR/AMD_PSTATE_STATUS" ] && [ -f /sys/devices/system/cpu/amd_pstate/status ]; then
        echo "$(cat "$BACKUP_DIR/AMD_PSTATE_STATUS")" > /sys/devices/system/cpu/amd_pstate/status \
            2>/dev/null || echo "  WARN: amd-pstate mode restore refused" >&2
    fi
    echo "restored all policies from $BACKUP_DIR"
}

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0 ...)"

case ${1:-} in
    limit)   shift; cmd_limit "$@" ;;
    restore) cmd_restore ;;
    status)  cmd_status ;;
    *) echo "usage: sudo $0 {limit [freq_kHz] | restore | status}"
       echo "  limit   clamp all policies (default ${DEFAULT_FREQ} kHz), DISABLE boost, backup originals"
       echo "          verifies real Bzy_MHz via turbostat; exits 2 if the cap is ignored (amd-pstate)"
       echo "  restore restore governor/min/max/boost/cpb/epp from backup"
       echo "  status  show per-policy state and global boost"
       exit 1 ;;
esac
