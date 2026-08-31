#!/bin/sh

# Minimal POSIX-shell workload supervisor. It never changes platform state.
# The agent is the sole UART writer; complete evidence is appended locally.

AGENT_VERSION=0.2.0
PROTOCOL_VERSION=1

if [ "${1:-}" = "--version" ]; then
    echo "avs-device-agent $AGENT_VERSION protocol $PROTOCOL_VERSION"
    exit 0
fi

test_id=
attempt_id=
target=
uart=
spool_dir=
workload_cwd=/
baudrate=9600
timeout_s=300
telemetry_agent=
telemetry_plan=
telemetry_interval_s=5

while [ "$#" -gt 0 ]; do
    case "$1" in
        --test-id) test_id=${2:-}; shift 2 ;;
        --attempt-id|--run-id) attempt_id=${2:-}; shift 2 ;;
        --target) target=${2:-}; shift 2 ;;
        --uart) uart=${2:-}; shift 2 ;;
        --spool-dir) spool_dir=${2:-}; shift 2 ;;
        --cwd) workload_cwd=${2:-}; shift 2 ;;
        --baudrate) baudrate=${2:-}; shift 2 ;;
        --timeout) timeout_s=${2:-}; shift 2 ;;
        --telemetry-agent) telemetry_agent=${2:-}; shift 2 ;;
        --telemetry-plan) telemetry_plan=${2:-}; shift 2 ;;
        --telemetry-interval) telemetry_interval_s=${2:-}; shift 2 ;;
        --) shift; break ;;
        *) echo "avs-device-agent: unsupported option: $1" >&2; exit 2 ;;
    esac
done

case "$test_id" in *[!A-Za-z0-9._:-]*|'') echo "avs-device-agent: invalid test id" >&2; exit 2 ;; esac
case "$attempt_id" in *[!A-Za-z0-9._:-]*|'') echo "avs-device-agent: invalid attempt id" >&2; exit 2 ;; esac
case "$target" in cpu|gpu) ;; *) echo "avs-device-agent: invalid target" >&2; exit 2 ;; esac
case "$baudrate" in *[!0-9]*|'') echo "avs-device-agent: invalid baudrate" >&2; exit 2 ;; esac
case "$timeout_s" in *[!0-9]*|'') echo "avs-device-agent: invalid timeout" >&2; exit 2 ;; esac
case "$telemetry_interval_s" in *[!0-9]*|'') echo "avs-device-agent: invalid telemetry interval" >&2; exit 2 ;; esac
if [ -z "$uart" ] || [ -z "$spool_dir" ] || [ "$#" -eq 0 ]; then
    echo "avs-device-agent: test id, attempt id, uart, spool directory, and workload command are required" >&2
    exit 2
fi

mkdir -p "$spool_dir" || { echo "avs-device-agent: cannot create spool directory" >&2; exit 3; }
events_file=$spool_dir/events.jsonl
workload_log=$spool_dir/workload.log
timeout_flag=$spool_dir/workload.timeout
telemetry_stop=$spool_dir/telemetry.stop
final_file=$spool_dir/final.json
touch "$events_file" "$workload_log" || { echo "avs-device-agent: cannot create append logs" >&2; exit 3; }
rm -f "$timeout_flag" "$telemetry_stop"

seq=0
summary_seen=false
workload_pid=
watchdog_pid=
telemetry_pid=
telemetry_exit=

now_ms() {
    seconds=$(date +%s 2>/dev/null || echo 0)
    echo $((seconds * 1000))
}

json_escape() {
    printf '%s' "$1" | awk '
        BEGIN { ORS = ""; first = 1 }
        {
            gsub(/\\/, "\\\\")
            gsub(/"/, "\\\"")
            gsub(/\r/, "\\r")
            gsub(/\t/, "\\t")
            if (!first) printf "\\n"
            printf "%s", $0
            first = 0
        }
    '
}

emit_event() {
    event_source=$1
    event_type=$2
    event_payload=$3
    seq=$((seq + 1))
    event_line="{\"schema_version\":1,\"test_id\":\"$test_id\",\"run_id\":\"$attempt_id\",\"seq\":$seq,\"timestamp_ms\":$(now_ms),\"source\":\"$event_source\",\"type\":\"$event_type\",\"payload\":$event_payload}"
    printf '%s\n' "$event_line" >> "$events_file" || return 1
    if ! printf '%s\n' "$event_line" >> "$uart"; then
        echo "avs-device-agent: UART write failed: $uart" >&2
        return 1
    fi
}

