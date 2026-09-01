from __future__ import annotations

from pathlib import Path

import pytest

from wake_codex import cli
from wake_codex.cli import build_command_parser, build_parser, main


def _help(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exit_info:
        main(arguments)
    assert exit_info.value.code == 0
    return capsys.readouterr().out


def test_top_level_help_exposes_both_modes_and_daemon_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _help(["-h"], capsys)

    assert "wake-codex [ONE-SHOT OPTIONS] TASK_FOLDER" in output
    assert "wake-codex COMMAND [OPTIONS]" in output
    assert "Modes:" in output
    assert "one-shot" in output
    assert "daemon" in output
    for command in ("submit", "list", "show", "events", "cancel", "purge"):
        assert command in output
    assert output.index("Daemon commands:") < output.index("one-shot mode:")
    assert "wake-codex COMMAND --help" in output


def test_daemon_help_describes_scheduler_controls(capsys: pytest.CaptureFixture[str]) -> None:
    output = _help(["daemon", "-h"], capsys)
    normalized = " ".join(output.split())

    assert "Run the scheduler in the foreground" in output
    assert "--codex PATH" in output
    assert "codex resolved from PATH" in normalized
    assert "--retry-interval SECONDS" in output
    assert "--max-workers N" in output
    assert "--state-dir STATE_DIR" in output


def test_codex_defaults_to_executable_resolved_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert build_parser().parse_args(["task"]).codex == str(codex)
    assert build_command_parser().parse_args(["daemon"]).codex == str(codex)
    assert build_parser().parse_args(["--codex", "/custom/codex", "task"]).codex == "/custom/codex"


@pytest.mark.parametrize("arguments", [["task"], ["daemon"]])
def test_missing_default_codex_is_rejected_before_execution(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    with pytest.raises(SystemExit) as exit_info:
        main(arguments)

    assert exit_info.value.code == 2
    assert "no codex executable was found in PATH" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["submit", "-h"], "Register a task.yaml containing a cron schedule"),
        (["list", "-h"], "include terminal tasks"),
        (["show", "-h"], "include full retained stdout and stderr"),
        (["events", "-h"], "maximum events to return"),
        (["cancel", "-h"], "Cancel an active daemon-managed task"),
        (["purge", "-h"], "Purges are dry-run"),
        (["purge", "outputs", "-h"], "perform deletion instead of a dry run"),
        (["purge", "tasks", "-h"], "cascade their retained event artifacts"),
    ],
)
def test_daemon_subcommand_help_is_descriptive(
    arguments: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert expected in _help(arguments, capsys)
