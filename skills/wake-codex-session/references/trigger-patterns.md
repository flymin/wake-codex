# Trigger Patterns

Use these patterns only after adapting commands, states, and paths to the actual workload. A
trigger answers whether the session needs attention now; it need not mean the workload succeeded.

## Scheduler Job

Query the scheduler by a stable job ID. Return `1` while any job is pending or running, `0` once
all jobs are terminal, and another code if state cannot be determined reliably. Wake on both
successful and failed terminal states so failures do not wait forever.

For Slurm, request machine-oriented output and normalize suffixes such as `COMPLETED+` before
classification. Treat an empty result or an unknown state as an error, not completion.

```bash
set -uo pipefail

job_id="JOB_ID"
state=$(sacct -n -X -j "$job_id" -o State | awk 'NF {print $1; exit}') || exit 2
[[ -n "$state" ]] || exit 2
state=${state%%+*}

case "$state" in
  PENDING|CONFIGURING|RUNNING|COMPLETING|SUSPENDED|REQUEUED|RESIZING)
    printf 'job %s: %s\n' "$job_id" "$state"
    exit 1
    ;;
  COMPLETED|FAILED|CANCELLED|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL|PREEMPTED|BOOT_FAIL|DEADLINE)
    printf 'job %s terminal: %s\n' "$job_id" "$state"
    exit 0
    ;;
  *)
    printf 'unknown job state for %s: %s\n' "$job_id" "$state" >&2
    exit 2
    ;;
esac
```

For multiple jobs, classify every job and return `0` only after all are terminal. Return an error
if any requested job is absent or unclassifiable.

## Download Or Background Program

Prefer a durable completion record written by a producer wrapper. Record the exit code with an
atomic rename whether the command succeeds or fails:

```bash
: "${RUN_STATE_DIR:?set RUN_STATE_DIR to a persistent state directory}"
status_file="$RUN_STATE_DIR/run.status"
status_tmp="${status_file}.tmp"
set +e
long_running_command >"$RUN_STATE_DIR/run.log" 2>&1
rc=$?
printf '%s\n' "$rc" >"$status_tmp"
mv -f -- "$status_tmp" "$status_file"
exit "$rc"
```

Launch this wrapper through a scheduler, service manager, or another verified durable detach
mechanism. Do not assume a bare trailing `&` will survive the current Codex tool session.
Replace `RUN_STATE_DIR` with the same durable location in the producer and trigger, or configure it
in both execution environments; do not assume the daemon inherits an ad-hoc interactive export.

The trigger becomes small and deterministic:

```bash
set -uo pipefail

: "${RUN_STATE_DIR:?set RUN_STATE_DIR to a persistent state directory}"
status_file="$RUN_STATE_DIR/run.status"
if [[ -s "$status_file" ]]; then
  printf 'producer finished with exit code %s\n' "$(<"$status_file")"
  exit 0
fi
printf 'producer still running\n'
exit 1
```

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
