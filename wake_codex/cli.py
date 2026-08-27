from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from wake_codex.config import ConfigError, load_task_config
from wake_codex.runner import EXIT_SETUP, RunnerSetupError, run_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wake-codex",
        description="Poll a task trigger and queue the task message to a Codex thread.",
    )
    parser.add_argument("task_folder", help="task directory containing task.yaml")
    parser.add_argument("--codex", required=True, help="path to the Codex executable or wrapper")
    parser.add_argument(
        "--codex-home",
        help="Codex state directory used to validate the session (default: CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "--app-server-endpoint",
        default="unix://",
        help="Unix app-server endpoint used by strict mode (default: unix://)",
    )
    parser.add_argument(
        "--mode",
        choices=("queue-only", "strict"),
        default="queue-only",
        help="activity check mode (default: queue-only)",
    )
    parser.add_argument(
        "--silent",
        type=int,
        choices=range(4),
        default=0,
        metavar="{0,1,2,3}",
        help="output suppression level: 0=all, 1=no note, 2=no poll, 3=final only (default: 0)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="seconds between checks or retries (default: 60)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=-1.0,
        metavar="SECONDS",
        help="overall timeout in seconds; -1 disables it (default: -1)",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="timeout for each trigger or Codex invocation (default: 30)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore delivered or ambiguous state and allow another delivery",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