handle_workload_line() {
    native_line=$1
    append_log=${2:-true}
    [ "$append_log" = true ] && printf '%s\n' "$native_line" >> "$workload_log"
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

stop_children() {
    [ -n "$watchdog_pid" ] && kill "$watchdog_pid" 2>/dev/null || true
    [ -n "$workload_pid" ] && kill "$workload_pid" 2>/dev/null || true
    if [ -n "$telemetry_pid" ]; then
        touch "$telemetry_stop" 2>/dev/null || true
        kill "$telemetry_pid" 2>/dev/null || true
    fi
}

trap 'stop_children' HUP INT TERM

emit_event agent agent_start "{\"agent_version\":\"$AGENT_VERSION\",\"protocol_version\":$PROTOCOL_VERSION,\"baudrate\":$baudrate}" || exit 3

if [ -n "$telemetry_agent" ] || [ -n "$telemetry_plan" ]; then
    if [ -z "$telemetry_agent" ] || [ -z "$telemetry_plan" ]; then
        emit_event agent error '{"origin":"agent","error_code":"TELEMETRY_CONFIGURATION_INCOMPLETE"}' || true
    elif [ ! -r "$telemetry_agent" ] || [ ! -r "$telemetry_plan" ]; then
        emit_event agent error '{"origin":"agent","error_code":"TELEMETRY_NOT_DEPLOYED"}' || true
    else
        sh "$telemetry_agent" \
            --test-id "$test_id" \
            --attempt-id "$attempt_id" \
            --target "$target" \
            --output "$spool_dir/telemetry.jsonl" \
            --plan "$telemetry_plan" \
            --interval "$telemetry_interval_s" \
            --stop-file "$telemetry_stop" \
            >> "$spool_dir/telemetry-agent.log" 2>&1 &
        telemetry_pid=$!
    fi
fi

workload_exit=3
timed_out=false
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
    while IFS= read -r native_line; do
        handle_workload_line "$native_line" || true
    done < "$workload_fifo"
    wait "$workload_pid"
    workload_exit=$?
    kill "$watchdog_pid" 2>/dev/null || true
    watchdog_pid=
    workload_pid=
    rm -f "$workload_fifo"
else
    (
        cd "$workload_cwd" || exit 126
        "$@"
    ) >> "$workload_log" 2>&1
    workload_exit=$?
    while IFS= read -r native_line; do handle_workload_line "$native_line" false || true; done < "$workload_log"
fi

[ -f "$timeout_flag" ] && timed_out=true
if [ -n "$telemetry_pid" ]; then
    touch "$telemetry_stop" 2>/dev/null || true
    wait "$telemetry_pid"
    telemetry_exit=$?
    telemetry_pid=
fi

telemetry_json=null
[ -n "$telemetry_exit" ] && telemetry_json=$telemetry_exit
emit_event agent agent_final "{\"workload_exit_code\":$workload_exit,\"summary_seen\":$summary_seen,\"timed_out\":$timed_out,\"spool_complete\":true,\"telemetry_exit_code\":$telemetry_json}" || true

printf '{"schema_version":1,"test_id":"%s","attempt_id":"%s","workload_exit_code":%s,"summary_seen":%s,"timed_out":%s,"telemetry_exit_code":%s}\n' \
    "$test_id" "$attempt_id" "$workload_exit" "$summary_seen" "$timed_out" "$telemetry_json" > "$final_file"

if command -v sha256sum >/dev/null 2>&1; then
    {
        printf '{"schema_version":1,"test_id":"%s","attempt_id":"%s","sha256":{' "$test_id" "$attempt_id"
        separator=
        for artifact in events.jsonl workload.log telemetry.jsonl telemetry-agent.log final.json; do
            [ -f "$spool_dir/$artifact" ] || continue
            digest=$(sha256sum "$spool_dir/$artifact" 2>/dev/null | awk '{print $1}') || digest=
            [ -n "$digest" ] || continue
            printf '%s"%s":"%s"' "$separator" "$artifact" "$digest"
            separator=,
        done
        printf '}}\n'
    } > "$spool_dir/artifact-hashes.json" 2>/dev/null || true
fi

exit "$workload_exit"
