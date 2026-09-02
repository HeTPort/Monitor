#!/bin/sh

# POSIX workload supervisor. Full evidence stays append-only on device; only
# compact lifecycle/verdict events are passed to the native UART relay.
AGENT_VERSION=0.3.2
PROTOCOL_VERSION=2

if [ "${1:-}" = "--version" ]; then
    echo "avs-device-agent $AGENT_VERSION protocol $PROTOCOL_VERSION"
    exit 0
fi

test_id= attempt_id= target= uart= relay= spool_dir=
workload_cwd=/ baudrate=9600 timeout_s=300 max_frame=512 tail_guard=64 safe_utilization=70
summary_metrics= telemetry_agent= telemetry_plan= telemetry_interval_s=5 telemetry_shutdown_s=10

while [ "$#" -gt 0 ]; do
    case "$1" in
        --test-id) test_id=${2:-}; shift 2 ;;
        --attempt-id|--run-id) attempt_id=${2:-}; shift 2 ;;
        --target) target=${2:-}; shift 2 ;;
        --uart) uart=${2:-}; shift 2 ;;
        --relay) relay=${2:-}; shift 2 ;;
        --spool-dir) spool_dir=${2:-}; shift 2 ;;
        --cwd) workload_cwd=${2:-}; shift 2 ;;
        --baudrate) baudrate=${2:-}; shift 2 ;;
        --timeout) timeout_s=${2:-}; shift 2 ;;
        --max-frame) max_frame=${2:-}; shift 2 ;;
        --tail-guard) tail_guard=${2:-}; shift 2 ;;
        --safe-utilization) safe_utilization=${2:-}; shift 2 ;;
        --summary-metric) summary_metrics="$summary_metrics ${2:-}"; shift 2 ;;
        --telemetry-agent) telemetry_agent=${2:-}; shift 2 ;;
        --telemetry-plan) telemetry_plan=${2:-}; shift 2 ;;
        --telemetry-interval) telemetry_interval_s=${2:-}; shift 2 ;;
        --telemetry-shutdown-timeout) telemetry_shutdown_s=${2:-}; shift 2 ;;
        --) shift; break ;;
        *) echo "avs-device-agent: unsupported option: $1" >&2; exit 2 ;;
    esac
done

case "$test_id" in *[!A-Za-z0-9._:-]*|'') echo "avs-device-agent: invalid test id" >&2; exit 2 ;; esac
case "$attempt_id" in *[!A-Za-z0-9._:-]*|'') echo "avs-device-agent: invalid attempt id" >&2; exit 2 ;; esac
case "$target" in cpu|gpu) ;; *) echo "avs-device-agent: invalid target" >&2; exit 2 ;; esac
for numeric in "$baudrate" "$timeout_s" "$max_frame" "$tail_guard" "$safe_utilization" "$telemetry_interval_s" "$telemetry_shutdown_s"; do
    case "$numeric" in *[!0-9]*|'') echo "avs-device-agent: invalid numeric option" >&2; exit 2 ;; esac
done
for metric in $summary_metrics; do
    case "$metric" in *[!A-Za-z0-9_.-]*|'') echo "avs-device-agent: invalid summary metric" >&2; exit 2 ;; esac
done
if [ -z "$uart" ] || [ -z "$relay" ] || [ -z "$spool_dir" ] || [ "$#" -eq 0 ]; then
    echo "avs-device-agent: IDs, UART, relay, spool directory, and workload command are required" >&2
    exit 2
fi
[ -x "$relay" ] || { echo "avs-device-agent: relay missing/not executable: $relay" >&2; exit 3; }
command -v mkfifo >/dev/null 2>&1 || { echo "avs-device-agent: mkfifo is required" >&2; exit 3; }

mkdir -p "$spool_dir" || exit 3
events_file=$spool_dir/events.jsonl
workload_log=$spool_dir/workload.log
workload_stderr=$spool_dir/workload-stderr.log
diagnostics_log=$spool_dir/workload-diagnostics.log
timeout_flag=$spool_dir/workload.timeout
telemetry_stop=$spool_dir/telemetry.stop
final_file=$spool_dir/final.json
relay_fifo=$spool_dir/relay.fifo
workload_fifo=$spool_dir/workload.fifo
touch "$events_file" "$workload_log" "$workload_stderr" "$diagnostics_log" || exit 3
rm -f "$timeout_flag" "$telemetry_stop" "$relay_fifo" "$workload_fifo"
mkfifo "$relay_fifo" "$workload_fifo" || exit 3

"$relay" --uart "$uart" --baud "$baudrate" --max-frame "$max_frame" --tail-guard "$tail_guard" < "$relay_fifo" \
    >> "$spool_dir/relay.log" 2>&1 &
relay_pid=$!
exec 3> "$relay_fifo" || exit 3

