from __future__ import annotations

import os
from pathlib import Path

import pytest

from wake_codex.config import ConfigError, load_task_config


THREAD_ID = "123e4567-e89b-42d3-a456-426614174000"


def _write_task(task_dir: Path, *, trigger: str = "trigger.sh", message: str = "message.txt") -> None:
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "name: test-task",
                f"thread_id: {THREAD_ID}",
                f"trigger: {trigger}",
                f"message: {message}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    trigger_path = task_dir / "trigger.sh"
    trigger_path.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    trigger_path.chmod(0o755)
    (task_dir / "message.txt").write_text("", encoding="utf-8")


def test_loads_valid_task_and_allows_empty_message(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    _write_task(task_dir)

    config = load_task_config(task_dir)

    assert config.name == "test-task"
    assert config.thread_id == THREAD_ID
    assert config.message_path.read_text() == ""


def test_rejects_path_outside_task(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    task_dir = tmp_path / "task"
    _write_task(task_dir, message="../outside.txt")

    with pytest.raises(ConfigError, match="inside the task folder"):
        load_task_config(task_dir)


def test_rejects_non_executable_trigger(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    _write_task(task_dir)
    os.chmod(task_dir / "trigger.sh", 0o644)

    with pytest.raises(ConfigError, match="not executable"):
        load_task_config(task_dir)


@pytest.mark.parametrize(
    "thread_id",
    [
        "123e4567-e89b-42d3-a456-42661417400",
        "not-a-session-id",
        "123E4567-E89B-42D3-A456-426614174000",
        "{123e4567-e89b-42d3-a456-426614174000}",
    ],
)
def test_rejects_noncanonical_session_id(tmp_path: Path, thread_id: str) -> None:
    task_dir = tmp_path / "task"
    _write_task(task_dir)
    config_path = task_dir / "task.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(THREAD_ID, thread_id),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="thread_id must"):
        load_task_config(task_dir)


def test_loads_daemon_schedule_fields(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    _write_task(task_dir)
    with (task_dir / "task.yaml").open("a", encoding="utf-8") as handle:
        handle.write(
            "schedule: '*/5 * * * *'\n"
            "timezone: Asia/Shanghai\n"
            "lifecycle: continuous\n"
            "continuous_trigger: always\n"
            "mode: strict\n"
        )

    config = load_task_config(task_dir)

    assert config.schedule == "*/5 * * * *"
    assert config.timezone == "Asia/Shanghai"
    assert config.lifecycle == "continuous"
    assert config.continuous_trigger == "always"
    assert config.mode == "strict"


@pytest.mark.parametrize(
    ("line", "match"),
    [
        ("schedule: '* * * *'", "five-field"),
        ("schedule: '* * * * * *'", "five"),
        ("timezone: Nowhere/Invalid", "timezone"),
        ("lifecycle: forever", "lifecycle"),
        ("lifecycle: continuous\ncontinuous_trigger: repeated", "continuous_trigger"),
        ("continuous_trigger: always", "only valid with lifecycle continuous"),
        ("mode: guessed", "mode"),
    ],
)
def test_rejects_invalid_daemon_fields(tmp_path: Path, line: str, match: str) -> None:
    task_dir = tmp_path / "task"
    _write_task(task_dir)
    with (task_dir / "task.yaml").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

    with pytest.raises(ConfigError, match=match):
        load_task_config(task_dir)
