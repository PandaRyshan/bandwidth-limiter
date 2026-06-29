#!/usr/bin/env bash
# tc_limit — Smart bandwidth limit daemon
# Monitors network traffic via a sliding window and proactively adjusts
# tc limits to avoid triggering cloud provider penalty policies.
#
# Usage:  tc_limit --on [OPTS]
#         tc_limit --off
#         tc_limit --status
#         tc_limit -h

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────
HIGHER_LIMIT=150        # Mbps — normal operating limit
LOWER_LIMIT=110         # Mbps — limit after sustained high usage
THRESHOLD=120           # Mbps — bandwidth alert line
WINDOW=19               # min  — sliding window size
INTERVAL=10             # sec  — sampling interval
COOLDOWN=5              # min  — cooldown after entering LIMITED
IFACE=""                # auto-detected unless overridden
DRY_RUN=false           # true → skip tc commands, log only
BURST_KBIT=16           # kbit — token bucket burst size

# ── Derived constants (set after parsing) ─────────────────────────────
BUF_SIZE=0
THRESHOLD_BPS=0         # bytes/sec
WINDOW_SEC=0
COOLDOWN_SEC=0

# ── Runtime globals ───────────────────────────────────────────────────
STATE="NORMAL"
COOLDOWN_START=0
IFB="ifb0"
STATE_FILE="/run/tc_limit.state"
PID_FILE="/run/tc_limit.pid"
CONFIG_FILE=""
DEFAULT_CONFIG="/etc/tc_limit/tc_limit.conf"
LOCK_FD=""
LOG_TAG="tc_limit"

declare -a RING_BUF=()
BUF_IDX=0
BUF_FILLED=0            # number of valid slots written so far
PREV_BYTES=0            # last raw counter reading
SAMPLE_N=0              # sample counter for periodic summary

# ── Helpers ───────────────────────────────────────────────────────────

LOG_LEVEL=1  # 0=ERROR 1=INFO 2=WARN 3=DEBUG

