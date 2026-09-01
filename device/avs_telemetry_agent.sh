#!/bin/sh

# Independent append-only collector. Uses POSIX shell plus sed only; notably no
# awk or tr, which are absent on the tested HarmonyOS image.
TELEMETRY_VERSION=0.2.0

if [ "${1:-}" = "--version" ]; then
    echo "avs-telemetry-agent $TELEMETRY_VERSION schema 1"
    exit 0
fi

test_id= attempt_id= target= output= plan= interval_s=5 duration_s=0 stop_file=
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

case "$test_id" in *[!A-Za-z0-9._:-]*|'') echo "invalid test id" >&2; exit 2 ;; esac
case "$attempt_id" in *[!A-Za-z0-9._:-]*|'') echo "invalid attempt id" >&2; exit 2 ;; esac
case "$target" in cpu|gpu) ;; *) echo "invalid target" >&2; exit 2 ;; esac
case "$interval_s:$duration_s" in *[!0-9:]*|:*) echo "invalid interval/duration" >&2; exit 2 ;; esac
[ "$interval_s" -gt 0 ] || interval_s=1
[ -n "$output" ] && [ -r "$plan" ] || { echo "readable plan and output are required" >&2; exit 2; }

mkdir -p "$(dirname "$output")" || exit 3
touch "$output" || exit 3
seq=0
started_s=$(date +%s 2>/dev/null || echo 0)

now_ms() { seconds=$(date +%s 2>/dev/null || echo 0); echo $((seconds * 1000)); }
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

numeric_value() {
    set -- $1
    value=${1:-}
    case "$value" in ''|*[!0-9.+-]*) return 1 ;; esac
    printf '%s' "$value"
}

millidegree_value() {
    value=$(numeric_value "$1") || return 1
    case "$value" in *.*) return 1 ;; esac
    sign=
    [ "$value" -lt 0 ] && { sign=-; value=$((-value)); }
    printf '%s%d.%03d' "$sign" $((value / 1000)) $((value % 1000))
}

proc_stat_value() {
    previous_file=$(dirname "$output")/proc-stat.previous
    IFS=' ' read -r cpu user nice system idle iowait irq softirq steal guest guestnice < "$1" || return 1
    [ "$cpu" = cpu ] || return 1
    total=$((user + nice + system + idle + iowait + irq + softirq + steal))
    idle_total=$((idle + iowait))
    if [ ! -s "$previous_file" ]; then
        printf '%s %s\n' "$total" "$idle_total" > "$previous_file"
        return 1
    fi
    IFS=' ' read -r previous_total previous_idle < "$previous_file" || return 1
    printf '%s %s\n' "$total" "$idle_total" > "$previous_file"
    delta_total=$((total - previous_total))
    delta_idle=$((idle_total - previous_idle))
    [ "$delta_total" -gt 0 ] || return 1
    scaled=$(((delta_total - delta_idle) * 100000 / delta_total))
    printf '%d.%03d' $((scaled / 1000)) $((scaled % 1000))
}

append_value() {
    metric=$1 parser=$2 source_path=$3
    [ -r "$source_path" ] || return 0
    IFS= read -r raw < "$source_path" || raw=
    raw=${raw%"$carriage_return"}
    case "$parser" in
        proc_stat_utilization) value_json=$(proc_stat_value "$source_path") || return 0 ;;
        int|number|float) value_json=$(numeric_value "$raw") || return 0 ;;
        millidegree_celsius) value_json=$(millidegree_value "$raw") || return 0 ;;
        temperature_auto)
            value=$(numeric_value "$raw") || return 0
            case "$value" in *.*) value_json=$value ;;
                *) if [ "$value" -gt 200 ] || [ "$value" -lt -200 ]; then
                       value_json=$(millidegree_value "$value") || return 0
                   else value_json=$value; fi ;;
            esac ;;
        *) value_json="\"$(json_escape "$raw")\"" ;;
    esac
    seq=$((seq + 1))
    printf '{"schema_version":1,"test_id":"%s","run_id":"%s","seq":%s,"timestamp_ms":%s,"source":"%s-telemetry","type":"telemetry","payload":{"metric":"%s","value":%s,"path":"%s"}}\n' \
        "$test_id" "$attempt_id" "$seq" "$(now_ms)" "$target" "$(json_escape "$metric")" "$value_json" "$(json_escape "$source_path")" >> "$output"
}

sample_once() {
    carriage_return=$(printf '\r')
    while IFS='|' read -r metric parser path_pattern; do
        metric=${metric%"$carriage_return"}
        parser=${parser%"$carriage_return"}
        path_pattern=${path_pattern%"$carriage_return"}
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
    if [ "$duration_s" -gt 0 ] && [ $((current_s - started_s)) -ge "$duration_s" ]; then break; fi
    [ "$duration_s" -eq 0 ] && [ -z "$stop_file" ] && break
    sleep "$interval_s"
done

if [ "$seq" -eq 0 ]; then
    echo "avs-telemetry-agent: no telemetry samples were collected" >&2
    exit 5
fi
exit 0
