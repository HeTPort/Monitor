#!/bin/sh

# Fixed POSIX-shell device supervisor for targets without Python.
# Normal workload/UART data is written only to the UART and local spool.

AGENT_VERSION=0.1.1
PROTOCOL_VERSION=1

if [ "${1:-}" = "--version" ]; then
    echo "avs-device-agent $AGENT_VERSION protocol $PROTOCOL_VERSION"
    exit 0
fi

run_id=
target=
uart=
spool_dir=
workload_cwd=/
baudrate=9600
timeout_s=300
telemetry_interval_s=5
telemetry_specs=
environment_specs=
kernel_mode=off
kernel_rules=

append_spec() {
    if [ -z "$1" ]; then
        printf '%s' "$2"
    else
        printf '%s\n%s' "$1" "$2"
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --run-id) run_id=${2:-}; shift 2 ;;
        --target) target=${2:-}; shift 2 ;;
        --uart) uart=${2:-}; shift 2 ;;
        --spool-dir) spool_dir=${2:-}; shift 2 ;;
        --cwd) workload_cwd=${2:-}; shift 2 ;;
        --baudrate) baudrate=${2:-}; shift 2 ;;
        --timeout) timeout_s=${2:-}; shift 2 ;;
        --telemetry-interval) telemetry_interval_s=${2:-}; shift 2 ;;
        --telemetry)
            telemetry_specs=$(append_spec "$telemetry_specs" "${2:-}")
            shift 2
            ;;
        --environment)
            environment_specs=$(append_spec "$environment_specs" "${2:-}")
            shift 2
            ;;
        --kernel-mode) kernel_mode=${2:-off}; shift 2 ;;
        --kernel-rule)
            kernel_rule=$(printf '%s\t%s\t%s' "${2:-}" "${3:-}" "${4:-}")
            kernel_rules=$(append_spec "$kernel_rules" "$kernel_rule")
            shift 4
            ;;
        --) shift; break ;;
        *) echo "avs-device-agent: unsupported option: $1" >&2; exit 2 ;;
    esac
done

case "$run_id" in *[!A-Za-z0-9._:-]*|'') echo "avs-device-agent: invalid run id" >&2; exit 2 ;; esac
case "$target" in cpu|gpu) ;; *) echo "avs-device-agent: invalid target" >&2; exit 2 ;; esac
case "$baudrate" in *[!0-9]*|'') echo "avs-device-agent: invalid baudrate" >&2; exit 2 ;; esac
case "$timeout_s" in *[!0-9]*|'') echo "avs-device-agent: invalid timeout" >&2; exit 2 ;; esac
case "$telemetry_interval_s" in *[!0-9]*|'') telemetry_interval_s=5 ;; esac
if [ "$telemetry_interval_s" -lt 5 ]; then telemetry_interval_s=5; fi
if [ -z "$uart" ] || [ -z "$spool_dir" ] || [ "$#" -eq 0 ]; then
    echo "avs-device-agent: uart, spool directory, and workload command are required" >&2
    exit 2
fi

mkdir -p "$spool_dir" || { echo "avs-device-agent: cannot create spool directory" >&2; exit 3; }
events_file=$spool_dir/events.jsonl
restore_file=$spool_dir/environment.restore
timeout_flag=$spool_dir/workload.timeout
kernel_file=$spool_dir/kernel.raw
: > "$events_file"
: > "$restore_file"
rm -f "$timeout_flag"

seq=0
summary_seen=false
restoration_ok=true
workload_pid=
watchdog_pid=
kernel_pid=

now_ms() {
    seconds=$(date +%s 2>/dev/null || echo 0)
    echo $((seconds * 1000))
}