log_error() { (( LOG_LEVEL >= 0 )) && echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR $*" >&2; return 0; }
log_warn()  { (( LOG_LEVEL >= 2 )) && echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN  $*" >&2; return 0; }
log_info()  { (( LOG_LEVEL >= 1 )) && echo "[$(date '+%Y-%m-%d %H:%M:%S')] INFO  $*" >&2; return 0; }
log_debug() { (( LOG_LEVEL >= 3 )) && echo "[$(date '+%Y-%m-%d %H:%M:%S')] DEBUG $*" >&2; return 0; }

get_iface() {
    ip route get 1.1.1.1 2>/dev/null | awk '
        { for(i=1;i<=NF;i++) if($i=="dev") { print $(i+1); exit } }'
}

file_exists() { [[ -f "$1" ]]; }
proc_running() {
    [[ -d "/proc/$1" ]] && grep -qF "tc_limit" "/proc/$1/cmdline" 2>/dev/null
}

mbit_to_bytes_per_sec() { echo $(( $1 * 125000 )); }   # Mbps → B/s

# ── Lock ──────────────────────────────────────────────────────────────

acquire_lock() {
    exec {LOCK_FD}>"$PID_FILE"
    if ! flock -n "$LOCK_FD"; then
        log_error "Another instance is already running (lock on $PID_FILE)"
        exit 1
    fi
    echo $$ > "$PID_FILE"
}

release_lock() {
    [[ -n "$LOCK_FD" ]] && flock -u "$LOCK_FD" 2>/dev/null || true
    rm -f "$PID_FILE"
}

# ── Persistence ───────────────────────────────────────────────────────

save_state() {
    local window_avg="${1:-}"
    local tmp="${STATE_FILE}.tmp"
    {
        echo "STATE=$STATE"
        echo "RATE=$(current_rate)"
        echo "THRESHOLD=$THRESHOLD"
        echo "COOLDOWN_SEC=$COOLDOWN_SEC"
        [[ "$STATE" == "LIMITED" ]] && echo "COOLDOWN_START=$COOLDOWN_START"
        [[ -n "$window_avg" ]] && echo "WINDOW_AVG=$window_avg"
    } > "$tmp"
    mv "$tmp" "$STATE_FILE"
}

load_state() {
    if [[ -r "$STATE_FILE" ]]; then
        # shellcheck source=/dev/null
        source "$STATE_FILE" 2>/dev/null || true
        if [[ "${STATE:-}" == "LIMITED" && -n "${COOLDOWN_START:-}" ]]; then
            local now elapsed remain
            now=$(date +%s)
            elapsed=$(( now - COOLDOWN_START ))
            remain=$(( COOLDOWN_SEC - elapsed ))
            if (( remain > 0 )); then
                STATE="LIMITED"
                log_info "Resumed LIMITED state (${remain}s cooldown remaining)"
            else
                STATE="NORMAL"
                COOLDOWN_START=0
                log_info "Stored cooldown expired, starting NORMAL"
            fi
        else
            STATE="NORMAL"
            COOLDOWN_START=0
        fi
    fi
}

# ── Config ──────────────────────────────────────────────────────────────

load_config() {
    local config_path="${1:-}"
    [[ -z "$config_path" ]] && config_path="$DEFAULT_CONFIG"
    if [[ -r "$config_path" ]]; then
        log_info "Loading config from ${config_path}"
        # shellcheck source=/dev/null
        source "$config_path" 2>/dev/null || {
            log_error "Failed to load config file: ${config_path}"
            return 1
        }
    fi
    return 0
}

reload_config() {
    log_info "Reloading configuration…"
    local config_path="${CONFIG_FILE:-$DEFAULT_CONFIG}"
    if ! load_config "$config_path"; then
        log_warn "Config load failed, reload aborted"
        return 0
    fi

    # Re-validate
    local err=0
    for v in HIGHER_LIMIT LOWER_LIMIT THRESHOLD COOLDOWN; do
        if ! [[ "${!v}" =~ ^[0-9]+$ ]] || (( ${!v} <= 0 )); then
            log_warn "Reload: invalid ${v}=${!v}, keeping old value"
            err=1
        fi
    done
    (( err )) && return

    # Warn on non-hot-reloadable params
    if [[ -n "${WINDOW_OLD:-}" ]] && (( WINDOW != WINDOW_OLD )); then
        log_warn "WINDOW change requires restart (old=${WINDOW_OLD} new=${WINDOW})"
    fi
    if [[ -n "${INTERVAL_OLD:-}" ]] && (( INTERVAL != INTERVAL_OLD )); then
        log_warn "INTERVAL change requires restart (old=${INTERVAL_OLD} new=${INTERVAL})"
    fi

    # Apply hot-reloadable params
    if [[ "$STATE" == "NORMAL" ]]; then
        tc_change_rate "$HIGHER_LIMIT"
    fi
    save_state
    log_info "Reload complete: higher=${HIGHER_LIMIT}M lower=${LOWER_LIMIT}M threshold=${THRESHOLD}M cooldown=${COOLDOWN}m"
}

# ── tc management ─────────────────────────────────────────────────────

tc_init() {
    local rate="$1"
    log_info "Initialising tc: ${rate} Mbps on ${IFACE}"

    if $DRY_RUN; then
        log_info "[dry-run] Would set up tc: ${rate} Mbps egress+ingress via IFB"
        return
    fi

    # Clean slate
    tc qdisc del dev "$IFACE" root     2>/dev/null || true
    tc qdisc del dev "$IFACE" ingress  2>/dev/null || true
    tc qdisc del dev "$IFB"   root     2>/dev/null || true
    ip link set "$IFB" down 2>/dev/null || true

    modprobe ifb numifbs=1 2>/dev/null || true
    ip link add "$IFB" type ifb 2>/dev/null || true
    ip link set "$IFB" up 2>/dev/null || true

    # Egress HTB
    if ! tc qdisc add dev "$IFACE" root handle 1: htb default 10 2>/dev/null; then
        log_error "Cannot add HTB root qdisc on ${IFACE} (existing: $(tc qdisc show dev "$IFACE" 2>/dev/null | head -1))"
        exit 1
    fi
    tc class add dev "$IFACE" parent 1: classid 1:10 \
        htb rate "${rate}mbit" ceil "${rate}mbit" \
        burst "${BURST_KBIT}kbit" cburst "${BURST_KBIT}kbit" || {
        log_error "Cannot add HTB class on ${IFACE}"
        exit 1
    }

    # Ingress → IFB
    tc qdisc add dev "$IFACE" handle ffff: ingress 2>/dev/null || true
    tc filter add dev "$IFACE" parent ffff: protocol all \
        u32 match u32 0 0 action mirred egress redirect dev "$IFB" 2>/dev/null || {
        log_error "Cannot add ingress redirect to ${IFB}"
        exit 1
    }

    # IFB limit
    tc qdisc add dev "$IFB" root handle 2: htb default 20 2>/dev/null || {
        log_error "Cannot add HTB root qdisc on ${IFB}"
        exit 1
    }
    tc class add dev "$IFB" parent 2: classid 2:20 \
        htb rate "${rate}mbit" ceil "${rate}mbit" \
        burst "${BURST_KBIT}kbit" cburst "${BURST_KBIT}kbit" || {
        log_error "Cannot add HTB class on ${IFB}"
        exit 1
    }

    log_info "tc initialised: ${rate} Mbps (egress + ingress)"
}

tc_change_rate() {
    local rate="$1"
    if $DRY_RUN; then
        log_info "[dry-run] Would switch tc to ${rate} Mbps"
        return
    fi
    tc class change dev "$IFACE" parent 1: classid 1:10 \
        htb rate "${rate}mbit" ceil "${rate}mbit" \
        burst "${BURST_KBIT}kbit" cburst "${BURST_KBIT}kbit" 2>/dev/null || true

    tc class change dev "$IFB" parent 2: classid 2:20 \
        htb rate "${rate}mbit" ceil "${rate}mbit" \
        burst "${BURST_KBIT}kbit" cburst "${BURST_KBIT}kbit" 2>/dev/null || true
}

cleanup() {
    log_info "Cleaning up tc rules and IFB device…"
    tc qdisc del dev "$IFACE" root     2>/dev/null || true
    tc qdisc del dev "$IFACE" ingress  2>/dev/null || true
    tc qdisc del dev "$IFB"   root     2>/dev/null || true
    ip link set "$IFB" down 2>/dev/null || true
    release_lock
    rm -f "$STATE_FILE"
    log_info "Cleanup complete. Bandwidth limits removed."
}

# ── Signal Handlers ───────────────────────────────────────────────────

on_signal() { cleanup; exit 0; }

on_hup() {
    local WINDOW_OLD="$WINDOW" INTERVAL_OLD="$INTERVAL"
    reload_config
}

on_usr1() {
    local now rate avg_mbps line
    now=$(date +%s)
    rate=$(current_rate)
    line="[STATUS] State=$STATE  Rate=${rate}Mbps  Samples=$SAMPLE_N"
    if [[ "$STATE" == "LIMITED" ]]; then
        local remain=$(( COOLDOWN_SEC - (now - COOLDOWN_START) ))
        (( remain < 0 )) && { remain=0; :; }
        line+="  Cooldown=${remain}s"
    fi
    if (( BUF_FILLED > 0 )); then
        avg_mbps=$(ring_buf_avg_mbps)
        line+="  WindowAvg=$(printf "%.1f" "$avg_mbps")Mbps"
    fi
    log_info "$line"
}

# ── Help ──────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
tc_limit — Smart bandwidth limit daemon

Monitors network traffic via a sliding window and proactively adjusts
bandwidth limits to avoid triggering cloud provider penalty policies.

Usage:
  tc_limit --on [OPTIONS]    Start daemon (defaults: -H 150 -L 110 -T 120 -W 20 -I 10 -C 5)
  tc_limit --off             Stop daemon and remove all limits
  tc_limit --status          Show running status
  tc_limit --reload          Reload config without restart (requires SIGHUP-capable daemon)
  tc_limit -h                Show this help

Options:
  -H, --higher-limit RATE   Normal operating bandwidth (Mbps, default 150)
  -L, --lower-limit  RATE   Bandwidth after sustained high usage (Mbps, default 110)
  -T, --threshold     RATE  Alert threshold (Mbps, default 120)
  -W, --window        MINS  Sliding window size (minutes, default 19)
  -I, --interval      SECS  Sampling interval (seconds, default 10)
  -C, --cooldown      MINS  Cooldown period after entering LIMITED (minutes, default 5)
  -c, --config         PATH  Config file (default: /etc/tc_limit/tc_limit.conf)
  --iface              DEV  Network interface (auto-detected if omitted)
  --dry-run                  Monitor only, do not modify tc rules
  --log-file           PATH  Write logs to file instead of stderr
  -v, --verbose              Verbose debug logging
  -q, --quiet                Errors only

Config priority: CLI args > --config file > /etc/tc_limit/tc_limit.conf > built-in defaults

State machine:
  NORMAL  (tc = higher-limit)  — sliding window avg > threshold → LIMITED
  LIMITED (tc = lower-limit)   — cooldown expires → NORMAL

Hot-reload (SIGHUP / --reload): HIGHER_LIMIT, LOWER_LIMIT, THRESHOLD, COOLDOWN
Requires restart: WINDOW, INTERVAL

Examples:
  tc_limit --on                          # defaults
  tc_limit --on -H 200 -L 100 -T 150     # custom limits
  tc_limit --on -c /etc/custom.conf -v   # config + debug logging
  tc_limit --on --dry-run                # observe, don't enforce
  tc_limit --reload                      # hot-reload config
EOF
}

# ── Ring Buffer ───────────────────────────────────────────────────────

ring_buf_init() {
    local i
    for (( i = 0; i < BUF_SIZE; i++ )); do
        RING_BUF[i]=0
    done
    BUF_IDX=0
    BUF_FILLED=0
}

ring_buf_push() {
    local delta="$1"
    RING_BUF[BUF_IDX]=$delta
    BUF_IDX=$(( (BUF_IDX + 1) % BUF_SIZE ))
    (( BUF_FILLED < BUF_SIZE )) && (( BUF_FILLED += 1 ))
    return 0
}

ring_buf_sum() {
    local i sum=0
    for (( i = 0; i < BUF_SIZE; i++ )); do
        sum=$(( sum + RING_BUF[i] ))
    done
    echo $sum
}

ring_buf_clear() {
    ring_buf_init
}

ring_buf_is_full() { (( BUF_FILLED >= BUF_SIZE )); }

ring_buf_avg_mbps() {
    # Average bandwidth in Mbps over the window (or filled portion)
    local count sum avg_bps
    if (( BUF_FILLED == 0 )); then
        echo 0
        return
    fi
    count=$BUF_FILLED
    sum=$(ring_buf_sum)
    # avg over filled slots: bytes/sample / (interval seconds)
    avg_bps=$(( sum / (count * INTERVAL) ))
    # bps → Mbps with one decimal
    awk "BEGIN { printf \"%.1f\", $avg_bps / 125000 }"
}

# ── /sys counters ─────────────────────────────────────────────────────

read_bytes() {
    local tx rx
    tx=$(cat "/sys/class/net/${IFACE}/statistics/tx_bytes" 2>/dev/null) || return 1
    rx=$(cat "/sys/class/net/${IFACE}/statistics/rx_bytes" 2>/dev/null) || return 1
    echo $(( tx + rx ))
}

# ── State queries ─────────────────────────────────────────────────────

current_rate() {
    if [[ "$STATE" == "LIMITED" ]]; then
        echo "$LOWER_LIMIT"
    else
        echo "$HIGHER_LIMIT"
    fi
}

# ── Daemon loop ───────────────────────────────────────────────────────

daemon() {
    log_info "Daemon started: higher=${HIGHER_LIMIT}M lower=${LOWER_LIMIT}M threshold=${THRESHOLD}M window=${WINDOW}m interval=${INTERVAL}s cooldown=${COOLDOWN}m"
    log_info "Interface: ${IFACE}"

    # Initialise tc with current state's rate
    tc_init "$(current_rate)"

    # Seed the byte counter
    PREV_BYTES=$(read_bytes) || { log_error "Cannot read /sys counters for ${IFACE}"; exit 1; }

    local consecutive_fail=0

    while true; do
        sleep "$INTERVAL"

        # ── Sample ───────────────────────────────────────────────────
        local cur_bytes delta
        if ! cur_bytes=$(read_bytes); then
            consecutive_fail=$(( consecutive_fail + 1 ))
            log_warn "Failed to read /sys counters (${consecutive_fail}/3)"
            if (( consecutive_fail >= 3 )); then
                log_error "3 consecutive reads failed, exiting"
                cleanup
                exit 1
            fi
            continue
        fi
        consecutive_fail=0

        delta=$(( cur_bytes - PREV_BYTES ))
        PREV_BYTES=$cur_bytes

        # Guard: counter wrap (virtually impossible with 64-bit, but be safe)
        if (( delta < 0 )); then
            log_warn "Counter wrap detected, resetting buffer"
            ring_buf_clear
            PREV_BYTES=$cur_bytes
            continue
        fi

        # ── Push to ring buffer ──────────────────────────────────────
        ring_buf_push "$delta"
        SAMPLE_N=$(( SAMPLE_N + 1 ))

        log_debug "sample #${SAMPLE_N}: delta_bytes=${delta} window_sum=$(ring_buf_sum) threshold_bytes=$(( THRESHOLD_BPS * WINDOW_SEC ))"
        log_debug "state=${STATE} rate=$(current_rate)M buf_filled=${BUF_FILLED}/${BUF_SIZE}"

        # ── Decision ─────────────────────────────────────────────────
        local now
        now=$(date +%s)

        case "$STATE" in
            NORMAL)
                # Only evaluate once buffer is full
                if ring_buf_is_full; then
                    local window_sum threshold_bytes
                    window_sum=$(ring_buf_sum)
                    threshold_bytes=$(( THRESHOLD_BPS * WINDOW_SEC ))

                    if (( window_sum > threshold_bytes )); then
                        STATE="LIMITED"
                        COOLDOWN_START=$now
                        tc_change_rate "$LOWER_LIMIT"
                        local avg_mbps
                        avg_mbps=$(ring_buf_avg_mbps)
                        save_state "$avg_mbps"
                        log_info "→ LIMITED (window avg ${avg_mbps}Mbps > ${THRESHOLD}M threshold, cooldown ${COOLDOWN_SEC}s)"
                    fi
                fi
                ;;

            LIMITED)
                local elapsed=$(( now - COOLDOWN_START ))
                if (( elapsed >= COOLDOWN_SEC )); then
                    STATE="NORMAL"
                    COOLDOWN_START=0
                    ring_buf_clear
                    tc_change_rate "$HIGHER_LIMIT"
                    save_state
                    log_info "→ NORMAL (cooldown complete, rate restored to ${HIGHER_LIMIT}M)"
                fi
                ;;
        esac

        # ── Periodic summary (every 60s) ─────────────────────────────
        if (( SAMPLE_N > 0 && SAMPLE_N % (60 / INTERVAL) == 0 )); then
            local avg
            avg=$(ring_buf_avg_mbps)
            save_state "$avg"
            log_info "summary: state=${STATE} rate=$(current_rate)M window_avg=${avg}Mbps samples=${BUF_FILLED}/${BUF_SIZE}"
        fi
    done
}

