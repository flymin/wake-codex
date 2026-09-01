from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wake_codex.config import ConfigError, load_task_config
from wake_codex.daemon import DaemonError, run_daemon
from wake_codex.daemon_paths import resolve_state_dir, socket_path
from wake_codex.ipc import IpcError, request
from wake_codex.runner import EXIT_SETUP, RunnerSetupError, run_task


DAEMON_COMMANDS = {"daemon", "submit", "list", "show", "events", "cancel", "purge"}

TOP_LEVEL_DESCRIPTION = """Queue task messages to Codex threads, either with a foreground one-shot
runner or a persistent multi-task daemon.

Modes:
  one-shot              Poll one task in the foreground until its message is queued.
  daemon                Run the multi-task cron scheduler in the foreground.

Daemon commands:
  daemon                Run the scheduler process. Manage its lifecycle externally.
  submit TASK_FOLDER    Register a scheduled task and return immediately.
  list                  List active tasks; use --all to include terminal tasks.
  show TASK_ID          Show task state and recent execution events.
  events TASK_ID        Show trigger, strict-check, and queue events.
  cancel TASK_ID        Cancel an active task.
  purge outputs|tasks   Preview or delete retained daemon data.
"""

TOP_LEVEL_EPILOG = """
Examples:
  wake-codex tasks/my-task
  wake-codex daemon
  wake-codex submit tasks/my-scheduled-task
  wake-codex list --all
  wake-codex show TASK_ID --full

Run 'wake-codex COMMAND --help' for command-specific options.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wake-codex",
        usage="wake-codex [ONE-SHOT OPTIONS] TASK_FOLDER\n       wake-codex COMMAND [OPTIONS]",
        description=TOP_LEVEL_DESCRIPTION,
        epilog=TOP_LEVEL_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    general = parser.add_argument_group("general")
    general.add_argument("-h", "--help", action="help", help="show this help message and exit")
    one_shot = parser.add_argument_group("one-shot mode")
    one_shot.add_argument("task_folder", metavar="TASK_FOLDER", help="task directory containing task.yaml")
    _codex_argument(one_shot)
    one_shot.add_argument(
        "--codex-home",
        metavar="PATH",
        help="Codex state directory used to validate the session (default: CODEX_HOME or ~/.codex)",
    )
    one_shot.add_argument(
        "--app-server-endpoint",
        default="unix://",
        metavar="ENDPOINT",
        help="Unix app-server endpoint used by strict mode (default: unix://)",
    )
    one_shot.add_argument(
        "--mode",
        choices=("queue-only", "strict"),
        default="queue-only",
        help="activity check mode (default: queue-only)",
    )
    one_shot.add_argument(
        "--silent",
        type=int,
        choices=range(4),
        default=0,
        metavar="{0,1,2,3}",
        help="output suppression level: 0=all, 1=no note, 2=no poll, 3=final only (default: 0)",
    )
    one_shot.add_argument(
        "--poll-interval",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="seconds between checks or retries (default: 60)",
    )
    one_shot.add_argument(
        "--timeout",
        type=float,
        default=-1.0,
        metavar="SECONDS",
        help="overall timeout in seconds; -1 disables it (default: -1)",
    )
    one_shot.add_argument(
        "--command-timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="timeout for each trigger or Codex invocation (default: 30)",
    )
    one_shot.add_argument(
        "--force",
        action="store_true",
        help="ignore delivered or ambiguous state and allow another delivery",
    )
    return parser


def _state_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir", help="daemon state directory (default: WAKE_CODEX_HOME or XDG state directory)"
    )


def _codex_argument(parser: argparse._ActionsContainer) -> None:
    parser.add_argument(
        "--codex",
        default=shutil.which("codex"),
        metavar="PATH",
        help="Codex executable or wrapper (default: codex resolved from PATH)",
    )


def build_command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wake-codex",
        description="Manage the wake-codex scheduler daemon and its registered tasks.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    daemon = commands.add_parser(
        "daemon",
        help="run the scheduler daemon in the foreground",
        description=(
            "Run the scheduler in the foreground. Use systemd, tmux, or another process "
            "manager when background lifecycle management is required."
        ),
    )
    _codex_argument(daemon)
    daemon.add_argument(
        "--codex-home",
        metavar="PATH",
        help="Codex state directory (default: CODEX_HOME or ~/.codex)",
    )
    daemon.add_argument(
        "--app-server-endpoint",
        default="unix://",
        metavar="ENDPOINT",
        help="Unix app-server endpoint shared by strict tasks (default: unix://)",
    )
    daemon.add_argument(
        "--command-timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="timeout for each trigger or Codex invocation (default: 30)",
    )
    daemon.add_argument(
        "--retry-interval",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="delay before retrying temporary queue failures (default: 60)",
    )
    daemon.add_argument(
        "--max-workers",
        type=int,
        default=4,
        metavar="N",
        help="maximum number of tasks executing concurrently (default: 4)",
    )
    _state_argument(daemon)

    submit = commands.add_parser(
        "submit",
        help="register a scheduled task",
        description="Register a task.yaml containing a cron schedule and return immediately.",
    )
    submit.add_argument("task_folder", metavar="TASK_FOLDER", help="task directory containing task.yaml")
    submit.add_argument(
        "--force",
        action="store_true",
        help="ignore prior terminal or ambiguous one-shot state after duplicate-delivery review",
    )
    _state_argument(submit)

    listing = commands.add_parser(
        "list", help="list daemon tasks", description="List tasks registered with the running daemon."
    )
    listing.add_argument("--all", action="store_true", help="include terminal tasks")
    listing.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    _state_argument(listing)

    show = commands.add_parser(
        "show",
        help="show a task and recent events",
        description="Show frozen task configuration, current state, and recent events.",
    )
    show.add_argument("task_id", metavar="TASK_ID", help="task UUID, unique UUID prefix, or unique name")
    show.add_argument("--full", action="store_true", help="include full retained stdout and stderr")
    show.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    _state_argument(show)

    events = commands.add_parser(
        "events",
        help="show task execution events",
        description="Show trigger, strict activity-check, and queue execution events.",
    )
    events.add_argument("task_id", metavar="TASK_ID", help="task UUID, unique UUID prefix, or unique name")
    events.add_argument(
        "--limit", type=int, default=20, metavar="N", help="maximum events to return (default: 20)"
    )
    events.add_argument("--full", action="store_true", help="include full retained stdout and stderr")
    events.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    _state_argument(events)

    cancel = commands.add_parser(
        "cancel", help="cancel a task", description="Cancel an active daemon-managed task."
    )
    cancel.add_argument("task_id", metavar="TASK_ID", help="task UUID, unique UUID prefix, or unique name")
    _state_argument(cancel)

    purge = commands.add_parser(
        "purge",
        help="purge retained daemon data",
        description="Preview or delete retained outputs or terminal tasks. Purges are dry-run by default.",
    )
    purge_commands = purge.add_subparsers(dest="purge_kind", required=True, metavar="TARGET")
    outputs = purge_commands.add_parser(
        "outputs", help="delete compressed command output", description="Purge gzip artifacts while retaining event metadata."
    )
    outputs.add_argument("--task", dest="task_id", metavar="TASK_ID", help="filter by task")
    outputs.add_argument("--type", dest="kind", metavar="TYPE", help="filter by event type")
    outputs.add_argument("--status", help="filter by event status")
    outputs.add_argument("--before", help="only events completed before this ISO-8601 timestamp")
    outputs.add_argument("--older-than", type=float, metavar="DAYS", help="only events older than this age")
    outputs.add_argument("--confirm", action="store_true", help="perform deletion instead of a dry run")
    outputs.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    _state_argument(outputs)
    tasks = purge_commands.add_parser(
        "tasks",
        help="delete terminal tasks and their events",
        description="Purge terminal task records and cascade their retained event artifacts.",
    )
    tasks.add_argument("--status", help="filter by terminal task status")
    tasks.add_argument("--before", help="only tasks updated before this ISO-8601 timestamp")
    tasks.add_argument("--older-than", type=float, metavar="DAYS", help="only tasks older than this age")
    tasks.add_argument("--confirm", action="store_true", help="perform deletion instead of a dry run")
    tasks.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    _state_argument(tasks)
    return parser


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _print_tasks(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        print("No tasks.")
        return
    print(f"{'ID':8}  {'STATUS':10}  {'NEXT RUN':25}  NAME")
    for task in tasks:
        print(
            f"{task['id'][:8]:8}  {task['status'][:10]:10}  "
            f"{(task.get('next_run_at') or '-')[:25]:25}  {task['name']}"
        )


def _print_events(events: list[dict[str, Any]], full: bool = False) -> None:
    if not events:
        print("No events.")
        return
    for event in events:
        print(
            f"event {event['id']} {event['kind']} {event['status']} "
            f"rc={event.get('returncode')} duration={event.get('duration_ms')}ms"
        )
        if event.get("detail"):
            print(f"  detail: {event['detail']}")
        if full:
            if event.get("stdout"):
                print("  stdout:\n" + str(event["stdout"]).rstrip())
            if event.get("stderr"):
                print("  stderr:\n" + str(event["stderr"]).rstrip())


def _daemon_request(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    if args.command == "daemon":
        if args.command_timeout <= 0 or args.retry_interval <= 0 or args.max_workers <= 0:
            raise RunnerSetupError("daemon timeouts and --max-workers must be greater than zero")
        return run_daemon(
            state_dir=state_dir,
            codex_entry=args.codex,
            codex_home=args.codex_home,
            app_server_endpoint=args.app_server_endpoint,
            command_timeout=args.command_timeout,
            retry_interval=args.retry_interval,
            max_workers=args.max_workers,
        )

    payload: dict[str, Any] = {"action": args.command}
    if args.command == "submit":
        payload.update(task_folder=str(Path(args.task_folder).expanduser().resolve()), force=args.force)
    elif args.command == "list":
        payload["all"] = args.all
    elif args.command == "show":
        payload.update(task_id=args.task_id, full=args.full)
    elif args.command == "events":
        payload.update(task_id=args.task_id, limit=args.limit, full=args.full)
    elif args.command == "cancel":
        payload["task_id"] = args.task_id
    elif args.command == "purge":
        if args.before and args.older_than is not None:
            raise RunnerSetupError("--before and --older-than are mutually exclusive")
        if args.older_than is not None:
            if args.older_than < 0:
                raise RunnerSetupError("--older-than must not be negative")
            args.before = (datetime.now(timezone.utc) - timedelta(days=args.older_than)).isoformat()
        payload["action"] = f"purge_{args.purge_kind}"
        if args.purge_kind == "outputs":
            payload.update(task_id=args.task_id, kind=args.kind, status=args.status, before=args.before)
        else:
            payload.update(status=args.status, before=args.before)
        payload["confirm"] = args.confirm

    result = request(socket_path(state_dir), payload)
    if getattr(args, "json", False):
        _print_json(result)
    elif args.command == "submit":
        print(f"Submitted {result['id']} ({result['name']}), next run {result['next_run_at']}")
    elif args.command == "list":
        _print_tasks(result)
    elif args.command == "show":
        print(
            f"{result['id']}  {result['status']}  {result['name']}\n"
            f"folder: {result['task_dir']}\nthread: {result['thread_id']}\n"
            f"schedule: {result['schedule']} ({result['timezone']})\n"
            f"lifecycle: {result['lifecycle']}  mode: {result['mode']}\n"
            f"next run: {result.get('next_run_at') or '-'}  deliveries: {result['delivery_count']}"
        )
        if result.get("last_error"):
            print(f"last error: {result['last_error']}")
        _print_events(result["events"], args.full)
    elif args.command == "events":
        _print_events(result, args.full)
    elif args.command == "cancel":
        print(f"Cancellation requested: {result['id']} ({result['status']})")
    elif args.command == "purge":
        verb = "Purged" if args.confirm else "Would purge"
        unit = "events" if args.purge_kind == "outputs" else "tasks"
        print(f"{verb} {result[unit]} {unit}, {result['bytes']} bytes")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments and arguments[0] in DAEMON_COMMANDS:
        parser = build_command_parser()
        args = parser.parse_args(arguments)
        if args.command == "daemon" and args.codex is None:
            parser.error("--codex was omitted and no codex executable was found in PATH")
        try:
            return _daemon_request(args)
        except (ConfigError, RunnerSetupError, DaemonError, IpcError) as exc:
            print(f"wake-codex: {exc}", file=sys.stderr)
            return EXIT_SETUP
        except KeyboardInterrupt:
            print("wake-codex: interrupted", file=sys.stderr)
            return 130
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.codex is None:
        parser.error("--codex was omitted and no codex executable was found in PATH")
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than 0")
    if args.timeout != -1 and args.timeout <= 0:
        parser.error("--timeout must be -1 or greater than 0")
    if args.command_timeout <= 0:
        parser.error("--command-timeout must be greater than 0")

    try:
        config = load_task_config(args.task_folder)
        return run_task(
            config,
            codex_entry=args.codex,
            codex_home=args.codex_home,
            app_server_endpoint=args.app_server_endpoint,
            mode=args.mode,
            silent=args.silent,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
            command_timeout=args.command_timeout,
            force=args.force,
        )
    except (ConfigError, RunnerSetupError) as exc:
        print(f"wake-codex: {exc}", file=sys.stderr)
        return EXIT_SETUP
    except KeyboardInterrupt:
        print("wake-codex: interrupted", file=sys.stderr)
        return 130