emit_event() {
    event_source=$1
    event_type=$2
    event_payload=$3
    seq=$((seq + 1))
    event_line="{\"schema_version\":1,\"run_id\":\"$run_id\",\"seq\":$seq,\"timestamp_ms\":$(now_ms),\"source\":\"$event_source\",\"type\":\"$event_type\",\"payload\":$event_payload}"
    printf '%s\n' "$event_line" >> "$events_file"
    printf '%s\n' "$event_line" >> "$uart"
}

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

restore_environment() {
    [ -s "$restore_file" ] || return 0
    while IFS='|' read -r state_path state_value; do
        [ -n "$state_path" ] || continue
        if ! printf '%s\n' "$state_value" > "$state_path" 2>/dev/null; then
            restoration_ok=false
            escaped_path=$(json_escape "$state_path")
            escaped_value=$(json_escape "$state_value")
            emit_event agent error "{\"origin\":\"restoration\",\"error_code\":\"ENVIRONMENT_RESTORE_FAILED\",\"phase\":\"restore\",\"path\":\"$escaped_path\",\"requested\":\"$escaped_value\"}"
        else
            escaped_path=$(json_escape "$state_path")
            escaped_value=$(json_escape "$state_value")
            emit_event agent environment "{\"phase\":\"restore\",\"path\":\"$escaped_path\",\"requested\":\"$escaped_value\",\"actual\":\"$escaped_value\",\"success\":true}"
        fi
    done < "$restore_file"
}

stop_children() {
    [ -n "$watchdog_pid" ] && kill "$watchdog_pid" 2>/dev/null || true
    [ -n "$kernel_pid" ] && kill "$kernel_pid" 2>/dev/null || true
    [ -n "$workload_pid" ] && kill "$workload_pid" 2>/dev/null || true
}

trap 'stop_children' HUP INT TERM

apply_environment() {
    [ -n "$environment_specs" ] || return 0
    while IFS='|' read -r state_path state_value state_required; do
        [ -n "$state_path" ] || continue
        required_json=false
        [ "$state_required" = "1" ] && required_json=true
        escaped_path=$(json_escape "$state_path")
        escaped_requested=$(json_escape "$state_value")
        old_value=$(cat "$state_path" 2>/dev/null) || old_value=
        printf '%s|%s\n' "$state_path" "$old_value" >> "$restore_file"
        if ! printf '%s\n' "$state_value" > "$state_path" 2>/dev/null; then
            emit_event agent error "{\"origin\":\"agent\",\"error_code\":\"ENVIRONMENT_APPLY_FAILED\",\"phase\":\"apply\",\"path\":\"$escaped_path\",\"requested\":\"$escaped_requested\",\"actual\":null,\"required\":$required_json}"
            [ "$state_required" = "1" ] && return 1
            continue
        fi
        readback=$(cat "$state_path" 2>/dev/null) || readback=
        escaped_actual=$(json_escape "$readback")
        if [ "$readback" != "$state_value" ]; then
            emit_event agent error "{\"origin\":\"agent\",\"error_code\":\"ENVIRONMENT_READBACK_FAILED\",\"phase\":\"readback\",\"path\":\"$escaped_path\",\"requested\":\"$escaped_requested\",\"actual\":\"$escaped_actual\",\"required\":$required_json}"
            [ "$state_required" = "1" ] && return 1
        else
            emit_event agent environment "{\"phase\":\"readback\",\"path\":\"$escaped_path\",\"requested\":\"$escaped_requested\",\"actual\":\"$escaped_actual\",\"required\":$required_json,\"success\":true}"
        fi
    done <<EOF
$environment_specs
EOF
    return 0
}