# ── Status command (CLI) ──────────────────────────────────────────────

show_status() {
    if [[ -r "$STATE_FILE" ]]; then
        local state rate threshold cooldown cooldown_start window_avg pid
        # shellcheck source=/dev/null
        source "$STATE_FILE" 2>/dev/null || true
        state="${STATE:-UNKNOWN}"
        rate="${RATE:--}"
        threshold="${THRESHOLD:--}"
        cooldown="${COOLDOWN_SEC:-0}"
        cooldown_start="${COOLDOWN_START:-0}"
        window_avg="${WINDOW_AVG:--}"

        echo "Daemon: running"
        echo "State:    ${state}"
        echo "Rate:     ${rate} Mbps"
        echo "Threshold: ${threshold} Mbps"
        if [[ "$window_avg" != "-" && -n "$window_avg" ]]; then
            echo "Window:   ${window_avg} Mbps avg"
        fi

        if [[ "$state" == "LIMITED" && "$cooldown_start" -gt 0 ]]; then
            local now remain
            now=$(date +%s)
            remain=$(( cooldown - (now - cooldown_start) ))
            (( remain < 0 )) && { remain=0; :; }
            echo "Recover:  ${remain}s remaining"
        fi

        if [[ -r "$PID_FILE" ]]; then
            pid=$(<"$PID_FILE")
            if proc_running "$pid"; then
                echo "PID:      ${pid}"
            fi
        fi
    else
        echo "Daemon: not running"
    fi

    # Always show current tc status
    echo
    echo "── tc egress ──"
    tc -s class show dev "$IFACE" 2>/dev/null || echo "(no rules)"
    echo
    echo "── tc ingress (IFB) ──"
    tc -s class show dev "$IFB" 2>/dev/null || echo "(no rules)"
}

