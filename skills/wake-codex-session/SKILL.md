---
name: wake-codex-session
description: Schedule the main Codex session to resume itself through wake-codex after a long-running external condition becomes ready, using the local daemon when available and a Codex-managed foreground one-shot shell otherwise. Use only from the main agent when Codex should stop the current turn while waiting for a scheduler job, download, detached program, service, artifact, or other durable condition, then queue a short resume marker that makes the same main session continue its original unfinished work. Side sessions and subagents must hand the request to the main agent without creating or starting a wake task. If the user explicitly invokes wake-codex-session, require a session ID that exactly matches the main session's current CODEX_THREAD_ID.
---

# Wake Codex Session

Create a new one-shot wake-codex task for the selected session. Prefer submitting it to a running
daemon; when no daemon is reachable, run wake-codex in the foreground of a Codex-managed persistent
shell. End the current agent turn after either path is confirmed active. Do not keep polling the
condition from the agent after handoff.

## Main Agent Only

Determine the execution context before selecting a target session or taking any task action. The
main agent is the root, user-facing agent for the thread being resumed. Treat every delegated task,
side session, subagent, or uncertain context as non-main. Only the main agent may use this workflow,
and it must not delegate any step.

When running in a side session or subagent:

1. Do not inspect the daemon, use that session's `CODEX_THREAD_ID` as the default target, create task
   files, execute the trigger, submit a task, or start a managed shell.
2. Return or send a handoff to the main agent containing the requested wait condition, any explicit
   session ID for validation, durable job/process/artifact identifiers, requested schedule or mode,
   and a concise proposed continuation message. Never substitute the side/subagent session ID.
3. Ask the main agent to perform the complete workflow and stop. If no supported handoff channel is
   available, report that the wake must be scheduled by the main agent; do not improvise locally.

The main agent must independently validate all supplied details, target only its own current
session, construct the task, and start the selected monitoring path.

## Select The Target Session

Require the main agent's current `CODEX_THREAD_ID` and validate that it is a canonical UUID. Use it
as `TARGET_SESSION_ID`; no other target is permitted.

- Treat `$wake-codex-session` or an unambiguous request to use the named `wake-codex-session` skill
  as explicit invocation. Require a canonical session ID in the user's request. If it is absent,
  ask the user for it and stop. If it does not exactly equal `CODEX_THREAD_ID`, reject it; do not
  schedule a wake for another session.
- Treat automatic selection based only on waiting/resume intent as implicit invocation. Use the
  main agent's `CODEX_THREAD_ID` without asking the user to repeat it.

If the main agent's `CODEX_THREAD_ID` is absent or invalid, stop without guessing from transcripts,
process state, recent sessions, or a side/subagent thread.

## Invariants

- Assume `wake-codex` is available on `PATH`. Prefer a reachable daemon, but allow the managed-shell
  one-shot fallback when no daemon is running.
- Ensure the monitored workload survives this turn independently. Scheduler jobs, services, and
  properly detached processes qualify. A subprocess tied to an active tool call does not.
- Default to a ten-minute check cadence, `lifecycle: once`, and `mode: queue-only`.
- Do not independently check whether the target session is loaded. The target is always the current
  main session, so ending the agent turn is sufficient. Never archive/delete the thread, exit the
  TUI, or kill its Codex process.
- Let wake-codex call `codex queue`; do not call it directly.
- Never use `nohup`, `setsid`, shell `&`, or another self-detaching launch for the fallback. Keep
  wake-codex in the foreground of the managed shell so its lifetime and output remain attached to
  the Codex session.

## Workflow

### 1. Select the execution path

Before creating task files, run:

```bash
wake-codex list --json
```

- If it succeeds, select the daemon path.
- If it fails specifically because no daemon is reachable, select the managed-shell path. Do not
  start a daemon merely for this task.
- If wake-codex itself is missing, broken, or fails for another reason, diagnose or report the
  blocker instead of treating it as daemon absence.

Remember the selected path and report it in the final handoff.

Define `EFFECTIVE_SCHEDULE`, `EFFECTIVE_MODE`, and `EFFECTIVE_POLL_INTERVAL` before constructing the
task. Defaults are `*/10 * * * *`, `queue-only`, and `600` seconds. Accept only the supported modes
`queue-only` and `strict`; reject any other requested mode before writing files.

Never discard a requested cadence. On the daemon path, honor a valid requested cron expression. If
the user supplies only an interval of `N` whole minutes, convert it to `*/N * * * *` only when
`1 <= N < 60` and `N` divides 60; map exactly 60 minutes to `0 * * * *`. Otherwise obtain an explicit
five-field cron expression. On the managed-shell path, honor an explicit positive interval. A
`*/N * * * *` cron may use `N * 60` seconds under the same `1 <= N < 60` divisor rule, and
`0 * * * *` may use `3600` seconds, with interval timing rather than wall-clock alignment. Do not
approximate any other cron expression in one-shot mode; obtain an explicit interval if the daemon
is unavailable.

### 2. Define the wake condition

Translate the requested wait into a short, deterministic check. A trigger exit code means:

- `0`: the condition is resolved and this thread needs attention now;
- `1`: the condition is definitely still waiting;
- any other code: the check itself failed or cannot determine state reliably.

