from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path

import pytest

from wake_codex.config import TaskConfig, load_task_config
from wake_codex.cli import main
from wake_codex.runner import (
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_SETUP,
    EXIT_STATE_REFUSED,
    EXIT_TIMEOUT,
    LOCK_FILENAME,
    RunnerSetupError,
    STATE_FILENAME,
    run_task,
)


THREAD_ID = "123e4567-e89b-42d3-a456-426614174000"


def _executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _task(tmp_path: Path, trigger_body: str, message: str = "prompt") -> tuple[Path, TaskConfig]:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        f"version: 1\nname: test\nthread_id: {THREAD_ID}\ntrigger: trigger.sh\nmessage: message.txt\n",
        encoding="utf-8",
    )
    _executable(task_dir / "trigger.sh", f"#!/usr/bin/env bash\nset -u\n{trigger_body}\n")
    (task_dir / "message.txt").write_text(message, encoding="utf-8")
    return task_dir, load_task_config(task_dir)


def _codex(path: Path, body: str, *, loaded_checks: list[bool] | None = None) -> Path:
    checks = [True] if loaded_checks is None else loaded_checks
    check_count = path.with_suffix(".loaded-check-count")
    return _executable(
        path,
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "if sys.argv[1:] == ['queue', '--help']:\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1:3] == ['app-server', 'proxy']:\n"
        "    from pathlib import Path\n"
        f"    count_path = Path({str(check_count)!r})\n"
        "    count = int(count_path.read_text()) if count_path.exists() else 0\n"
        "    count_path.write_text(str(count + 1))\n"
        f"    checks = {checks!r}\n"
        "    loaded = checks[min(count, len(checks) - 1)]\n"
        "    sys.stdin.read()\n"
        "    print(json.dumps({'id': 1, 'result': {}}))\n"
        f"    print(json.dumps({{'id': 2, 'result': {{'data': [{THREAD_ID!r}] if loaded else []}}}}))\n"
        "    raise SystemExit(0)\n"
        f"{body}\n",
    )


def _run(config: TaskConfig, codex: Path, **overrides: object) -> int:
    codex_home = config.task_dir.parent / "codex-home"
    codex_home.mkdir(exist_ok=True)
    session_index = codex_home / "session_index.jsonl"
    if not session_index.exists():
        session_index.write_text(json.dumps({"id": THREAD_ID}) + "\n", encoding="utf-8")
    options = {
        "codex_entry": str(codex),
        "codex_home": str(codex_home),
        "poll_interval": 0.01,
        "timeout": 2.0,
        "command_timeout": 1.0,
        "force": False,
    }
    options.update(overrides)
    return run_task(config, **options)  # type: ignore[arg-type]


def test_retries_trigger_errors_and_blocks_before_delivery(tmp_path: Path) -> None:
    task_dir, config = _task(
        tmp_path,
        "count_file=trigger-count\n"
        "count=$(cat \"$count_file\" 2>/dev/null || echo 0)\n"
        "count=$((count + 1))\n"
        "echo \"$count\" > \"$count_file\"\n"
        "if [[ $count -eq 1 ]]; then exit 2; fi\n"
        "if [[ $count -eq 2 ]]; then exit 1; fi\n"
        "exit 0",
    )
    calls = tmp_path / "calls.jsonl"
    codex = _codex(
        tmp_path / "codex",
        f"import json\nwith open({str(calls)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[2:], ensure_ascii=False) + '\\n')\n",
    )

    assert _run(config, codex) == EXIT_OK
    assert (task_dir / "trigger-count").read_text().strip() == "3"
    assert json.loads(calls.read_text().splitlines()[0]) == [
        "--thread",
        THREAD_ID,
        "--message",
        "prompt",
    ]
    assert not (tmp_path / "codex.loaded-check-count").exists()


def test_default_queue_only_does_not_check_loaded_state(tmp_path: Path) -> None:
    _, config = _task(tmp_path, "exit 0")
    calls = tmp_path / "queue-calls"
    codex = _codex(
        tmp_path / "codex",
        f"open({str(calls)!r}, 'a').write('queue\\n')",
        loaded_checks=[False],
    )

    assert _run(config, codex, silent=1) == EXIT_OK
    assert calls.read_text().splitlines() == ["queue"]
    assert not (tmp_path / "codex.loaded-check-count").exists()


def test_strict_checks_before_trigger_and_queue_and_uses_endpoint(tmp_path: Path) -> None:
    _, config = _task(tmp_path, "exit 0")
    calls = tmp_path / "queue-args.json"
    codex = _codex(
        tmp_path / "codex",
        f"open({str(calls)!r}, 'w').write(json.dumps(sys.argv[2:]))",
        loaded_checks=[True, True],
    )

    assert _run(config, codex, mode="strict", silent=1) == EXIT_OK
    assert int((tmp_path / "codex.loaded-check-count").read_text()) == 2
    assert json.loads(calls.read_text()) == [
        "--remote",
        "unix://",
        "--thread",
        THREAD_ID,
        "--message",
        "prompt",
    ]