# ── Stop command (CLI) ────────────────────────────────────────────────

stop_daemon() {
    if [[ -r "$PID_FILE" ]]; then
        local pid
        pid=$(<"$PID_FILE")
        if proc_running "$pid"; then
            log_info "Sending SIGTERM to daemon (PID ${pid})…"
            kill "$pid" 2>/dev/null || true
            # Wait up to 5s for graceful exit
            local i
            for (( i = 0; i < 50; i++ )); do
                proc_running "$pid" || break
                sleep 0.1
            done
            proc_running "$pid" && log_warn "Daemon did not exit, forcing cleanup"
        fi
    fi
    # Always run cleanup regardless of daemon state
    cleanup
}

# ── Parameter Parsing ─────────────────────────────────────────────────

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --higher-limit|-H)
                HIGHER_LIMIT="$2"; shift 2 ;;
            --lower-limit|-L)
                LOWER_LIMIT="$2"; shift 2 ;;
            --threshold|-T)
                THRESHOLD="$2"; shift 2 ;;
            --window|-W)
                WINDOW="$2"; shift 2 ;;
            --interval|-I)
                INTERVAL="$2"; shift 2 ;;
            --cooldown|-C)
                COOLDOWN="$2"; shift 2 ;;
            --iface)
                IFACE="$2"; shift 2 ;;
            --dry-run)
                DRY_RUN=true; shift ;;
            --log-file)
                LOG_FILE="$2"; shift 2 ;;
            -v|--verbose)
                LOG_LEVEL=3; shift ;;
            -q|--quiet)
                LOG_LEVEL=0; shift ;;
            -c|--config)
                CONFIG_FILE="$2"; shift 2 ;;
            *)
                log_error "Unknown option: $1"
                usage
                exit 1
                ;;
        esac
    done
}

