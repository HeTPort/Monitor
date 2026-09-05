#!/bin/sh

# Independent append-only collector. Required metrics are read as one bounded
# snapshot before optional metrics. Sysfs/proc reads and numeric parsing use
# POSIX shell builtins; external date is called once per emitted snapshot.
# No awk or tr is required on the tested HarmonyOS image.
TELEMETRY_VERSION=0.3.0

if [ "${1:-}" = "--version" ]; then
    echo "avs-telemetry-agent $TELEMETRY_VERSION schema 1 snapshot 1"
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

output_dir=${output%/*}
[ "$output_dir" = "$output" ] && output_dir=.
mkdir -p "$output_dir" || exit 3
touch "$output" || exit 3
previous_file=$output_dir/proc-stat.previous
snapshot_seq=0
complete_snapshots=0
started_s=$(date +%s 2>/dev/null || echo 0)
carriage_return=$(printf '\r')

should_stop() {
    [ -n "$stop_file" ] && [ -e "$stop_file" ] && return 0
    if [ "$duration_s" -gt 0 ]; then
        current_s=$(date +%s 2>/dev/null || echo 0)
        [ $((current_s - started_s)) -ge "$duration_s" ] && return 0
    fi
    return 1
}

numeric_value() {
    set -f
    set -- $1
    set +f
    parsed_json=${1:-}
    case "$parsed_json" in ''|*[!0-9.+-]*) return 1 ;; esac
    return 0
}

millidegree_value() {
    numeric_value "$1" || return 1
    number=$parsed_json
    case "$number" in *.*) return 1 ;; esac
    sign=
    [ "$number" -lt 0 ] && { sign=-; number=$((-number)); }
    whole=$((number / 1000))
    fraction=$((number % 1000))
    case "$fraction" in
        [0-9]) fraction=00$fraction ;;
        [0-9][0-9]) fraction=0$fraction ;;
    esac
    parsed_json=$sign$whole.$fraction
}

proc_stat_value() {
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
    whole=$((scaled / 1000))
    fraction=$((scaled % 1000))
    case "$fraction" in
        [0-9]) fraction=00$fraction ;;
        [0-9][0-9]) fraction=0$fraction ;;
    esac
    parsed_json=$whole.$fraction
}

parse_source() {
    parser=$1 source_path=$2
    if [ "$parser" = proc_stat_utilization ]; then
        proc_stat_value "$source_path"
        return $?
    fi
    IFS= read -r raw < "$source_path" || raw=
    raw=${raw%"$carriage_return"}
    case "$parser" in
        int|number|float) numeric_value "$raw" ;;
        prefixed_number)
            raw=${raw##*:}
            numeric_value "$raw"
            ;;
        millidegree_celsius) millidegree_value "$raw" ;;
        temperature_auto)
            numeric_value "$raw" || return 1
            number=$parsed_json
            case "$number" in
                *.*) parsed_json=$number ;;
                *)
                    if [ "$number" -gt 200 ] || [ "$number" -lt -200 ]; then
                        millidegree_value "$number" || return 1
                    else
                        parsed_json=$number
                    fi
                    ;;
            esac
            ;;
        text)
            # Platform text values used here are single-line sysfs tokens. Skip
            # unsafe JSON delimiters rather than invoking sed once per value.
            case "$raw" in *\\*|*\"*) return 1 ;; esac
            parsed_json="\"$raw\""
            ;;
        *) return 1 ;;
    esac
}

append_missing() {
    [ -n "$missing_json" ] && missing_json="$missing_json,"
    missing_json="$missing_json\"$1\""
}

collect_metric() {
    metric=$1 parser=$2 selection=$3 path_patterns=$4
    values_json= paths_json= metric_count=0 value_separator=
    for source_path in $path_patterns; do
        [ -e "$source_path" ] && [ -r "$source_path" ] || continue
        parse_source "$parser" "$source_path" || continue
        values_json="$values_json$value_separator$parsed_json"
        paths_json="$paths_json$value_separator\"$source_path\""
        value_separator=,
        metric_count=$((metric_count + 1))
        [ "$selection" = first ] && break
    done
    [ "$metric_count" -gt 0 ] || return 1
    [ -n "$metrics_json" ] && metrics_json="$metrics_json,"
    [ -n "$sources_json" ] && sources_json="$sources_json,"
    metrics_json="$metrics_json\"$metric\":[$values_json]"
    sources_json="$sources_json\"$metric\":[$paths_json]"
    return 0
}

collect_priority() {
    wanted=$1
    while IFS='|' read -r priority metric parser selection path_patterns; do
        priority=${priority%"$carriage_return"}
        metric=${metric%"$carriage_return"}
        parser=${parser%"$carriage_return"}
        selection=${selection%"$carriage_return"}
        path_patterns=${path_patterns%"$carriage_return"}
        case "$priority" in ''|'#'*) continue ;; esac

        # Backward compatibility for a deployed 0.2 plan during an atomic
        # upgrade: metric|parser|path becomes required|metric|parser|all|path.
        if [ -z "$path_patterns" ]; then
            path_patterns=$parser
            parser=$metric
            metric=$priority
            priority=required
            selection=all
        fi
        [ "$priority" = "$wanted" ] || continue
        [ "$wanted" = optional ] && should_stop && return
        case "$metric:$parser:$selection:$path_patterns" in *\"*|*\\*|*\'*) continue ;; esac
        if ! collect_metric "$metric" "$parser" "$selection" "$path_patterns"; then
            [ "$wanted" = required ] && append_missing "$metric"
        fi
    done < "$plan"
}

prewarm_proc_stat() {
    while IFS='|' read -r priority metric parser selection path_patterns; do
        priority=${priority%"$carriage_return"}
        parser=${parser%"$carriage_return"}
        path_patterns=${path_patterns%"$carriage_return"}
        if [ -z "$path_patterns" ]; then
            path_patterns=$parser
            parser=$metric
        fi
        [ "$parser" = proc_stat_utilization ] || continue
        for source_path in $path_patterns; do
            [ -r "$source_path" ] || continue
            proc_stat_value "$source_path" >/dev/null 2>&1 || true
            return
        done
    done < "$plan"
}

count_required_plan_rows() {
    required_plan_rows=0
    while IFS='|' read -r priority metric parser selection path_patterns; do
        priority=${priority%"$carriage_return"}
        case "$priority" in ''|'#'*) continue ;; esac
        if [ "$priority" = required ] || [ -z "$path_patterns" ]; then
            required_plan_rows=$((required_plan_rows + 1))
        fi
    done < "$plan"
}

sample_once() {
    metrics_json= sources_json= missing_json=
    collect_priority required
    # A stop request that arrives during the required scan cannot tear the
    # snapshot. It only skips optional work.
    should_stop || collect_priority optional
    if [ -z "$missing_json" ]; then
        complete=true
        complete_snapshots=$((complete_snapshots + 1))
    else
        complete=false
    fi
    snapshot_seq=$((snapshot_seq + 1))
    seconds=$(date +%s 2>/dev/null || echo 0)
    timestamp_ms=$((seconds * 1000))
    printf '{"schema_version":1,"test_id":"%s","run_id":"%s","seq":%s,"timestamp_ms":%s,"source":"%s-telemetry","type":"telemetry","payload":{"sample_id":%s,"complete":%s,"metrics":{%s},"sources":{%s},"missing_required":[%s]}}\n' \
        "$test_id" "$attempt_id" "$snapshot_seq" "$timestamp_ms" "$target" "$snapshot_seq" "$complete" \
        "$metrics_json" "$sources_json" "$missing_json" >> "$output"
}

count_required_plan_rows
if [ "$required_plan_rows" -eq 0 ]; then
    echo "avs-telemetry-agent: telemetry plan has no required metrics" >&2
    exit 5
fi
prewarm_proc_stat
while :; do
    if should_stop; then
        [ "$snapshot_seq" -eq 0 ] && [ -n "$stop_file" ] && [ -e "$stop_file" ] && exit 0
        break
    fi
    sample_once
    should_stop && break
    [ "$duration_s" -eq 0 ] && [ -z "$stop_file" ] && break
    slept_s=0
    while [ "$slept_s" -lt "$interval_s" ]; do
        should_stop && break
        sleep 1
        slept_s=$((slept_s + 1))
    done
done

if [ "$complete_snapshots" -eq 0 ]; then
    echo "avs-telemetry-agent: no complete required telemetry snapshot was collected" >&2
    exit 5
fi
exit 0
