from datetime import datetime, tzinfo

import pytest


@pytest.fixture
def message_time(monkeypatch: pytest.MonkeyPatch) -> str:
    instant = datetime(2026, 10, 8, 13, 45, 6).astimezone()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr("wake_codex.runner.datetime", FixedDatetime)
    return "261008-134506"