validate_args() {
    local err=0
    for v in HIGHER_LIMIT LOWER_LIMIT THRESHOLD WINDOW INTERVAL COOLDOWN; do
        if ! [[ "${!v}" =~ ^[0-9]+$ ]] || (( ${!v} <= 0 )); then
            log_error "${v}=${!v} is not a positive integer"
            err=1
        fi
    done

    if (( HIGHER_LIMIT <= THRESHOLD )); then
        log_error "higher-limit (${HIGHER_LIMIT}) must be > threshold (${THRESHOLD})"
        err=1
    fi
    if (( THRESHOLD <= LOWER_LIMIT )); then
        log_error "threshold (${THRESHOLD}) must be > lower-limit (${LOWER_LIMIT})"
        err=1
    fi
    if (( INTERVAL < 1 )); then
        log_error "interval must be >= 1"
        err=1
    fi

    if (( err )); then exit 1; fi
}

# ── Main ──────────────────────────────────────────────────────────────

main() {
    case "${1:-}" in
        --on)
            shift
            parse_args "$@"
            load_config "$CONFIG_FILE" || true
            validate_args

            # Derived constants
            BUF_SIZE=$(( WINDOW * 60 / INTERVAL ))
            THRESHOLD_BPS=$(mbit_to_bytes_per_sec "$THRESHOLD")
            WINDOW_SEC=$(( WINDOW * 60 ))
            COOLDOWN_SEC=$(( COOLDOWN * 60 ))

            # Interface
            [[ -z "$IFACE" ]] && IFACE=$(get_iface)
            if [[ -z "$IFACE" ]]; then
                log_error "Cannot determine network interface"
                exit 1
            fi

            # Lock & persistence
            acquire_lock
            load_state

            # Redirect logs if requested
            if [[ -n "${LOG_FILE:-}" ]]; then
                exec 2>>"$LOG_FILE"
            fi

            # Signals
            trap on_signal SIGTERM SIGINT
            trap on_usr1  SIGUSR1
            trap on_hup   SIGHUP

            # Run
            daemon
            ;;

        --off)
            stop_daemon
            ;;

        --status)
            [[ -z "$IFACE" ]] && IFACE=$(get_iface)
            show_status
            ;;

        --reload)
            if [[ -r "$PID_FILE" ]]; then
                local pid
                pid=$(<"$PID_FILE")
                if proc_running "$pid"; then
                    log_info "Sending SIGHUP to daemon (PID ${pid})…"
                    kill -HUP "$pid" && echo "Reload signal sent."
                else
                    echo "Daemon not running (stale PID file)" >&2
                    exit 1
                fi
            else
                echo "Daemon not running (no PID file)" >&2
                exit 1
            fi
            ;;

        -h|--help)
            usage
            ;;

        *)
            usage
            exit 1
            ;;
    esac
}

main "$@"