Wake on terminal failure as well as success when monitoring a job, download, or program. Otherwise
a failed workload could wait forever. Print concise trigger diagnostics, but do not rely on stdout
for continuation state because it is not inserted into `message.txt`. Keep any state needed after
wake-up in a durable external source or an atomically published terminal-result file.

Read [references/trigger-patterns.md](references/trigger-patterns.md) when constructing checks for
scheduler jobs, downloads, background processes, artifacts, or remote conditions.

### 3. Choose a persistent task folder

Create a new unique folder for every invocation. Use the first applicable root:

1. the user's requested task root;
2. `WAKE_CODEX_SESSION_TASKS_DIR`;
3. `$WAKE_CODEX_HOME/session-tasks` when `WAKE_CODEX_HOME` is set;
4. `$XDG_STATE_HOME/wake-codex/session-tasks` when `XDG_STATE_HOME` is set;
5. `$HOME/.local/state/wake-codex/session-tasks`.

Use a concise purpose slug plus a UTC timestamp or UUID. Do not use an ephemeral temporary
directory, overwrite an existing folder, include secrets in its name, or place it in a tracked
source directory. Create the task folder with mode `0700` where possible and retain its resolved
absolute path for the final report.

### 4. Construct all three task files

Create `task.yaml`, `trigger.sh`, and `message.txt` inside the new folder.

Use this YAML shape, substituting the canonical `TARGET_SESSION_ID` and a unique safe name:

```yaml
version: 1
name: session-wake-PURPOSE-UNIQUE
thread_id: TARGET_SESSION_ID
trigger: trigger.sh
message: message.txt
schedule: "*/10 * * * *"
lifecycle: once
mode: queue-only
```

Replace the shown schedule and mode with `EFFECTIVE_SCHEDULE` and `EFFECTIVE_MODE`.

Construct `trigger.sh` with a shebang and executable permission. Prefer `set -uo pipefail`; use
`set -e` only when normal waiting probes cannot be mistaken for shell failures. The script must be
idempotent, non-interactive, bounded, and normally finish well within the daemon command timeout.
It may update private observation state in its task folder but must not mutate the monitored work.

Construct `message.txt` as a resume marker, not a new task specification or handoff summary. Start
from this template, preserving the first two sentences or their faithful translation:

```text
Resume the original task from where this wake was scheduled; re-read the preceding context or active goal and continue all unfinished work. Do not stop after only checking the wake condition. Wake condition: CONDITION; evidence: REFERENCE.
```

The wake-condition sentence is optional. When useful, keep it to one short condition and at most one
durable job, path, artifact, or service reference. The resume instruction is always the dominant
content.

Limit the message to three sentences and 400 Unicode characters, including its trailing newline.
Do not restate the original objective, add a checklist or new plan, enumerate configurations or
results, or turn trigger verification into the resumed task. The prior conversation or active goal
already carries the original work. Keep the file editable until queue time.

Do not embed credentials in any task file or command output. Remote checks may use only a
pre-existing, access-restricted credential source that is confirmed available to the selected
runner. If none is available, report a blocker.

### 5. Validate before handoff

Perform all of these checks:

1. Validate shell syntax, paths, permissions, YAML fields, and the canonical target session ID.
2. Verify that `message.txt` is non-empty, preserves the resume-first template semantics, contains
   at most three sentences, and satisfies `test "$(wc -m < message.txt)" -le 400`. Shorten it before
   continuing if any check fails.
3. Execute the trigger once and capture its exact exit code without treating `1` as a shell/tool
   failure.
4. If it returns `1`, continue to the selected handoff path.
5. If it returns `0`, do not schedule a redundant wake. The condition is already ready; continue
   the original work in the current turn or report that no wait is needed.
6. If it returns any other code or times out, fix the trigger or report the blocker. Do not submit
   a broken task.

Do not invoke the trigger through a long polling loop. The daemon or managed one-shot runner owns
subsequent checks.

### 6. Start monitoring and stop

#### Daemon path

Run exactly one normal registration after validation:

```bash
wake-codex submit ABSOLUTE_TASK_FOLDER
```

Do not use `--force` for a newly created session-wake task. Treat submission as successful only when
the command exits successfully and returns a task ID/next-run confirmation. On failure, diagnose
or report it; remain in the current turn because no wake is registered.

#### Managed-shell path

Start exactly one long-running command in a Codex-managed shell or PTY, with a short initial yield
so the command remains running there while control returns to the agent:

```bash
wake-codex --mode EFFECTIVE_MODE --poll-interval EFFECTIVE_POLL_INTERVAL --timeout -1 ABSOLUTE_TASK_FOLDER
```

The YAML cron schedule does not drive one-shot mode; `--poll-interval` controls its cadence.

Run wake-codex as the foreground process in that shell. Do not background it inside the shell and
do not redirect away its terminal output. Treat startup as successful only after its output confirms
session validation, monitoring startup, and an initial block result, and the managed command is
still running. Retain the managed shell/session handle for later inspection. If it exits during
startup, inspect its result: a completed delivery needs no wake task, while any failure must be
diagnosed or reported in the current turn.

After successful daemon submission or managed-shell startup:

1. Report the selected path, task ID or managed shell handle, absolute task folder, monitored
   condition, and effective schedule or poll interval.
2. Stop all further polling and work on the original task.
3. End the agent turn with the short handoff. Do not run `exit`, close Codex, archive the thread, or
   wait for the trigger. For the managed-shell path, intentionally leave its foreground wake-codex
   command running across turns.
