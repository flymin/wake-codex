#!/usr/bin/env bash
set -uo pipefail

sacct_bin="${SACCT_BIN:-sacct}"
job_ids=(12345 12346 12347)
job_list="$(IFS=,; printf '%s' "${job_ids[*]}")"

if ! status_output="$("$sacct_bin" -n -X -j "$job_list" --format=JobIDRaw,State --parsable2 2>&1)"; then
    printf 'sacct failed: %s\n' "$status_output" >&2
    exit 2
fi

declare -A job_statuses=()
while IFS='|' read -r job_id status _; do
    case "$job_id" in
        12345|12346|12347)
            job_statuses["$job_id"]="${status%%+*}"
            ;;
    esac
done <<< "$status_output"

all_terminal=1
for job_id in "${job_ids[@]}"; do
    status="${job_statuses[$job_id]:-}"
    if [[ -z "$status" ]]; then
        printf 'job %s is missing from sacct output\n' "$job_id" >&2
        exit 2
    fi
    printf 'job %s: %s\n' "$job_id" "$status"
    case "$status" in
        COMPLETED|FAILED|CANCELLED|TIMEOUT|NODE_FAIL|OUT_OF_MEMORY|PREEMPTED)
            ;;
        PENDING|RUNNING|CONFIGURING|COMPLETING|SUSPENDED)
            all_terminal=0
            ;;
        *)
            printf 'job %s has unknown state: %s\n' "$job_id" "$status" >&2
            exit 2
            ;;
    esac
done

if [[ "$all_terminal" -eq 1 ]]; then
    exit 0
fi
exit 1
