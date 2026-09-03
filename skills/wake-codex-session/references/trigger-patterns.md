# Trigger Patterns

Use these patterns only after adapting commands, states, and paths to the actual workload. A
trigger answers whether the session needs attention now; it need not mean the workload succeeded.

## Scheduler Job

Query the scheduler by a stable job ID. Return `1` while work is progressing normally, `0` when all
jobs are terminal or any job enters a configured attention-required state, and another code if
state cannot be determined reliably. Wake on both successful and failed terminal states so failures
do not wait forever.

For Slurm, request machine-oriented output and normalize suffixes such as `COMPLETED+` before
classification. The following example intentionally accepts exactly one allocation record for one
non-array job. Treat no record, multiple records, or an unknown state as an error, not completion.
Before using it, compare the mapping with the states documented by the target cluster and extend it
for any site- or version-specific states; never guess how an unknown state should be classified.

```bash
set -uo pipefail

job_id="JOB_ID"
raw=$(sacct -n -X -j "$job_id" -o JobIDRaw,State -P) || exit 2
mapfile -t records < <(printf '%s\n' "$raw" | awk -F'|' 'NF == 2 && $1 != "" && $2 != ""')
if (( ${#records[@]} != 1 )); then
  printf 'expected one allocation record for job %s, got %s\n' "$job_id" "${#records[@]}" >&2
  exit 2
fi

record_job_id=${records[0]%%|*}
state=${records[0]#*|}
[[ "$record_job_id" == "$job_id" ]] || {
  printf 'unexpected allocation record for job %s: %s\n' "$job_id" "$record_job_id" >&2
  exit 2
}
state=${state%% *}
state=${state%%+*}

case "$state" in
  PENDING|CONFIGURING|RUNNING|COMPLETING|SUSPENDED|REQUEUED|RESIZING|SIGNALING|STAGE_OUT|STOPPED|REQUEUE_FED)
    printf 'job %s: %s\n' "$job_id" "$state"
    exit 1
    ;;
  REQUEUE_HOLD|RESV_DEL_HOLD|SPECIAL_EXIT)
    printf 'job %s requires attention: %s\n' "$job_id" "$state"
    exit 0
    ;;
  COMPLETED|FAILED|CANCELLED|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL|PREEMPTED|BOOT_FAIL|DEADLINE|REVOKED)
    printf 'job %s terminal: %s\n' "$job_id" "$state"
    exit 0
    ;;
  *)
    printf 'unknown job state for %s: %s\n' "$job_id" "$state" >&2
    exit 2
    ;;
esac
```

For arrays, heterogeneous jobs, or multiple jobs, query and classify every expected allocation
record. Apply this precedence after reading all records: return an error if any requested record is
absent, duplicated, or unclassifiable; otherwise return `0` if any record requires attention;
otherwise return `1` if any record is active; otherwise return `0` because every record is terminal.
Do not adapt the single-record example by selecting only its first row.

## Download Or Background Program

Prefer a fresh per-run state directory and a durable completion record written by a producer
wrapper. Refuse to start if the final status already exists, and publish the exit code with an
atomic rename whether the command succeeds or fails:

```bash
: "${RUN_STATE_DIR:?set RUN_STATE_DIR to a persistent state directory}"
status_file="$RUN_STATE_DIR/run.status"
status_tmp="${status_file}.tmp"
[[ ! -e "$status_file" && ! -e "$status_tmp" ]] || {
  printf 'status path already exists: %s\n' "$status_file" >&2
  exit 125
}
set +e
long_running_command >"$RUN_STATE_DIR/run.log" 2>&1
rc=$?
set -e
printf '%s\n' "$rc" >"$status_tmp" || exit 125
mv -- "$status_tmp" "$status_file" || exit 125
exit "$rc"
```

Create `RUN_STATE_DIR` uniquely for this run with mode `0700`, then launch this wrapper through a
scheduler, service manager, or another verified durable detach mechanism. Do not assume a bare
trailing `&` will survive the current Codex tool session.
Replace `RUN_STATE_DIR` with the same durable location in the producer and trigger, or configure it
in both execution environments; do not assume the daemon inherits an ad-hoc interactive export.

The trigger must combine the result file with a durable supervisor query. Define
`query_durable_supervisor_state` using the scheduler, service manager, or equivalent authoritative
interface for the workload, then adapt this pattern:

```bash
set -uo pipefail

: "${RUN_STATE_DIR:?set RUN_STATE_DIR to a persistent state directory}"
status_file="$RUN_STATE_DIR/run.status"
if [[ -e "$status_file" ]]; then
  status=$(<"$status_file") || exit 2
  if [[ ! "$status" =~ ^([0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])$ ]]; then
    printf 'invalid producer exit code: %s\n' "$status" >&2
    exit 2
  fi
  printf 'producer finished with exit code %s\n' "$status"
  exit 0
fi

supervisor_state=$(query_durable_supervisor_state) || exit 2
case "$supervisor_state" in
  active)
    printf 'producer still running\n'
    exit 1
    ;;
  terminal)
    printf 'producer terminated without publishing status\n'
    exit 0
    ;;
  *)
    printf 'indeterminate producer state: %s\n' "$supervisor_state" >&2
    exit 2
    ;;
esac
```

Use `active` only while the supervisor positively confirms liveness. Map every terminal supervisor
outcome, including launch failure and disappearance after a recorded start, to `terminal`; the
resumed agent can then diagnose the missing status. Treat an unavailable or ambiguous supervisor
answer as an error.

Do not use final-file existence alone when partial files can appear early. For downloads, write a
verified `.complete` or status record only after checksum/size validation. Wake on a recorded
failure too; the queued message should tell Codex to inspect the exit code and log.

## Existing Process Without a Wrapper

Use a stable supervisor/service/job identifier when available. A bare PID is weak because it can
be reused. If no stronger interface exists, record both PID and expected command/start identity,
verify identity on every check, and distinguish:

- matching process alive: return `1`;
- matching process ended and durable output is available: return `0`;
- PID reused, identity unavailable, or state ambiguous: return another code.

Never make the trigger wait for the process. Each invocation should inspect and return quickly.

## Artifact Or Remote Condition

Use a machine-readable readiness signal: a manifest flag, atomic sentinel, checksum, API status,
or database state. Return `0` only when the complete predicate is true, `1` when it is definitely
not true yet, and another code on authentication, parsing, transport, or schema errors. Bound all
network calls with a timeout shorter than the daemon command timeout.

Never embed credentials in a trigger, task configuration, message, result file, or command output.
Use only a pre-existing restricted credential source available to the selected runner.
