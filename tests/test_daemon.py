from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path

import pytest

from wake_codex.cli import _print_json, _print_tasks, main
from wake_codex.daemon import ActiveRun, WakeDaemon
from wake_codex.daemon_paths import socket_path
from wake_codex.daemon_store import DaemonStore
from wake_codex.ipc import request
from wake_codex.runner import EXIT_SETUP
from wake_codex.schedule import utc_now


THREAD_ID = "123e4567-e89b-42d3-a456-426614174000"


def _executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_codex(tmp_path: Path, behavior: str = "success") -> Path:
    behavior_path = tmp_path / "behavior"
    behavior_path.write_text(behavior, encoding="utf-8")
    calls = tmp_path / "queue-calls"
    checks = tmp_path / "loaded-checks"
    return _executable(
        tmp_path / "codex",
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        f"behavior = Path({str(behavior_path)!r}).read_text().strip()\n"
        "if sys.argv[1:] == ['queue', '--help']:\n    raise SystemExit(0)\n"
        "if len(sys.argv) >= 3 and sys.argv[1] == 'app-server' and sys.argv[2] in {'--stdio', 'proxy'}:\n"
        "    home = Path(os.environ['CODEX_HOME'])\n"
        f"    archived = any(home.joinpath('archived_sessions').rglob('*-{THREAD_ID}.jsonl'))\n"
        f"    active = (home / 'session_index.jsonl').exists() and {THREAD_ID!r} in (home / 'session_index.jsonl').read_text()\n"
        "    for line in sys.stdin:\n"
        "        request = json.loads(line)\n"
        "        method = request.get('method')\n"
        "        request_id = request.get('id')\n"
        "        if method == 'initialize':\n"
        "            response = {'id': request_id, 'result': {}}\n"
        "        elif method == 'thread/read':\n"
        "            if active or archived:\n"
        f"                response = {{'id': request_id, 'result': {{'thread': {{'id': {THREAD_ID!r}}}}}}}\n"
        "            else:\n"
        "                response = {'id': request_id, 'error': {'code': -32600, 'message': 'thread not loaded'}}\n"
        "        elif method == 'thread/list':\n"
        f"            data = [{{'id': {THREAD_ID!r}}}] if request.get('params', {{}}).get('archived') and archived else []\n"
        "            response = {'id': request_id, 'result': {'data': data, 'nextCursor': None}}\n"
        "        elif method == 'thread/loaded/list':\n"
        f"            p = Path({str(checks)!r})\n"
        "            p.write_text(str((int(p.read_text()) if p.exists() else 0) + 1))\n"
        f"            response = {{'id': request_id, 'result': {{'data': [{THREAD_ID!r}]}}}}\n"
        "        else:\n"
        "            continue\n"
        "        print(json.dumps(response), flush=True)\n"
        "    raise SystemExit(0)\n"
        f"p = Path({str(calls)!r})\n"
        "p.write_text(str((int(p.read_text()) if p.exists() else 0) + 1))\n"
        "if behavior == 'timeout': time.sleep(30)\n"
        "if behavior == 'missing':\n    print('failed to read thread: no rollout found for thread id', file=sys.stderr)\n    raise SystemExit(2)\n"
        "print('queued')\n",
    )


def _task(tmp_path: Path, trigger: str = "exit 0", **fields: str) -> Path:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    lines = [
        "version: 1",
        "name: daemon-test",
        f"thread_id: {THREAD_ID}",
        "trigger: trigger.sh",
        "message: message.txt",
        "schedule: '* * * * *'",
    ]
    lines.extend(f"{key}: {value}" for key, value in fields.items())
    (task_dir / "task.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _executable(task_dir / "trigger.sh", f"#!/usr/bin/env bash\n{trigger}\n")
    (task_dir / "message.txt").write_text("test prompt", encoding="utf-8")
    return task_dir


def _daemon(tmp_path: Path, behavior: str = "success", timeout: float = 1.0) -> WakeDaemon:
    codex = _fake_codex(tmp_path, behavior)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "session_index.jsonl").write_text(
        json.dumps({"id": THREAD_ID}) + "\n", encoding="utf-8"
    )
    return WakeDaemon(
        state_dir=tmp_path / "state",
        codex_entry=str(codex),
        codex_home=str(codex_home),
        app_server_endpoint="unix://",
        command_timeout=timeout,
        retry_interval=0.01,
        max_workers=2,
        tick_interval=0.01,
    )


async def _execute(daemon: WakeDaemon, task_id: str) -> None:
    worker = asyncio.create_task(daemon._run_task_guarded(task_id))
    daemon.runs[task_id] = ActiveRun(worker)
    try:
        await worker
    finally:
        daemon.runs.pop(task_id, None)


def _close(daemon: WakeDaemon) -> None:
    for lock in daemon.locks.values():
        lock.close()
    daemon.store.close()


