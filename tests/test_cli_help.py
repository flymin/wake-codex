from __future__ import annotations

import pytest

from wake_codex.cli import main


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

    assert "Run the scheduler in the foreground" in output
    assert "--codex PATH" in output
    assert "--retry-interval SECONDS" in output
    assert "--max-workers N" in output
    assert "--state-dir STATE_DIR" in output


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
