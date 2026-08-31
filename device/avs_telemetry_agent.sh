#!/bin/sh

# Independent, device-local, append-only telemetry collector.

TELEMETRY_VERSION=0.1.0

if [ "${1:-}" = "--version" ]; then
    echo "avs-telemetry-agent $TELEMETRY_VERSION schema 1"
    exit 0
fi

test_id=
attempt_id=
target=
output=
plan=
interval_s=5
duration_s=0
stop_file=

while [ "$#" -gt 0 ]; do
    case "$1" in
        --test-id) test_id=${2:-}; shift 2 ;;
        --attempt-id|--run-id) attempt_id=${2:-}; shift 2 ;;
        --target) target=${2:-}; shift 2 ;;
        --output) output=${2:-}; shift 2 ;;
        --plan) plan=${2:-}; shift 2 ;;
        --interval) interval_s=${2:-}; shift 2 ;;
        --duration) duration_s=${2:-}; shift 2 ;;
        --stop-file) stop_file=${2:-}; shift 2 ;;
        *) echo "avs-telemetry-agent: unsupported option: $1" >&2; exit 2 ;;
    esac
done

case "$test_id" in *[!A-Za-z0-9._:-]*|'') echo "avs-telemetry-agent: invalid test id" >&2; exit 2 ;; esac
case "$attempt_id" in *[!A-Za-z0-9._:-]*|'') echo "avs-telemetry-agent: invalid attempt id" >&2; exit 2 ;; esac
case "$target" in cpu|gpu) ;; *) echo "avs-telemetry-agent: invalid target" >&2; exit 2 ;; esac
case "$interval_s" in *[!0-9]*|'') echo "avs-telemetry-agent: invalid interval" >&2; exit 2 ;; esac
case "$duration_s" in *[!0-9]*|'') echo "avs-telemetry-agent: invalid duration" >&2; exit 2 ;; esac
[ "$interval_s" -gt 0 ] || interval_s=1
if [ -z "$output" ] || [ -z "$plan" ] || [ ! -r "$plan" ]; then
    echo "avs-telemetry-agent: readable plan and output are required" >&2
    exit 2
fi

mkdir -p "$(dirname "$output")" || exit 3
touch "$output" || exit 3
seq=0
started_s=$(date +%s 2>/dev/null || echo 0)

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

append_value() {
    metric=$1
    parser=$2
    source_path=$3
    [ -r "$source_path" ] || return 0
    raw=$(cat "$source_path" 2>/dev/null) || return 0
    case "$parser" in
        proc_stat_utilization)
            previous_file=$(dirname "$output")/proc-stat.previous
            current=$(awk '$1 == "cpu" { total=0; for (i=2; i<=NF; i++) total += $i; idle=$5; if (NF >= 6) idle += $6; print total, idle; exit }' "$source_path")
            [ -n "$current" ] || return 0
            if [ ! -s "$previous_file" ]; then
                printf '%s\n' "$current" > "$previous_file"
                return 0
            fi
            previous=$(cat "$previous_file" 2>/dev/null) || previous=
            printf '%s\n' "$current" > "$previous_file"
            [ -n "$previous" ] || return 0
            current_total=${current%% *}
            current_idle=${current#* }
            previous_total=${previous%% *}
            previous_idle=${previous#* }
            delta_total=$((current_total - previous_total))
            delta_idle=$((current_idle - previous_idle))
            [ "$delta_total" -gt 0 ] || return 0
            value=$(awk -v total="$delta_total" -v idle="$delta_idle" 'BEGIN { printf "%.3f",100*(1-idle/total) }')
            value_json=$value
            ;;
        int|number|float)
            value=$(printf '%s\n' "$raw" | awk 'match($0,/[-+]?[0-9]+([.][0-9]+)?/){print substr($0,RSTART,RLENGTH); exit}')
            [ -n "$value" ] || return 0
            value_json=$value
            ;;
        millidegree_celsius)
            value=$(printf '%s\n' "$raw" | awk 'match($0,/[-+]?[0-9]+([.][0-9]+)?/){v=substr($0,RSTART,RLENGTH)+0; printf "%.3f",v/1000; exit}')
            [ -n "$value" ] || return 0
            value_json=$value
            ;;
        temperature_auto)
            value=$(printf '%s\n' "$raw" | awk 'match($0,/[-+]?[0-9]+([.][0-9]+)?/){v=substr($0,RSTART,RLENGTH)+0; if(v>200 || v< -200) v=v/1000; printf "%.3f",v; exit}')
            [ -n "$value" ] || return 0
            value_json=$value
            ;;
        *)
            escaped=$(json_escape "$raw")
            value_json="\"$escaped\""
            ;;
    esac
    escaped_path=$(json_escape "$source_path")
    seq=$((seq + 1))
    printf '{"schema_version":1,"test_id":"%s","run_id":"%s","seq":%s,"timestamp_ms":%s,"source":"%s-telemetry","type":"telemetry","payload":{"metric":"%s","value":%s,"path":"%s"}}\n' \
        "$test_id" "$attempt_id" "$seq" "$(now_ms)" "$target" "$metric" "$value_json" "$escaped_path" >> "$output"
}

sample_once() {
    while IFS='|' read -r metric parser path_pattern; do
        metric=$(printf '%s' "$metric" | tr -d '\r')
        parser=$(printf '%s' "$parser" | tr -d '\r')
        path_pattern=$(printf '%s' "$path_pattern" | tr -d '\r')
        case "$metric" in ''|'#'*) continue ;; esac
        [ -n "$parser" ] && [ -n "$path_pattern" ] || continue
        for source_path in $path_pattern; do
            [ -e "$source_path" ] || continue
            append_value "$metric" "$parser" "$source_path"
        done
    done < "$plan"
}

while :; do
    [ -n "$stop_file" ] && [ -e "$stop_file" ] && break
    sample_once
    current_s=$(date +%s 2>/dev/null || echo 0)
    if [ "$duration_s" -gt 0 ] && [ $((current_s - started_s)) -ge "$duration_s" ]; then
        break
    fi
    [ "$duration_s" -eq 0 ] && [ -z "$stop_file" ] && break
    sleep "$interval_s"
done

exit 0