def _cli_output(arguments: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(arguments)
    return code, output.getvalue()


def test_once_task_delivers_and_retains_output(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    task = daemon._submit(str(_task(tmp_path)), False)
    try:
        asyncio.run(_execute(daemon, task["id"]))
        stored = daemon.store.get_task(task["id"])
        events = daemon.store.list_events(task["id"])
        assert stored["status"] == "delivered"
        assert stored["delivery_count"] == 1
        assert [event["kind"] for event in events] == ["queue", "trigger"]
        assert [event["status"] for event in events] == ["ok", "ok"]
        assert daemon.store.read_artifact(events[0]["stdout_path"]).strip() == "queued"
        assert (tmp_path / "queue-calls").read_text() == "1"
    finally:
        _close(daemon)


def test_continuous_task_requires_block_to_rearm(tmp_path: Path) -> None:
    trigger = (
        "count=$(cat count 2>/dev/null || echo 0); count=$((count + 1)); echo $count > count; "
        "if [[ $count -eq 3 ]]; then exit 1; fi; exit 0"
    )
    daemon = _daemon(tmp_path)
    task = daemon._submit(str(_task(tmp_path, trigger, lifecycle="continuous")), False)
    try:
        async def scenario() -> None:
            for _ in range(4):
                await _execute(daemon, task["id"])

        asyncio.run(scenario())
        stored = daemon.store.get_task(task["id"])
        assert stored["status"] == "scheduled"
        assert stored["delivery_count"] == 2
        assert stored["armed"] == 0
        assert (tmp_path / "queue-calls").read_text() == "2"
        trigger_statuses = [
            event["status"] for event in daemon.store.list_events(task["id"], 20)
            if event["kind"] == "trigger"
        ]
        assert "block" in trigger_statuses
        assert "error" not in trigger_statuses
    finally:
        _close(daemon)


@pytest.mark.parametrize(
    ("trigger", "command_timeout", "event_status", "returncode", "task_status", "last_result"),
    [
        ("echo go; exit 0", 1.0, "ok", 0, "delivered", "go"),
        ("echo 'job queued'; exit 1", 1.0, "block", 1, "scheduled", "block"),
        ("echo failed >&2; exit 7", 1.0, "error", 7, "scheduled", "trigger-error"),
        ("sleep 30", 0.05, "timeout", None, "scheduled", "trigger-timeout"),
    ],
)
def test_trigger_event_status_follows_trigger_protocol(
    tmp_path: Path,
    trigger: str,
    command_timeout: float,
    event_status: str,
    returncode: int | None,
    task_status: str,
    last_result: str,
) -> None:
    daemon = _daemon(tmp_path)
    task = daemon._submit(str(_task(tmp_path, trigger)), False)
    daemon.command_timeout = command_timeout
    try:
        asyncio.run(_execute(daemon, task["id"]))
        stored = daemon.store.get_task(task["id"])
        event = next(
            item for item in daemon.store.list_events(task["id"], 20)
            if item["kind"] == "trigger"
        )
        assert event["status"] == event_status
        assert event["returncode"] == returncode
        assert stored["status"] == task_status
        assert stored["last_result"] == last_result
        assert (stored["next_run_at"] is not None) is (task_status == "scheduled")
    finally:
        _close(daemon)


def test_strict_checks_before_trigger_and_queue(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    task = daemon._submit(str(_task(tmp_path, mode="strict")), False)
    try:
        asyncio.run(_execute(daemon, task["id"]))
        assert (tmp_path / "loaded-checks").read_text() == "2"
        assert daemon.store.get_task(task["id"])["status"] == "delivered"
        events = daemon.store.list_events(task["id"], 20)
        assert all(event["status"] == "ok" for event in events)
    finally:
        _close(daemon)


def test_queue_permanent_rejection_is_terminal(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, "missing")
    task = daemon._submit(str(_task(tmp_path)), False)
    try:
        asyncio.run(_execute(daemon, task["id"]))
        assert daemon.store.get_task(task["id"])["status"] == "rejected"
        queue_event = daemon.store.list_events(task["id"], 20)[0]
        assert queue_event["kind"] == "queue"
        assert queue_event["status"] == "error"
    finally:
        _close(daemon)


def test_queue_timeout_is_ambiguous(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, "timeout")
    task = daemon._submit(str(_task(tmp_path)), False)
    daemon.command_timeout = 0.05
    try:
        asyncio.run(_execute(daemon, task["id"]))
        assert daemon.store.get_task(task["id"])["status"] == "ambiguous"
        queue_event = daemon.store.list_events(task["id"], 20)[0]
        assert queue_event["kind"] == "queue"
        assert queue_event["status"] == "timeout"
    finally:
        _close(daemon)


def test_cancel_during_queue_is_ambiguous(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, "timeout", timeout=5)
    task = daemon._submit(str(_task(tmp_path)), False)

    async def scenario() -> None:
        worker = asyncio.create_task(daemon._run_task_guarded(task["id"]))
        daemon.runs[task["id"]] = ActiveRun(worker)
        while daemon.runs[task["id"]].phase != "sending":
            await asyncio.sleep(0.005)
        await daemon._cancel(task["id"])
        await worker
        daemon.runs.pop(task["id"], None)

    try:
        asyncio.run(scenario())
        assert daemon.store.get_task(task["id"])["status"] == "ambiguous"
    finally:
        _close(daemon)


def test_recovery_and_output_purge_preserve_metadata(tmp_path: Path) -> None:
    store = DaemonStore(tmp_path / "state")
    base = {
        "name": "test",
        "task_dir": str(tmp_path / "task"),
        "thread_id": THREAD_ID,
        "trigger_path": str(tmp_path / "trigger"),
        "message_path": str(tmp_path / "message"),
        "schedule": "* * * * *",
        "timezone": "UTC",
        "lifecycle": "once",
        "mode": "queue-only",
        "next_run_at": "2000-01-01T00:00:00+00:00",
    }
    try:
        store.create_task({**base, "id": "a", "status": "checking"})
        store.create_task({**base, "id": "b", "task_dir": str(tmp_path / "task-b"), "status": "sending"})
        event_id = store.start_event("a", "trigger")
        stdout, stderr = store.event_temp_paths("a", event_id)
        stdout.write_text("retained output", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        event = store.finish_event(
            event_id, status="ok", returncode=0, duration_ms=1, stdout_tmp=stdout, stderr_tmp=stderr
        )
        interrupted_id = store.start_event("b", "queue")
        interrupted_stdout, _ = store.event_temp_paths("b", interrupted_id)
        interrupted_stdout.write_text("partial output", encoding="utf-8")
        store.recover("2001-01-01T00:00:00+00:00")
        assert store.get_task("a")["status"] == "scheduled"
        assert store.get_task("b")["status"] == "ambiguous"
        interrupted = store.get_event(interrupted_id)
        assert interrupted["status"] == "interrupted"
        assert store.read_artifact(interrupted["stdout_path"]) == "partial output"
        assert store.purge_outputs(task_id="a", kind=None, status=None, before=None, confirm=False)["events"] == 1
        store.purge_outputs(task_id="a", kind=None, status=None, before=None, confirm=True)
        purged = store.get_event(event_id)
        assert purged["stdout_path"] is None
        assert purged["stdout_bytes"] == event["stdout_bytes"]
    finally:
        store.close()


def test_daemon_cli_fails_cleanly_when_unavailable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--state-dir", str(tmp_path / "absent")]) == EXIT_SETUP
    assert "cannot contact wake-codex daemon" in capsys.readouterr().err


def test_human_task_list_uses_configured_timezone(capsys: pytest.CaptureFixture[str]) -> None:
    task = {
        "id": THREAD_ID,
        "status": "scheduled",
        "next_run_at": "2026-01-01T01:00:00.123456+00:00",
        "timezone": "Asia/Shanghai",
        "name": "timezone-test",
    }

    _print_tasks([task, {**task, "id": "terminal", "status": "delivered", "next_run_at": None}])

    output = capsys.readouterr().out
    assert "TIMEZONE" in output
    assert "2026-01-01T09:00:00+08:00" in output
    assert "Asia/Shanghai" in output
    assert "2026-01-01T01:00:00.123456+00:00" not in output
    assert "delivered   -" in output


def test_json_task_timestamp_remains_canonical_utc(capsys: pytest.CaptureFixture[str]) -> None:
    timestamp = "2026-01-01T01:00:00.123456+00:00"

    _print_json([{"next_run_at": timestamp, "timezone": "Asia/Shanghai"}])

    assert json.loads(capsys.readouterr().out)[0]["next_run_at"] == timestamp


def test_daemon_socket_submit_schedule_and_list(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    task_dir = _task(tmp_path)

    async def scenario() -> None:
        serving = asyncio.create_task(daemon.serve())
        try:
            for _ in range(200):
                if socket_path(daemon.state_dir).exists():
                    break
                await asyncio.sleep(0.005)
            else:
                raise AssertionError("daemon socket was not created")
            submitted = await asyncio.to_thread(
                request,
                socket_path(daemon.state_dir),
                {"action": "submit", "task_folder": str(task_dir), "force": False},
            )
            daemon.store.update_task(submitted["id"], next_run_at=utc_now())
            for _ in range(400):
                if daemon.store.get_task(submitted["id"])["status"] == "delivered":
                    break
                await asyncio.sleep(0.005)
            else:
                raise AssertionError("scheduled task did not run")
            tasks = await asyncio.to_thread(
                request,
                socket_path(daemon.state_dir),
                {"action": "list", "all": True},
            )
            assert tasks[0]["id"] == submitted["id"]
            assert tasks[0]["status"] == "delivered"
            assert socket_path(daemon.state_dir).stat().st_mode & 0o777 == 0o600
            code, output = await asyncio.to_thread(
                _cli_output,
                ["list", "--all", "--json", "--state-dir", str(daemon.state_dir)],
            )
            assert code == 0
            assert json.loads(output)[0]["status"] == "delivered"
            code, output = await asyncio.to_thread(
                _cli_output,
                [
                    "show",
                    submitted["id"],
                    "--full",
                    "--json",
                    "--state-dir",
                    str(daemon.state_dir),
                ],
            )
            assert code == 0
            shown = json.loads(output)
            assert shown["events"][0]["stdout"].strip() == "queued"
        finally:
            daemon.stop()
            await serving

    asyncio.run(scenario())
