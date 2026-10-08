---
name: wake-codex-session
description: Schedule wake-codex to resume a Codex session when a durable external condition becomes ready, such as a scheduler job, download, detached program, service, artifact, or remote status. Use only from the main agent; side sessions and subagents must not invoke it.
---

# Wake Codex Session

Use this workflow only from the root, user-facing agent. It creates a durable task containing a bounded trigger and a short resume marker, then hands monitoring to either the running wake-codex daemon or a foreground managed shell. After a successful handoff, end the turn and do not poll.

## Target and execution path

Choose one target session: use a session ID explicitly supplied by the user, otherwise the main agent's `CODEX_THREAD_ID`. Validate it as a canonical UUID; never guess another ID.

Run `wake-codex list --json` before creating files. Success selects daemon submission. Use the managed-shell one-shot fallback only when it fails because no daemon is reachable. Missing/broken wake-codex or another failure is a blocker. The fallback cannot deliver recurring wakes; report that limitation if recurrence was requested.

Defaults are one-shot, `queue-only`, and a ten-minute cadence. Accept only `queue-only` or `strict`. Honor a valid requested cron on the daemon path. For the fallback, use an explicit positive poll interval; convert only `*/N * * * *` (where `N` divides 60) and `0 * * * *` to seconds. Do not silently approximate other cron expressions.

## Build the task

Use a new private persistent directory under a user-specified root, or the standard wake-codex state directory. Use a purpose slug plus a timestamp or UUID; do not use an ephemeral directory, overwrite an existing task, or put secrets in names.

Create `task.yaml`, executable `trigger.sh`, and `message.txt`. YAML should use the wake-codex task schema: `version`, `name`, `thread_id`, `trigger`, `message`, and the applicable schedule/lifecycle/mode fields. Let wake-codex apply defaults where supported. Recurring tasks require an explicit user request and `lifecycle: continuous` plus `continuous_trigger: edge` or `always`; omit that field for one-shot tasks.

The trigger must be non-interactive, bounded, idempotent, and read-only with respect to the workload. Return `0` when the session needs attention (success or terminal failure), `1` when definitely still waiting, and another code when state is unavailable or ambiguous. Keep durable state outside stdout; use atomic result publication and a supervisor query where applicable. Read [trigger-patterns.md](references/trigger-patterns.md) for workload-specific checks. Do not embed credentials; use only an existing restricted credential source available to the runner.

Write `message.txt` with a summary of at most 10 words on the first line. Start the actual resume prompt on the second line. Do not write a wake-codex tag yourself: wake-codex automatically prepends `[wake-codex: TASK_NAME | TASK_ID_PREFIX] ` to the file contents, using the task name and the first eight characters of its task ID. The delivered first line is the tag followed by your summary.

Keep the body a concise resume marker, preferably within three sentences and 400 Unicode characters. For example:

```text
Resume original task after job completion
Resume the original task from where this wake was scheduled; re-read the preceding context or active goal and continue all unfinished work. Do not stop after only checking the wake condition.
```

Optionally add one short condition and at most one durable reference.

## Validate and hand off

Check that the trigger is executable and bounded, then run it once and capture its exact exit code: `0` means the condition is already ready, so continue the current task without handoff; `1` means waiting, so proceed to handoff; any other code or timeout requires fixing or reporting the blocker.

For the daemon, run one normal `wake-codex submit ABSOLUTE_TASK_FOLDER`; require successful output with a task ID or next-run confirmation. For the fallback, start exactly one foreground command in a Codex-managed shell:

```bash
wake-codex --mode EFFECTIVE_MODE --poll-interval EFFECTIVE_POLL_INTERVAL --timeout -1 ABSOLUTE_TASK_FOLDER
```

Do not use `nohup`, `setsid`, shell `&`, direct `codex queue`, or a long polling loop. Treat fallback startup as successful once the command starts without a setup error; an immediately delivered wake is also success. Retain the shell handle when it remains running.

Report the path, task ID or shell handle, task directory, condition, and effective cadence. If the target differs from the current session, tell the user the target thread must later be loaded by a Codex process (it may be resumed then); do not perform a liveness check. Then stop work and end the turn.