local_seq=0 wire_seq=0 summary_seen=false
workload_pid= watchdog_pid= telemetry_pid= telemetry_exit= last_heartbeat_s=0
heartbeat_min_s=$((max_frame * 10 * 100 / (baudrate * safe_utilization)))
[ "$heartbeat_min_s" -gt 0 ] || heartbeat_min_s=1

now_s() { date +%s 2>/dev/null || echo 0; }
now_ms() { seconds=$(now_s); echo $((seconds * 1000)); }

emit_local() {
    local_seq=$((local_seq + 1))
    printf '{"schema_version":1,"test_id":"%s","run_id":"%s","seq":%s,"timestamp_ms":%s,"source":"%s","type":"%s","payload":%s}\n' \
        "$test_id" "$attempt_id" "$local_seq" "$(now_ms)" "$1" "$2" "$3" >> "$events_file"
}

emit_wire() {
    wire_seq=$((wire_seq + 1))
    printf '{"schema_version":1,"test_id":"%s","run_id":"%s","seq":%s,"timestamp_ms":%s,"source":"%s","type":"%s","payload":%s}\n' \
        "$test_id" "$attempt_id" "$wire_seq" "$(now_ms)" "$1" "$2" "$3" >&3
}

extract_string() {
    printf '%s\n' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}
extract_number() {
    printf '%s\n' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([-+0-9.eE][+0-9.eE-]*\).*/\1/p"
}

compact_summary() {
    line=$1
    result=$(extract_string "$line" result)
    exit_code=$(extract_number "$line" exit_code)
    verify_pass=$(printf '%s\n' "$line" | sed -n 's/.*"verify_pass"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p')
    [ -n "$result" ] || result=INVALID
    [ -n "$exit_code" ] || exit_code=-1
    payload="{\"result\":\"$result\",\"exit_code\":$exit_code"
    [ -n "$verify_pass" ] && payload="$payload,\"verify_pass\":$verify_pass"
    for metric in $summary_metrics; do
        metric_value=$(extract_number "$line" "$metric")
        [ -n "$metric_value" ] && payload="$payload,\"$metric\":$metric_value"
    done
    printf '%s}' "$payload"
}

handle_workload_line() {
    native_line=$1
    printf '%s\n' "$native_line" >> "$workload_log"
    case "$native_line" in
        DEBUG:*|TRACE:*|INFO:*) printf '%s\n' "$native_line" >> "$diagnostics_log"; return 0 ;;
    esac
    event_type=$(extract_string "$native_line" type)
    case "$event_type" in
        start|batch|verify|golden) emit_local "$target-workload" "$event_type" "$native_line" ;;
        heartbeat)
            emit_local "$target-workload" heartbeat "$native_line"
            current_s=$(now_s)
            if [ $((current_s - last_heartbeat_s)) -ge "$heartbeat_min_s" ]; then
                emit_wire "$target-workload" heartbeat '{}'
                last_heartbeat_s=$current_s
            fi
            ;;
        error|violation)
            emit_local "$target-workload" "$event_type" "$native_line"
            error_code=$(extract_string "$native_line" error_code)
            [ -n "$error_code" ] || error_code=WORKLOAD_ERROR
            emit_wire "$target-workload" "$event_type" "{\"origin\":\"workload\",\"error_code\":\"$error_code\"}"
            ;;
        summary)
            emit_local "$target-workload" summary "$native_line"
            emit_wire "$target-workload" summary "$(compact_summary "$native_line")"
            summary_seen=true
            ;;
        *)
            printf '%s\n' "$native_line" >> "$diagnostics_log"
            emit_local "$target-workload" error '{"origin":"workload","error_code":"WORKLOAD_OUTPUT_INVALID"}'
            emit_wire "$target-workload" error '{"origin":"workload","error_code":"WORKLOAD_OUTPUT_INVALID"}'
            ;;
    esac
}

stop_children() {
    [ -n "$watchdog_pid" ] && kill "$watchdog_pid" 2>/dev/null || true
    [ -n "$workload_pid" ] && kill "$workload_pid" 2>/dev/null || true
    if [ -n "$telemetry_pid" ]; then
        touch "$telemetry_stop" 2>/dev/null || true
        kill "$telemetry_pid" 2>/dev/null || true
    fi
}
trap 'stop_children' HUP INT TERM

start_payload="{\"agent_version\":\"$AGENT_VERSION\",\"protocol_version\":$PROTOCOL_VERSION,\"baudrate\":$baudrate}"
emit_local agent agent_start "$start_payload" || exit 3
emit_wire agent agent_start "$start_payload" || exit 3

if [ -n "$telemetry_agent" ] || [ -n "$telemetry_plan" ]; then
    if [ -z "$telemetry_agent" ] || [ -z "$telemetry_plan" ] || [ ! -r "$telemetry_agent" ] || [ ! -r "$telemetry_plan" ]; then
        emit_local agent error '{"origin":"agent","error_code":"TELEMETRY_NOT_DEPLOYED"}'
        emit_wire agent error '{"origin":"agent","error_code":"TELEMETRY_NOT_DEPLOYED"}'
    else
        sh "$telemetry_agent" --test-id "$test_id" --attempt-id "$attempt_id" --target "$target" \
            --output "$spool_dir/telemetry.jsonl" --plan "$telemetry_plan" \
            --interval "$telemetry_interval_s" --stop-file "$telemetry_stop" \
            >> "$spool_dir/telemetry-agent.log" 2>&1 &
        telemetry_pid=$!
    fi
