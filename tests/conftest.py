import sqlite3
import sys
from datetime import datetime, tzinfo
from pathlib import Path

import pytest


@pytest.fixture
def queue_only_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    """Allow only queue subprocesses; fail on any Codex session inspection."""
    from wake_codex import daemon, runner

    codex_home = tmp_path / "isolated-codex-home"
    codex_home.mkdir()
    calls = tmp_path / "codex-commands.jsonl"
    behavior = tmp_path / "queue-behavior"
    behavior.write_text("success", encoding="utf-8")
    codex = tmp_path / "queue-only-codex"
    codex.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"calls = Path({str(calls)!r})\n"
        "args = sys.argv[1:]\n"
        "with calls.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(args) + '\\n')\n"
        "if args == ['queue', '--help']:\n    raise SystemExit(0)\n"
        "if len(args) != 5 or args[:2] != ['queue', '--thread'] or args[3] != '--message':\n"
        "    raise SystemExit('unexpected command: ' + repr(args))\n"
        f"behavior = Path({str(behavior)!r}).read_text()\n"
        "if behavior == 'missing':\n"
        "    print('no rollout found for thread id', file=sys.stderr)\n"
        "    raise SystemExit(2)\n"
        "if behavior == 'retry':\n    raise SystemExit(9)\n"
        "if behavior == 'retry-once':\n"
        "    attempts = [json.loads(line) for line in calls.read_text().splitlines()]\n"
        "    if sum(item[:2] == ['queue', '--thread'] for item in attempts) == 1:\n"
        "        raise SystemExit(9)\n"
        "print('queued')\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("queue-only attempted a Codex session/app-server query")

    for name in (
        "_legacy_session_state", "_AppServerClient", "_validate_strict_session_exists",
        "_proxy_command", "_require_session_loaded",
    ):
        monkeypatch.setattr(runner, name, forbidden)
    monkeypatch.setattr(daemon, "_validate_strict_session_exists", forbidden)
    monkeypatch.setattr(daemon, "_proxy_command", forbidden)
    monkeypatch.setattr(daemon.WakeDaemon, "_strict_check", forbidden)

    for name in ("open", "iterdir", "glob", "rglob"):
        original = getattr(Path, name)

        def guarded(path: Path, *args: object, _original=original, **kwargs: object):
            if path.is_relative_to(codex_home):
                pytest.fail(f"queue-only accessed Codex home contents: {path}")
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, guarded)

    original_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        if str(codex_home) in str(database):
            pytest.fail(f"queue-only opened a Codex database: {database}")
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    return codex, codex_home, calls, behavior


@pytest.fixture
def message_time(monkeypatch: pytest.MonkeyPatch) -> str:
    instant = datetime(2026, 10, 8, 13, 45, 6).astimezone()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr("wake_codex.runner.datetime", FixedDatetime)
    return "261008-134506"