@pytest.mark.parametrize("silent", [0, 1])
def test_queue_only_success_note_respects_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], silent: int
) -> None:
    _, config = _task(tmp_path, "exit 0")
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)")

    assert _run(config, codex, silent=silent) == EXIT_OK
    stderr = capsys.readouterr().err
    assert (f"codex resume {THREAD_ID}" in stderr) is (silent == 0)


def test_silent_level_2_hides_poll_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task_dir, config = _task(
        tmp_path,
        "count=$(cat poll-count 2>/dev/null || echo 0)\n"
        "count=$((count + 1))\necho \"$count\" > poll-count\n"
        "if [[ $count -eq 1 ]]; then exit 1; fi\nexit 0",
    )
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)")

    assert _run(config, codex, silent=2) == EXIT_OK
    stderr = capsys.readouterr().err
    assert (task_dir / "poll-count").read_text().strip() == "2"
    assert "trigger poll" not in stderr
    assert "monitoring task" in stderr
    assert "queue attempt" in stderr
    assert "message queued successfully" in stderr


def test_silent_level_3_keeps_only_final_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, config = _task(tmp_path, "exit 0")
    codex = _codex(tmp_path / "codex", "print('queued by fake Codex')")

    assert _run(config, codex, silent=3) == EXIT_OK
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert "message queued successfully" in lines[0]
    assert "queued by fake Codex" in lines[0]


def test_silent_level_3_keeps_timeout_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, config = _task(tmp_path, "exit 1")
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)")

    assert _run(config, codex, silent=3, timeout=0.04) == EXIT_TIMEOUT
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert "overall timeout" in lines[0]


def test_rereads_message_before_each_queue_retry(tmp_path: Path) -> None:
    task_dir, config = _task(tmp_path, "exit 0", message="first prompt\n")
    calls = tmp_path / "calls.jsonl"
    count = tmp_path / "codex-count"
    message_path = task_dir / "message.txt"
    codex = _codex(
        tmp_path / "codex",
        "import json\nfrom pathlib import Path\n"
        f"calls = Path({str(calls)!r})\ncount_path = Path({str(count)!r})\n"
        "count = int(count_path.read_text()) if count_path.exists() else 0\n"
        "count += 1\ncount_path.write_text(str(count))\n"
        "with calls.open('a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[2:], ensure_ascii=False) + '\\n')\n"
        f"if count == 1:\n    Path({str(message_path)!r}).write_text('second prompt\\n', encoding='utf-8')\n    raise SystemExit(9)\n",
    )

    assert _run(config, codex) == EXIT_OK
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert recorded[0][-1] == "first prompt\n"
    assert recorded[1][-1] == "second prompt\n"


def test_completed_state_refuses_restart_and_force_resends(tmp_path: Path) -> None:
    _, config = _task(tmp_path, "exit 0")
    calls = tmp_path / "calls"
    codex = _codex(tmp_path / "codex", f"open({str(calls)!r}, 'a').write('call\\n')\n")

    assert _run(config, codex) == EXIT_OK
    assert _run(config, codex) == EXIT_STATE_REFUSED
    assert _run(config, codex, force=True) == EXIT_OK
    assert calls.read_text().splitlines() == ["call", "call"]


def test_sending_state_is_ambiguous(tmp_path: Path) -> None:
    task_dir, config = _task(tmp_path, "exit 0")
    (task_dir / STATE_FILENAME).write_text(
        json.dumps({"version": 1, "status": "sending", "attempt": 1}),
        encoding="utf-8",
    )
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)")

    assert _run(config, codex) == EXIT_STATE_REFUSED


def test_timeout_while_trigger_blocks(tmp_path: Path) -> None:
    _, config = _task(tmp_path, "exit 1")
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)")

    assert _run(config, codex, timeout=0.04) == EXIT_TIMEOUT


def test_waits_for_user_to_fill_empty_message(tmp_path: Path) -> None:
    task_dir, config = _task(tmp_path, "exit 0", message="")
    calls = tmp_path / "calls"
    codex = _codex(tmp_path / "codex", f"open({str(calls)!r}, 'w').write(sys.argv[-1])\n")
    timer = threading.Timer(0.03, (task_dir / "message.txt").write_text, args=("ready prompt",))
    timer.start()
    try:
        assert _run(config, codex) == EXIT_OK
    finally:
        timer.join()
    assert calls.read_text() == "ready prompt"


def test_queue_timeout_leaves_ambiguous_sending_state(tmp_path: Path) -> None:
    task_dir, config = _task(tmp_path, "exit 0")
    codex = _codex(tmp_path / "codex", "import time\ntime.sleep(10)\n")

    assert _run(config, codex, command_timeout=0.5) == EXIT_STATE_REFUSED
    state = json.loads((task_dir / STATE_FILENAME).read_text())
    assert state["status"] == "sending"
    assert _run(config, codex, command_timeout=0.5) == EXIT_STATE_REFUSED


