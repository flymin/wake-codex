from __future__ import annotations

from datetime import datetime, timezone

from wake_codex.schedule import local_timezone_name, next_run_at


def test_next_run_uses_task_timezone_and_returns_utc() -> None:
    after = datetime(2026, 1, 1, 0, 4, 30, tzinfo=timezone.utc)

    assert next_run_at("0 9 * * *", "Asia/Shanghai", after) == "2026-01-01T01:00:00+00:00"


def test_local_timezone_honors_valid_tz_environment(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Shanghai")

    assert local_timezone_name() == "Asia/Shanghai"