sample_proc_stat() {
    metric=$1
    source_path=$2
    current_file=$spool_dir/proc-stat.current
    previous_file=$spool_dir/proc-stat.previous
    awk '$1 ~ /^cpu[0-9]+$/ { total=0; for (i=2; i<=NF; i++) total += $i; idle=$5; if (NF >= 6) idle += $6; print $1, total, idle }' "$source_path" > "$current_file" 2>/dev/null || return 0
    if [ -s "$previous_file" ]; then
        while read -r cpu total idle; do
            previous=$(awk -v wanted="$cpu" '$1 == wanted { print $2, $3; exit }' "$previous_file")
            [ -n "$previous" ] || continue
            previous_total=${previous%% *}
            previous_idle=${previous#* }
            delta_total=$((total - previous_total))
            delta_idle=$((idle - previous_idle))
            [ "$delta_total" -gt 0 ] || continue
            value=$(awk -v total="$delta_total" -v idle="$delta_idle" 'BEGIN { value=100*(1-idle/total); if (value<0) value=0; if (value>100) value=100; printf "%.3f", value }')
            emit_event "$target-telemetry" telemetry "{\"metric\":\"$metric.$cpu\",\"value\":$value,\"path\":\"$source_path\"}"
        done < "$current_file"
    fi
    mv "$current_file" "$previous_file"
}

sample_telemetry() {
    [ -n "$telemetry_specs" ] || return 0
    while IFS='|' read -r metric parser source_path; do
        [ -n "$metric" ] && [ -r "$source_path" ] || continue
        if [ "$parser" = "proc_stat_utilization" ]; then
            sample_proc_stat "$metric" "$source_path"
            continue
        fi
        raw=$(cat "$source_path" 2>/dev/null) || continue
        case "$parser" in
            int|online|float|number)
                value=$(printf '%s\n' "$raw" | awk 'match($0,/[-+]?[0-9]+([.][0-9]+)?/){print substr($0,RSTART,RLENGTH); exit}')
                [ -n "$value" ] || continue
                payload="{\"metric\":\"$metric\",\"value\":$value,\"path\":\"$source_path\"}"
                ;;
            millidegree_celsius)
                value=$(printf '%s\n' "$raw" | awk 'match($0,/[-+]?[0-9]+([.][0-9]+)?/){v=substr($0,RSTART,RLENGTH)+0; v=v/1000; if(v>=-40 && v<=200) printf "%.3f",v; exit}')
                [ -n "$value" ] || continue
                payload="{\"metric\":\"$metric\",\"value\":$value,\"path\":\"$source_path\"}"
                ;;
            degree_celsius)
                value=$(printf '%s\n' "$raw" | awk 'match($0,/[-+]?[0-9]+([.][0-9]+)?/){v=substr($0,RSTART,RLENGTH)+0; if(v>=-40 && v<=200) printf "%.3f",v; exit}')
                [ -n "$value" ] || continue
                payload="{\"metric\":\"$metric\",\"value\":$value,\"path\":\"$source_path\"}"
                ;;
            temperature_auto)
                value=$(printf '%s\n' "$raw" | awk 'match($0,/[-+]?[0-9]+([.][0-9]+)?/){v=substr($0,RSTART,RLENGTH)+0; if(v>200 || v< -200) v=v/1000; if(v>=-40 && v<=200) printf "%.3f",v; exit}')
                [ -n "$value" ] || continue
                payload="{\"metric\":\"$metric\",\"value\":$value,\"path\":\"$source_path\"}"
                ;;
            *)
                escaped=$(json_escape "$raw")
                payload="{\"metric\":\"$metric\",\"value\":\"$escaped\",\"path\":\"$source_path\"}"
                ;;
        esac
        emit_event "$target-telemetry" telemetry "$payload"
    done <<EOF
$telemetry_specs
EOF
}

handle_workload_line() {
    native_line=$1
    event_type=$(printf '%s\n' "$native_line" | sed -n 's/.*"type"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
    case "$event_type" in
        start|heartbeat|batch|verify|golden|error|summary|violation)
            emit_event "$target-workload" "$event_type" "$native_line"
            [ "$event_type" = "summary" ] && summary_seen=true
            ;;
        *)
            diagnostic=$(printf '%s' "$native_line" | cut -c 1-256)
            escaped=$(json_escape "$diagnostic")
            emit_event "$target-workload" error "{\"origin\":\"workload\",\"error_code\":\"WORKLOAD_OUTPUT_INVALID\",\"line\":\"$escaped\"}"
            ;;
    esac
}