def test_task_lock_prevents_concurrent_runner(tmp_path: Path) -> None:
    task_dir, config = _task(tmp_path, "exit 0")
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)")
    lock_fd = os.open(task_dir / LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert _run(config, codex) == EXIT_LOCKED
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_missing_session_is_rejected_before_trigger(tmp_path: Path) -> None:
    task_dir, config = _task(tmp_path, "touch trigger-ran\nexit 0")
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)")
    empty_home = tmp_path / "empty-codex-home"
    empty_home.mkdir()
    (empty_home / "session_index.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(RunnerSetupError, match="session does not exist"):
        _run(config, codex, codex_home=str(empty_home))
    assert not (task_dir / "trigger-ran").exists()


def test_archived_session_is_rejected_before_trigger(tmp_path: Path) -> None:
    task_dir, config = _task(tmp_path, "touch trigger-ran\nexit 0")
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)")
    codex_home = tmp_path / "archived-codex-home"
    archived_dir = codex_home / "archived_sessions"
    archived_dir.mkdir(parents=True)
    (codex_home / "session_index.jsonl").write_text(
        json.dumps({"id": THREAD_ID}) + "\n", encoding="utf-8"
    )
    (archived_dir / f"rollout-test-{THREAD_ID}.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RunnerSetupError, match="session is archived"):
        _run(config, codex, codex_home=str(codex_home))
    assert not (task_dir / "trigger-ran").exists()


@pytest.mark.parametrize("archived", [False, True], ids=["missing", "archived"])
def test_cli_session_preflight_errors_exit_2(tmp_path: Path, archived: bool) -> None:
    task_dir, _ = _task(tmp_path, "touch trigger-ran\nexit 0")
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)")
    codex_home = tmp_path / "preflight-codex-home"
    codex_home.mkdir()
    if archived:
        archived_dir = codex_home / "archived_sessions"
        archived_dir.mkdir()
        (archived_dir / f"rollout-test-{THREAD_ID}.jsonl").write_text("{}\n", encoding="utf-8")

    assert main(
        [
            str(task_dir),
            "--codex",
            str(codex),
            "--codex-home",
            str(codex_home),
        ]
    ) == EXIT_SETUP
    assert not (task_dir / "trigger-ran").exists()


def test_not_loaded_session_is_rejected_before_trigger(tmp_path: Path) -> None:
    task_dir, config = _task(tmp_path, "touch trigger-ran\nexit 0")
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)", loaded_checks=[False])

    with pytest.raises(RunnerSetupError, match="not loaded"):
        _run(config, codex, mode="strict")
    assert not (task_dir / "trigger-ran").exists()


def test_session_unloaded_after_trigger_is_not_queued(tmp_path: Path) -> None:
    task_dir, config = _task(tmp_path, "touch trigger-ran\nexit 0")
    queue_calls = tmp_path / "queue-calls"
    codex = _codex(
        tmp_path / "codex",
        f"open({str(queue_calls)!r}, 'a').write('queue\\n')",
        loaded_checks=[True, False],
    )

    with pytest.raises(RunnerSetupError, match="not loaded"):
        _run(config, codex, mode="strict")
    assert (task_dir / "trigger-ran").exists()
    assert not queue_calls.exists()
    assert not (task_dir / STATE_FILENAME).exists()


@pytest.mark.parametrize(
    "error",
    [
        "Error: session is archived",
        "Error: thread is not loaded",
        "Error: session not found",
    ],
)
def test_queue_target_rejection_is_permanent_setup_error(tmp_path: Path, error: str) -> None:
    task_dir, config = _task(tmp_path, "exit 0")
    calls = tmp_path / "queue-calls"
    codex = _codex(
        tmp_path / "codex",
        f"open({str(calls)!r}, 'a').write('queue\\n')\n"
        f"print({error!r}, file=sys.stderr)\nraise SystemExit(2)",
    )

    with pytest.raises(RunnerSetupError, match="permanently rejected"):
        _run(config, codex)
    assert calls.read_text().splitlines() == ["queue"]
    state = json.loads((task_dir / STATE_FILENAME).read_text())
    assert state["status"] == "rejected"


def test_transcript_fallback_validates_session_without_index(tmp_path: Path) -> None:
    _, config = _task(tmp_path, "exit 0")
    codex = _codex(tmp_path / "codex", "raise SystemExit(0)")
    codex_home = tmp_path / "transcript-codex-home"
    transcript_dir = codex_home / "sessions" / "2026" / "08" / "27"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / f"rollout-test-{THREAD_ID}.jsonl").write_text("{}\n", encoding="utf-8")

    assert _run(config, codex, codex_home=str(codex_home)) == EXIT_OK
