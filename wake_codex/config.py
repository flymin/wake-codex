from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import CroniterBadCronError, croniter


CONFIG_FILENAME = "task.yaml"
SUPPORTED_FIELDS = {
    "version",
    "name",
    "thread_id",
    "trigger",
    "message",
    "schedule",
    "timezone",
    "lifecycle",
    "continuous_trigger",
    "mode",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TaskConfig:
    task_dir: Path
    config_path: Path
    name: str
    thread_id: str
    trigger_path: Path
    message_path: Path
    schedule: str | None = None
    timezone: str | None = None
    lifecycle: str = "once"
    continuous_trigger: str = "edge"
    mode: str = "queue-only"


def _required_text(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _session_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ConfigError(
            "thread_id must be a canonical UUID, for example "
            "123e4567-e89b-42d3-a456-426614174000"
        ) from exc
    canonical = str(parsed)
    if value != canonical:
        raise ConfigError(f"thread_id must use canonical lowercase UUID format: {canonical}")
    return canonical


def _task_file(task_dir: Path, value: str, field: str) -> Path:
    candidate = (task_dir / value).resolve()
    try:
        candidate.relative_to(task_dir)
    except ValueError as exc:
        raise ConfigError(f"{field} must resolve inside the task folder: {value}") from exc
    if not candidate.is_file():
        raise ConfigError(f"{field} is not a regular file: {candidate}")
    return candidate


def _optional_text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string when provided")
    return value.strip()


def load_task_config(task_folder: str | os.PathLike[str]) -> TaskConfig:
    task_dir = Path(task_folder).expanduser().resolve()
    if not task_dir.is_dir():
        raise ConfigError(f"task folder is not a directory: {task_dir}")

    config_path = task_dir / CONFIG_FILENAME
    if not config_path.is_file():
        raise ConfigError(f"missing {CONFIG_FILENAME}: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_FILENAME} must contain a YAML mapping")

    data: dict[str, object] = raw
    unknown = sorted(set(data) - SUPPORTED_FIELDS)
    if unknown:
        raise ConfigError(f"unknown task field(s): {', '.join(unknown)}")
    if data.get("version") != 1:
        raise ConfigError("version must be 1")

    name_value = data.get("name", task_dir.name)
    if not isinstance(name_value, str) or not name_value.strip():
        raise ConfigError("name must be a non-empty string when provided")
    thread_id = _session_id(_required_text(data, "thread_id"))
    trigger_path = _task_file(task_dir, _required_text(data, "trigger"), "trigger")
    message_path = _task_file(task_dir, _required_text(data, "message"), "message")
    if not os.access(trigger_path, os.X_OK):
        raise ConfigError(f"trigger is not executable: {trigger_path}")

    schedule = _optional_text(data, "schedule")
    if schedule is not None:
        try:
            croniter(schedule)
        except (CroniterBadCronError, ValueError, KeyError) as exc:
            raise ConfigError(f"schedule must be a valid five-field cron expression: {schedule}") from exc
        if len(schedule.split()) != 5:
            raise ConfigError("schedule must contain exactly five cron fields")

    timezone_name = _optional_text(data, "timezone")
    if timezone_name is not None:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"unknown IANA timezone: {timezone_name}") from exc

    lifecycle = _optional_text(data, "lifecycle") or "once"
    if lifecycle not in {"once", "continuous"}:
        raise ConfigError("lifecycle must be once or continuous")
    continuous_trigger = _optional_text(data, "continuous_trigger") or "edge"
    if continuous_trigger not in {"edge", "always"}:
        raise ConfigError("continuous_trigger must be edge or always")
    if "continuous_trigger" in data and lifecycle != "continuous":
        raise ConfigError("continuous_trigger is only valid with lifecycle continuous")
    mode = _optional_text(data, "mode") or "queue-only"
    if mode not in {"queue-only", "strict"}:
        raise ConfigError("mode must be queue-only or strict")

    return TaskConfig(
        task_dir=task_dir,
        config_path=config_path,
        name=name_value.strip(),
        thread_id=thread_id,
        trigger_path=trigger_path,
        message_path=message_path,
        schedule=schedule,
        timezone=timezone_name,
        lifecycle=lifecycle,
        continuous_trigger=continuous_trigger,
        mode=mode,
    )