process_kernel_events() {
    [ -s "$kernel_file" ] && [ -n "$kernel_rules" ] || return 0
    match_file=$spool_dir/kernel.matches
    while IFS="$(printf '\t')" read -r severity rule_id pattern; do
        [ -n "$pattern" ] || continue
        grep -E "$pattern" "$kernel_file" 2>/dev/null | awk '!seen[$0]++' | head -n 10 > "$match_file"
        while IFS= read -r kernel_line; do
            escaped=$(json_escape "$kernel_line")
            emit_event kernel kernel "{\"severity\":\"$severity\",\"rule_id\":\"$rule_id\",\"message\":\"$escaped\"}"
        done < "$match_file"
    done <<EOF
$kernel_rules
EOF
    rm -f "$match_file"
    [ "$kernel_mode" = "full-local" ] || rm -f "$kernel_file"
}

emit_event agent agent_start "{\"agent_version\":\"$AGENT_VERSION\",\"protocol_version\":$PROTOCOL_VERSION,\"baudrate\":$baudrate}"

setup_ok=true
apply_environment || setup_ok=false

if [ "$kernel_mode" != "off" ] && command -v dmesg >/dev/null 2>&1; then
    dmesg -w > "$kernel_file" 2>/dev/null &
    kernel_pid=$!
fi

workload_exit=3
timed_out=false
if [ "$setup_ok" = true ]; then
    workload_fifo=$spool_dir/workload.fifo
    if command -v mkfifo >/dev/null 2>&1 && mkfifo "$workload_fifo" 2>/dev/null; then
        (
            cd "$workload_cwd" || exit 126
            "$@"
        ) > "$workload_fifo" 2>&1 &
        workload_pid=$!
        (
            sleep "$timeout_s"
            if kill -0 "$workload_pid" 2>/dev/null; then
                echo 1 > "$timeout_flag"
                kill "$workload_pid" 2>/dev/null || true
            fi
        ) &
        watchdog_pid=$!
        next_telemetry=0
        while IFS= read -r native_line; do
            current_s=$(date +%s 2>/dev/null || echo 0)
            if [ "$current_s" -ge "$next_telemetry" ]; then
                sample_telemetry
                next_telemetry=$((current_s + telemetry_interval_s))
            fi
            handle_workload_line "$native_line"
        done < "$workload_fifo"
        wait "$workload_pid"
        workload_exit=$?
        kill "$watchdog_pid" 2>/dev/null || true
        watchdog_pid=
        rm -f "$workload_fifo"
    else
        workload_log=$spool_dir/workload.log
        (
            cd "$workload_cwd" || exit 126
            "$@"
        ) > "$workload_log" 2>&1
        workload_exit=$?
        while IFS= read -r native_line; do handle_workload_line "$native_line"; done < "$workload_log"
        sample_telemetry
    fi
fi

[ -f "$timeout_flag" ] && timed_out=true
[ -n "$kernel_pid" ] && kill "$kernel_pid" 2>/dev/null || true
kernel_pid=
process_kernel_events
sample_telemetry
restore_environment

emit_event agent agent_final "{\"workload_exit_code\":$workload_exit,\"summary_seen\":$summary_seen,\"timed_out\":$timed_out,\"restoration_ok\":$restoration_ok,\"spool_complete\":true}"

if command -v sha256sum >/dev/null 2>&1; then
    events_sha256=$(sha256sum "$events_file" 2>/dev/null | awk '{print $1}') || events_sha256=
    if [ -n "$events_sha256" ]; then
        printf '{"schema_version":1,"sha256":{"events.jsonl":"%s"}}\n' "$events_sha256" > "$spool_dir/artifact-hashes.json" 2>/dev/null || true
    fi
fi

exit "$workload_exit"