fi

(
    cd "$workload_cwd" || exit 126
    "$@"
) > "$workload_fifo" 2>> "$workload_stderr" &
workload_pid=$!
(
    sleep "$timeout_s"
    if kill -0 "$workload_pid" 2>/dev/null; then
        echo 1 > "$timeout_flag"
        kill "$workload_pid" 2>/dev/null || true
    fi
) &
watchdog_pid=$!
while IFS= read -r native_line; do handle_workload_line "$native_line" || true; done < "$workload_fifo"
wait "$workload_pid"
workload_exit=$?
kill "$watchdog_pid" 2>/dev/null || true
watchdog_pid= workload_pid=

timed_out=false
[ -f "$timeout_flag" ] && timed_out=true
if [ "$timed_out" = true ]; then
    emit_local "$target-workload" error '{"origin":"workload","error_code":"WORKLOAD_DEADLINE_EXCEEDED"}'
    emit_wire "$target-workload" error '{"origin":"workload","error_code":"WORKLOAD_DEADLINE_EXCEEDED"}'
fi
telemetry_timed_out=false
if [ -n "$telemetry_pid" ]; then
    touch "$telemetry_stop" 2>/dev/null || true
    telemetry_deadline=$(( $(now_s) + telemetry_shutdown_s ))
    while kill -0 "$telemetry_pid" 2>/dev/null; do
        [ "$(now_s)" -ge "$telemetry_deadline" ] && break
        sleep 1
    done
    if kill -0 "$telemetry_pid" 2>/dev/null; then
        telemetry_timed_out=true
        kill "$telemetry_pid" 2>/dev/null || true
        sleep 1
        kill -9 "$telemetry_pid" 2>/dev/null || true
    fi
    wait "$telemetry_pid" 2>/dev/null
    telemetry_exit=$?
    telemetry_pid=
    if [ "$telemetry_timed_out" = true ]; then
        emit_local agent error '{"origin":"agent","error_code":"TELEMETRY_SHUTDOWN_TIMEOUT"}'
        emit_wire agent error '{"origin":"agent","error_code":"TELEMETRY_SHUTDOWN_TIMEOUT"}'
    elif [ "$telemetry_exit" -ne 0 ]; then
        emit_local agent error '{"origin":"agent","error_code":"TELEMETRY_COLLECTION_FAILED"}'
        emit_wire agent error '{"origin":"agent","error_code":"TELEMETRY_COLLECTION_FAILED"}'
    fi
fi

telemetry_json=null
[ -n "$telemetry_exit" ] && telemetry_json=$telemetry_exit
final_payload="{\"workload_exit_code\":$workload_exit,\"summary_seen\":$summary_seen,\"timed_out\":$timed_out,\"spool_complete\":true,\"telemetry_exit_code\":$telemetry_json,\"telemetry_timed_out\":$telemetry_timed_out}"
emit_local agent agent_final "$final_payload" || true
emit_wire agent agent_final "$final_payload" || true
printf '{"schema_version":1,"test_id":"%s","attempt_id":"%s","workload_exit_code":%s,"summary_seen":%s,"timed_out":%s,"telemetry_exit_code":%s,"telemetry_timed_out":%s}\n' \
    "$test_id" "$attempt_id" "$workload_exit" "$summary_seen" "$timed_out" "$telemetry_json" "$telemetry_timed_out" > "$final_file"

if command -v sha256sum >/dev/null 2>&1; then
    {
        printf '{"schema_version":1,"test_id":"%s","attempt_id":"%s","sha256":{' "$test_id" "$attempt_id"
        separator=
        for artifact in events.jsonl workload.log workload-stderr.log workload-diagnostics.log telemetry.jsonl telemetry-agent.log relay.log final.json; do
            [ -f "$spool_dir/$artifact" ] || continue
            set -- $(sha256sum "$spool_dir/$artifact" 2>/dev/null)
            digest=${1:-}
            [ -n "$digest" ] || continue
            printf '%s"%s":"%s"' "$separator" "$artifact" "$digest"
            separator=,
        done
        printf '}}\n'
    } > "$spool_dir/artifact-hashes.json" 2>/dev/null || true
fi

exec 3>&-
wait "$relay_pid"
relay_exit=$?
rm -f "$relay_fifo" "$workload_fifo"
[ "$relay_exit" -eq 0 ] || exit 3
[ -n "$telemetry_exit" ] && [ "$telemetry_exit" -ne 0 ] && exit 3
exit "$workload_exit"
