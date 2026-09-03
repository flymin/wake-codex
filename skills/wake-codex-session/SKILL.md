---
name: wake-codex-session
description: Schedule a Codex session to resume through the local wake-codex daemon after a long-running external condition becomes ready. Use when Codex should stop the current turn while waiting for a scheduler job, download, detached program, service, artifact, or other durable condition, then queue a continuation message to a target session. If the user explicitly invokes wake-codex-session, require the user to provide the target session ID; when the skill is selected implicitly, default to the current CODEX_THREAD_ID.
---

# Wake Codex Session

Create a new one-shot wake-codex task for the selected session, submit it to the running daemon,
and end the current agent turn. Do not poll the condition inside this turn after successful
submission.

## Select The Target Session

Determine whether the user explicitly invoked this skill before creating any files:

- Treat `$wake-codex-session` or an unambiguous request to use the named `wake-codex-session` skill
  as explicit invocation. Require the session ID in the user's request. If it is absent, ask the
  user for it and stop. Never fall back to the current session in this branch.
- Treat automatic selection based only on waiting/resume intent as implicit invocation. Use the
  current `CODEX_THREAD_ID`. If it is absent, stop without guessing from transcripts, process
  state, or recent sessions.

In either branch, require the selected ID to be a canonical UUID. Refer to it as `TARGET_SESSION_ID`
throughout the remaining workflow. Do not silently replace an invalid user-provided ID.

## Invariants

- Require a reachable wake-codex daemon. Assume `wake-codex` is available on `PATH`.
- Ensure the monitored workload survives this turn independently. Scheduler jobs, services, and
  properly detached processes qualify. A subprocess tied to an active tool call does not.
- Use `schedule: "*/10 * * * *"`, `lifecycle: once`, and `mode: queue-only` unless the user
  explicitly requests another supported schedule or mode.
- Keep the target session loaded by a Codex TUI/CLI/app-server process. When targeting the current
  session, ending the agent turn is sufficient; never archive/delete the thread, exit the TUI, or
  kill its Codex process. If the target is not loaded, the queued message waits until
  `codex resume <TARGET_SESSION_ID>` loads it again.
- Submit through `wake-codex submit`; do not call `codex queue` directly.
- Before creating a task, run `wake-codex list --json`. If it cannot reach the daemon, report that
  the daemon must be started and do not claim a wake was scheduled.

## Workflow

### 1. Define the wake condition

Translate the requested wait into a short, deterministic check. A trigger exit code means:

- `0`: the condition is resolved and this thread needs attention now;
- `1`: the condition is definitely still waiting;
- any other code: the check itself failed or cannot determine state reliably.

Wake on terminal failure as well as success when monitoring a job, download, or program. Otherwise
a failed workload could wait forever. Put the outcome in trigger output and instruct the resumed
agent to inspect it.

Read [references/trigger-patterns.md](references/trigger-patterns.md) when constructing checks for
scheduler jobs, downloads, background processes, artifacts, or remote conditions.

### 2. Choose a persistent task folder

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

### 3. Construct all three task files

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

Construct `trigger.sh` with a shebang and executable permission. Prefer `set -uo pipefail`; use
`set -e` only when normal waiting probes cannot be mistaken for shell failures. The script must be
idempotent, non-interactive, bounded, and normally finish well within the daemon command timeout.
It may update private observation state in its task folder but must not mutate the monitored work.

Construct `message.txt` as a concrete continuation instruction for the target session. Include:

- what condition resolved;
- which durable output, status, or log paths to inspect;
- the next action the resumed agent should take;
- a reminder to verify success/failure rather than assuming success.

Keep it concise and exclude credentials, tokens, and unnecessary log content. The file remains
editable until queue time.

### 4. Validate before submission

Perform all of these checks:

1. Validate shell syntax, paths, permissions, non-empty message, YAML fields, and the canonical
   target session ID.
2. Execute the trigger once and capture its exact exit code without treating `1` as a shell/tool
   failure.
3. If it returns `1`, continue to submission.
4. If it returns `0`, do not schedule a redundant wake. The condition is already ready; continue
   the original work in the current turn or report that no wait is needed.
5. If it returns any other code or times out, fix the trigger or report the blocker. Do not submit
   a broken task.

Do not invoke the trigger through a long polling loop. The daemon owns subsequent checks.

### 5. Submit and stop

Run exactly one normal registration after validation:

```bash
wake-codex submit ABSOLUTE_TASK_FOLDER
```

Do not use `--force` for a newly created session-wake task. Treat submission as successful only when
the command exits successfully and returns a task ID/next-run confirmation. On failure, diagnose
or report it; remain in the current turn because no wake is registered.

After successful submission:

1. Report the task ID/output, absolute task folder, monitored condition, and effective schedule.
2. State that the target Codex process/thread must remain loaded; otherwise the user must resume it.
3. Stop all further polling and work on the original task.
4. End the agent turn with the short handoff. Do not run `exit`, close Codex, archive the thread, or
   wait for the trigger.
