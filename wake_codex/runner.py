from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from wake_codex.config import TaskConfig


EXIT_OK = 0
EXIT_SETUP = 2
EXIT_TIMEOUT = 3
EXIT_STATE_REFUSED = 4
EXIT_LOCKED = 5

STATE_FILENAME = ".wake-codex-state.json"
LOCK_FILENAME = ".wake-codex.lock"
SESSION_INDEX_FILENAME = "session_index.jsonl"
KNOWN_STATES = {"sending", "delivered", "retrying", "rejected"}
MODES = {"queue-only", "strict"}
STATE_DB_SOURCE_KINDS = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]
_MISSING_THREAD_ERROR = re.compile(
    r"(?:thread|session).{0,160}(?:not[ _-]found|not[ _-]loaded|does not exist)"
    r"|(?:not[ _-]found|not[ _-]loaded|does not exist).{0,160}(?:thread|session)"
    r"|no rollout found for thread id",
    re.IGNORECASE | re.DOTALL,
)


class RunnerSetupError(ValueError):
    pass


class SessionCheckTimeout(RunnerSetupError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


def _timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(message: str, *, silent: int = 0, hide_at: int | None = None) -> None:
    if hide_at is not None and silent >= hide_at:
        return
    print(f"[{_timestamp()}] {message}", file=sys.stderr, flush=True)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
        stdin=subprocess.PIPE if input_text is not None else None,
        env=env,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
        return CommandResult(None, stdout, stderr, timed_out=True)
    except BaseException:
        _terminate_process_group(process)
        raise
    return CommandResult(process.returncode, stdout, stderr)


class TaskLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            return False
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"pid={os.getpid()} started_at={_timestamp()}\n".encode())
        os.fsync(self.fd)
        return True

    def close(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> "TaskLock":
        if not self.acquire():
            raise RuntimeError("lock already held")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _read_state(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunnerSetupError(f"cannot read state file {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("status") not in KNOWN_STATES:
        raise RunnerSetupError(f"invalid state file: {path}")
    return data


def _write_state(path: Path, data: dict[str, object]) -> None:
    payload = {**data, "updated_at": _timestamp()}
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _command_output(result: CommandResult) -> str:
    parts = []
    if result.stdout.strip():
        parts.append(f"stdout={result.stdout.strip()!r}")
    if result.stderr.strip():
        parts.append(f"stderr={result.stderr.strip()!r}")
    return " ".join(parts)


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _effective_timeout(command_timeout: float, deadline: float | None) -> float | None:
    remaining = _remaining(deadline)
    if remaining is not None and remaining <= 0:
        return None
    return command_timeout if remaining is None else min(command_timeout, remaining)


def _wait(interval: float, deadline: float | None) -> bool:
    remaining = _remaining(deadline)
    if remaining is not None and remaining <= 0:
        return False
    time.sleep(interval if remaining is None else min(interval, remaining))
    return _remaining(deadline) is None or _remaining(deadline) > 0


def _resolve_codex(codex_entry: str) -> Path:
    path = Path(codex_entry).expanduser().resolve()
    if not path.is_file():
        raise RunnerSetupError(f"Codex entry is not a file: {path}")
    if not os.access(path, os.X_OK):
        raise RunnerSetupError(f"Codex entry is not executable: {path}")
    return path


def _resolve_codex_home(codex_home: str | None) -> Path:
    raw_path = codex_home or os.environ.get("CODEX_HOME")
    path = Path(raw_path).expanduser().resolve() if raw_path else (Path.home() / ".codex").resolve()
    if not path.is_dir():
        source = "--codex-home/CODEX_HOME" if raw_path else "default Codex home"
        raise RunnerSetupError(f"{source} is not a directory: {path}")
    return path


def _transcript_exists(directory: Path, thread_id: str, *, recursive: bool) -> bool:
    filename_pattern = f"*-{thread_id}.jsonl"
    try:
        if not directory.is_dir():
            return False
        matches = directory.rglob(filename_pattern) if recursive else directory.glob(filename_pattern)
        return next(matches, None) is not None
    except OSError as exc:
        raise RunnerSetupError(f"cannot inspect Codex session transcripts in {directory}: {exc}") from exc


def _legacy_session_state(codex_home: Path, thread_id: str) -> tuple[str, int]:
    archived_dir = codex_home / "archived_sessions"
    if _transcript_exists(archived_dir, thread_id, recursive=True):
        return "archived", 0

    if _transcript_exists(codex_home / "sessions", thread_id, recursive=True):
        return "active", 0

    index_path = codex_home / SESSION_INDEX_FILENAME
    malformed_lines = 0
    if index_path.is_file():
        try:
            with index_path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_lines += 1
                        continue
                    if isinstance(item, dict) and item.get("id") == thread_id:
                        return "active", malformed_lines
        except (OSError, UnicodeError) as exc:
            raise RunnerSetupError(f"cannot read Codex session index {index_path}: {exc}") from exc

    return "missing", malformed_lines


class _AppServerClient:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
    ) -> None:
        try:
            self.process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
            )
        except OSError as exc:
            raise RunnerSetupError(f"cannot start Codex app-server session lookup: {exc}") from exc
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        self.stdout_buffer = bytearray()
        self.stderr = bytearray()
        self.next_id = 1
        self.deadline = time.monotonic() + timeout

    def close(self) -> None:
        try:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _terminate_process_group(self.process)
        finally:
            self.selector.close()
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()

    def __enter__(self) -> "_AppServerClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._write({"method": method, "params": params})

    def request(self, method: str, params: dict[str, object]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._write({"method": method, "id": request_id, "params": params})
        while True:
            response = self._next_response()
            if response.get("id") == request_id:
                return response

    def error_details(self) -> str:
        text = self.stderr.decode("utf-8", errors="replace").strip()
        return f": {text}" if text else ""

    def _write(self, payload: dict[str, object]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RunnerSetupError(
                f"Codex app-server closed during session lookup{self.error_details()}"
            ) from exc

    def _next_response(self) -> dict[str, Any]:
        while True:
            line = self._pop_stdout_line()
            if line is not None:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RunnerSetupError(
                        f"invalid app-server response during session lookup: {exc}"
                    ) from exc
                if isinstance(value, dict):
                    return value

            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RunnerSetupError("Codex app-server session lookup timed out")
            events = self.selector.select(remaining)
            if not events:
                raise RunnerSetupError("Codex app-server session lookup timed out")
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except OSError as exc:
                    raise RunnerSetupError(f"cannot read Codex app-server response: {exc}") from exc
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    self.stdout_buffer.extend(chunk)
                elif len(self.stderr) < 65536:
                    self.stderr.extend(chunk[: 65536 - len(self.stderr)])
            if not self.selector.get_map():
                raise RunnerSetupError(
                    f"Codex app-server returned no session lookup response{self.error_details()}"
                )

    def _pop_stdout_line(self) -> bytes | None:
        newline = self.stdout_buffer.find(b"\n")
        if newline < 0:
            return None
        line = bytes(self.stdout_buffer[:newline])
        del self.stdout_buffer[: newline + 1]
        return line


def _rpc_result(response: dict[str, Any], method: str) -> dict[str, Any]:
    error = response.get("error")
    if error is not None:
        raise RunnerSetupError(f"Codex app-server {method} rejected session lookup: {error!r}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RunnerSetupError(f"invalid Codex app-server {method} response")
    return result


def _is_missing_thread_error(response: dict[str, Any]) -> bool:
    error = response.get("error")
    return error is not None and _MISSING_THREAD_ERROR.search(str(error)) is not None


def _state_db_has_archived_thread(client: _AppServerClient, thread_id: str) -> bool:
    cursor: str | None = None
    while True:
        result = _rpc_result(
            client.request(
                "thread/list",
                {
                    "archived": True,
                    "cursor": cursor,
                    "limit": 1000,
                    "sourceKinds": STATE_DB_SOURCE_KINDS,
                    "useStateDbOnly": True,
                },
            ),
            "thread/list",
        )
        data = result.get("data")
        if not isinstance(data, list):
            raise RunnerSetupError("invalid Codex app-server thread/list data")
        for item in data:
            if isinstance(item, dict) and item.get("id") == thread_id:
                return True
        next_cursor = result.get("nextCursor")
        if next_cursor is None:
            return False
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            raise RunnerSetupError("invalid Codex app-server thread/list pagination cursor")
        cursor = next_cursor


def _validate_session_exists(
    codex: Path,
    codex_home: Path,
    endpoint: str,
    mode: str,
    thread_id: str,
    cwd: Path,
    timeout: float,
) -> None:
    legacy_state, malformed_lines = _legacy_session_state(codex_home, thread_id)
    if legacy_state == "archived":
        raise RunnerSetupError(f"Codex session is archived and cannot receive queue messages: {thread_id}")

    command = (
        _proxy_command(codex, endpoint)
        if mode == "strict"
        else [str(codex), "app-server", "--stdio"]
    )
    with _AppServerClient(
        command,
        cwd=cwd,
        env=_codex_env(codex_home),
        timeout=timeout,
    ) as client:
        _rpc_result(
            client.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "wake-codex",
                        "title": "wake-codex",
                        "version": "0.1.0",
                    }
                },
            ),
            "initialize",
        )
        client.notify("initialized", {})
        read_response = client.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        state_db_exists = False
        if "error" in read_response:
            if not _is_missing_thread_error(read_response):
                _rpc_result(read_response, "thread/read")
        else:
            result = _rpc_result(read_response, "thread/read")
            thread = result.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                raise RunnerSetupError("invalid Codex app-server thread/read data")
            state_db_exists = True

        if state_db_exists and _state_db_has_archived_thread(client, thread_id):
            raise RunnerSetupError(
                f"Codex session is archived and cannot receive queue messages: {thread_id}"
            )

    if state_db_exists or legacy_state == "active":
        return

    if malformed_lines:
        raise RunnerSetupError(
            f"Codex session does not exist in {codex_home}: {thread_id} "
            f"({malformed_lines} malformed session index line(s) were ignored)"
        )
    raise RunnerSetupError(f"Codex session does not exist in {codex_home}: {thread_id}")


def _codex_env(codex_home: Path) -> dict[str, str]:
    return {**os.environ, "CODEX_HOME": str(codex_home)}


def _proxy_command(codex: Path, endpoint: str) -> list[str]:
    if endpoint == "unix://":
        return [str(codex), "app-server", "proxy"]
    if endpoint.startswith("unix:///"):
        return [str(codex), "app-server", "proxy", "--sock", endpoint.removeprefix("unix://")]
    raise RunnerSetupError(
        "--app-server-endpoint must be unix:// or unix:///absolute/path; "
        "wake-codex requires the supported app-server proxy for fail-closed loaded checks"
    )


def _preflight_codex(
    codex: Path,
    codex_home: Path,
    mode: str,
    endpoint: str,
    cwd: Path,
    command_timeout: float,
) -> None:
    if mode not in MODES:
        raise RunnerSetupError(f"unsupported mode: {mode}")
    if mode == "strict":
        _proxy_command(codex, endpoint)
    result = run_command(
        [str(codex), "queue", "--help"],
        cwd=cwd,
        timeout=command_timeout,
        env=_codex_env(codex_home),
    )
    if result.timed_out:
        raise RunnerSetupError("Codex queue preflight timed out")
    if result.returncode != 0:
        details = _command_output(result)
        raise RunnerSetupError(f"Codex entry does not support queue{': ' + details if details else ''}")


def _require_session_loaded(
    codex: Path,
    codex_home: Path,
    endpoint: str,
    thread_id: str,
    cwd: Path,
    timeout: float,
) -> None:
    requests = [
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {"name": "wake-codex", "title": "wake-codex", "version": "0.1.0"}
            },
        },
        {"method": "initialized", "params": {}},
        {"method": "thread/loaded/list", "id": 2},
    ]
    input_text = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in requests)
    result = run_command(
        _proxy_command(codex, endpoint),
        cwd=cwd,
        timeout=timeout,
        input_text=input_text,
        env=_codex_env(codex_home),
    )
    if result.timed_out:
        raise SessionCheckTimeout(
            f"cannot confirm that Codex session is loaded: app-server check timed out ({endpoint})"
        )
    if result.returncode != 0:
        details = _command_output(result)
        raise RunnerSetupError(
            f"cannot confirm that Codex session is loaded via {endpoint}"
            f"{': ' + details if details else ''}"
        )

    response: object | None = None
    try:
        for line in result.stdout.splitlines():
            candidate = json.loads(line)
            if isinstance(candidate, dict) and candidate.get("id") == 2:
                response = candidate
                break
    except json.JSONDecodeError as exc:
        raise RunnerSetupError(f"invalid app-server response while checking loaded sessions: {exc}") from exc

    if not isinstance(response, dict):
        raise RunnerSetupError("cannot confirm that Codex session is loaded: app-server returned no response")
    if "error" in response:
        raise RunnerSetupError(
            "cannot confirm that Codex session is loaded: "
            f"thread/loaded/list was rejected: {response['error']!r}"
        )
    data = response.get("result")
    loaded = data.get("data") if isinstance(data, dict) else None
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise RunnerSetupError("cannot confirm that Codex session is loaded: invalid thread/loaded/list result")
    if thread_id not in loaded:
        raise RunnerSetupError(
            f"Codex session exists but is not loaded by the app-server at {endpoint}: {thread_id}"
        )


_PERMANENT_QUEUE_ERROR = re.compile(
    r"(?:thread|session).{0,160}(?:archived|not(?: currently)?[ _-]loaded|not[ _-]found|does not exist)"
    r"|(?:archived|not(?: currently)?[ _-]loaded|not[ _-]found|does not exist).{0,160}(?:thread|session)"
    r"|no rollout found for thread id",
    re.IGNORECASE | re.DOTALL,
)


def _is_permanent_queue_error(result: CommandResult) -> bool:
    return _PERMANENT_QUEUE_ERROR.search(f"{result.stdout}\n{result.stderr}") is not None


def _read_message(path: Path) -> tuple[str | None, str | None]:
    try:
        message = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, str(exc)
    if not message.strip():
        return None, "message file is empty or whitespace-only"
    return message, None


def _format_message(name: str, task_id: str, message: str) -> str:
    return f"[wake-codex: {name} | {task_id[:8]}] {message}"


def _base_state(
    config: TaskConfig, status: str, attempt: int, *, task_id: str
) -> dict[str, object]:
    return {
        "version": 1,
        "status": status,
        "task": config.name,
        "task_id": task_id,
        "thread_id": config.thread_id,
        "attempt": attempt,
    }


def _delivery_loop(
    config: TaskConfig,
    codex: Path,
    codex_home: Path,
    endpoint: str,
    mode: str,
    silent: int,
    state_path: Path,
    *,
    poll_interval: float,
    command_timeout: float,
    deadline: float | None,
    initial_attempt: int,
    task_id: str,
) -> int:
    attempt = initial_attempt
    while True:
        effective_timeout = _effective_timeout(command_timeout, deadline)
        if effective_timeout is None:
            log("overall timeout reached before message delivery")
            return EXIT_TIMEOUT

        message, message_error = _read_message(config.message_path)
        if message is None:
            log(f"message unavailable; retrying: {message_error}", silent=silent, hide_at=2)
            if not _wait(poll_interval, deadline):
                log("overall timeout reached while waiting for a valid message")
                return EXIT_TIMEOUT
            continue

        if mode == "strict":
            try:
                _require_session_loaded(
                    codex,
                    codex_home,
                    endpoint,
                    config.thread_id,
                    config.task_dir,
                    effective_timeout,
                )
            except SessionCheckTimeout:
                if deadline is not None and _remaining(deadline) <= 0:
                    log("overall timeout reached while checking session before message delivery")
                    return EXIT_TIMEOUT
                raise

        attempt += 1
        message = _format_message(config.name, task_id, message)
        message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        sending_state = {
            **_base_state(config, "sending", attempt, task_id=task_id),
            "message_sha256": message_hash,
            "started_at": _timestamp(),
        }
        _write_state(state_path, sending_state)
        log(
            f"queue attempt {attempt} for thread {config.thread_id} "
            f"(message sha256 {message_hash[:12]})",
            silent=silent,
            hide_at=3,
        )
        queue_command = [str(codex), "queue"]
        if mode == "strict":
            queue_command.extend(["--remote", endpoint])
        queue_command.extend(["--thread", config.thread_id, "--message", message])
        result = run_command(
            queue_command,
            cwd=config.task_dir,
            timeout=effective_timeout,
            env=_codex_env(codex_home),
        )
        if result.timed_out:
            log("Codex queue timed out; delivery result is ambiguous and automatic retry is disabled")
            return EXIT_STATE_REFUSED
        if result.returncode == 0:
            _write_state(
                state_path,
                {
                    **_base_state(config, "delivered", attempt, task_id=task_id),
                    "message_sha256": message_hash,
                    "delivered_at": _timestamp(),
                },
            )
            details = _command_output(result)
            log(f"message queued successfully{': ' + details if details else ''}")
            if mode == "queue-only":
                log(
                    "note: 消息只有在目标会话存在活跃 Codex 进程时才会执行；"
                    f"否则请运行 codex resume {config.thread_id}",
                    silent=silent,
                    hide_at=1,
                )
            return EXIT_OK

        details = _command_output(result)
        if _is_permanent_queue_error(result):
            _write_state(
                state_path,
                {
                    **_base_state(config, "rejected", attempt, task_id=task_id),
                    "message_sha256": message_hash,
                    "last_returncode": result.returncode,
                    "reason": "target session was archived, not loaded, or not found",
                },
            )
            raise RunnerSetupError(
                f"Codex queue permanently rejected the target session"
                f"{': ' + details if details else ''}"
            )
        _write_state(
            state_path,
            {
                **_base_state(config, "retrying", attempt, task_id=task_id),
                "message_sha256": message_hash,
                "last_returncode": result.returncode,
            },
        )
        log(
            f"Codex queue failed with exit {result.returncode}; retrying"
            f"{': ' + details if details else ''}",
            silent=silent,
            hide_at=2,
        )
        if not _wait(poll_interval, deadline):
            log("overall timeout reached after a failed queue attempt")
            return EXIT_TIMEOUT


def run_task(
    config: TaskConfig,
    *,
    codex_entry: str,
    codex_home: str | None,
    app_server_endpoint: str = "unix://",
    mode: str = "queue-only",
    silent: int = 0,
    poll_interval: float,
    timeout: float,
    command_timeout: float,
    force: bool,
) -> int:
    if isinstance(silent, bool) or not isinstance(silent, int) or silent not in range(4):
        raise RunnerSetupError("silent level must be an integer from 0 to 3")
    codex = _resolve_codex(codex_entry)
    resolved_codex_home = _resolve_codex_home(codex_home)
    _preflight_codex(
        codex,
        resolved_codex_home,
        mode,
        app_server_endpoint,
        config.task_dir,
        command_timeout,
    )
    _validate_session_exists(
        codex,
        resolved_codex_home,
        app_server_endpoint,
        mode,
        config.thread_id,
        config.task_dir,
        command_timeout,
    )
    log(
        f"validated Codex session {config.thread_id} in state DB/legacy storage",
        silent=silent,
        hide_at=3,
    )
    state_path = config.task_dir / STATE_FILENAME
    lock = TaskLock(config.task_dir / LOCK_FILENAME)
    if not lock.acquire():
        log(f"another runner already holds the task lock: {config.task_dir}")
        return EXIT_LOCKED

    try:
        state = _read_state(state_path)
        if state and state["status"] in {"delivered", "sending", "rejected"} and not force:
            log(f"task state is {state['status']}; use --force only after checking for duplicate delivery")
            return EXIT_STATE_REFUSED
        if force and state:
            log(
                f"ignoring prior {state['status']} state because --force was supplied",
                silent=silent,
                hide_at=3,
            )
            state = None

        task_id = str((state or {}).get("task_id") or uuid.uuid4())
        deadline = None if timeout == -1 else time.monotonic() + timeout
        if state and state["status"] == "retrying":
            log(
                "resuming message delivery after a previously failed queue attempt",
                silent=silent,
                hide_at=3,
            )
            return _delivery_loop(
                config,
                codex,
                resolved_codex_home,
                app_server_endpoint,
                mode,
                silent,
                state_path,
                poll_interval=poll_interval,
                command_timeout=command_timeout,
                deadline=deadline,
                initial_attempt=int(state.get("attempt", 0)),
                task_id=task_id,
            )

        poll_count = 0
        log(
            f"monitoring task {config.name!r}; timeout={'none' if timeout == -1 else timeout}",
            silent=silent,
            hide_at=3,
        )
        while True:
            effective_timeout = _effective_timeout(command_timeout, deadline)
            if effective_timeout is None:
                log("overall timeout reached while waiting for trigger")
                return EXIT_TIMEOUT
            poll_count += 1
            if mode == "strict":
                try:
                    _require_session_loaded(
                        codex,
                        resolved_codex_home,
                        app_server_endpoint,
                        config.thread_id,
                        config.task_dir,
                        effective_timeout,
                    )
                except SessionCheckTimeout:
                    if deadline is not None and _remaining(deadline) <= 0:
                        log("overall timeout reached while checking session before trigger")
                        return EXIT_TIMEOUT
                    raise
            result = run_command([str(config.trigger_path)], cwd=config.task_dir, timeout=effective_timeout)
            details = _command_output(result)
            if result.timed_out:
                log(
                    f"trigger poll {poll_count} timed out; retrying"
                    f"{': ' + details if details else ''}",
                    silent=silent,
                    hide_at=2,
                )
            elif result.returncode == 0:
                log(
                    f"trigger poll {poll_count} returned go{': ' + details if details else ''}",
                    silent=silent,
                    hide_at=2,
                )
                return _delivery_loop(
                    config,
                    codex,
                    resolved_codex_home,
                    app_server_endpoint,
                    mode,
                    silent,
                    state_path,
                    poll_interval=poll_interval,
                    command_timeout=command_timeout,
                    deadline=deadline,
                    initial_attempt=0,
                    task_id=task_id,
                )
            elif result.returncode == 1:
                log(
                    f"trigger poll {poll_count} returned block{': ' + details if details else ''}",
                    silent=silent,
                    hide_at=2,
                )
            else:
                log(
                    f"trigger poll {poll_count} failed with exit {result.returncode}; retrying"
                    f"{': ' + details if details else ''}",
                    silent=silent,
                    hide_at=2,
                )
            if not _wait(poll_interval, deadline):
                log("overall timeout reached while waiting for trigger")
                return EXIT_TIMEOUT
    finally:
        lock.close()
