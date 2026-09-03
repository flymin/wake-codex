from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wake_codex.config import ConfigError, TaskConfig, load_task_config
from wake_codex.daemon_paths import socket_path
from wake_codex.daemon_store import ACTIVE_STATUSES, DaemonStore, StoreError
from wake_codex.runner import (
    LOCK_FILENAME,
    STATE_FILENAME,
    CommandResult,
    RunnerSetupError,
    TaskLock,
    _base_state,
    _codex_env,
    _is_permanent_queue_error,
    _preflight_codex,
    _proxy_command,
    _read_message,
    _read_state,
    _resolve_codex,
    _resolve_codex_home,
    _validate_session_exists,
    _write_state,
)
from wake_codex.schedule import local_timezone_name, next_run_at, utc_now


class DaemonError(RuntimeError):
    pass


@dataclass
class ActiveRun:
    task: asyncio.Task[None]
    phase: str = "checking"
    process: asyncio.subprocess.Process | None = None


class WakeDaemon:
    def __init__(
        self,
        *,
        state_dir: Path,
        codex_entry: str,
        codex_home: str | None,
        app_server_endpoint: str,
        command_timeout: float,
        retry_interval: float,
        max_workers: int,
        tick_interval: float = 0.5,
    ) -> None:
        self.state_dir = state_dir
        self.socket_path = socket_path(state_dir)
        self.codex = _resolve_codex(codex_entry)
        self.codex_home = _resolve_codex_home(codex_home)
        self.endpoint = app_server_endpoint
        self.command_timeout = command_timeout
        self.retry_interval = retry_interval
        self.max_workers = max_workers
        self.tick_interval = tick_interval
        self.store = DaemonStore(state_dir)
        self.semaphore = asyncio.Semaphore(max_workers)
        self.runs: dict[str, ActiveRun] = {}
        self.locks: dict[str, TaskLock] = {}
        self.server: asyncio.Server | None = None
        self.stop_event = asyncio.Event()
        self.daemon_lock = TaskLock(state_dir / "daemon.lock")

    async def serve(self) -> None:
        if not self.daemon_lock.acquire():
            raise DaemonError(f"another daemon already owns {self.state_dir}")
        try:
            _preflight_codex(
                self.codex,
                self.codex_home,
                "queue-only",
                self.endpoint,
                self.state_dir,
                self.command_timeout,
            )
            self.store.recover(utc_now())
            self._restore_locks()
            self.socket_path.unlink(missing_ok=True)
            try:
                self.server = await asyncio.start_unix_server(
                    self._handle_client, path=str(self.socket_path), limit=1024 * 1024
                )
            except OSError as exc:
                raise DaemonError(f"cannot create daemon socket {self.socket_path}: {exc}") from exc
            os.chmod(self.socket_path, 0o600)
            print(f"wake-codex daemon listening on {self.socket_path}", file=sys.stderr, flush=True)
            scheduler = asyncio.create_task(self._scheduler(), name="scheduler")
            await self.stop_event.wait()
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
            await self._shutdown_runs()
        finally:
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
            self.socket_path.unlink(missing_ok=True)
            for lock in self.locks.values():
                lock.close()
            self.locks.clear()
            self.store.close()
            self.daemon_lock.close()

    def stop(self) -> None:
        self.stop_event.set()

    def _restore_locks(self) -> None:
        for task in self.store.list_tasks():
            lock = TaskLock(Path(task["task_dir"]) / LOCK_FILENAME)
            if lock.acquire():
                self.locks[task["id"]] = lock
            else:
                self.store.update_task(
                    task["id"],
                    status="rejected",
                    next_run_at=None,
                    last_error="task folder is locked by another runner",
                )

    async def _scheduler(self) -> None:
        while True:
            available = max(0, self.max_workers - len(self.runs))
            if available:
                for task in self.store.due_tasks(utc_now(), available):
                    task_id = task["id"]
                    if task_id in self.runs:
                        continue
                    worker = asyncio.create_task(
                        self._run_task_guarded(task_id), name=f"task-{task_id}"
                    )
                    self.runs[task_id] = ActiveRun(worker)
                    worker.add_done_callback(lambda _done, ident=task_id: self.runs.pop(ident, None))
            await asyncio.sleep(self.tick_interval)

    async def _run_task_guarded(self, task_id: str) -> None:
        try:
            await self._run_task(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task = self.store.get_task(task_id)
            if task["status"] in ACTIVE_STATUSES:
                status = "ambiguous" if task["status"] in {"sending", "cancelling"} else "rejected"
                self._terminal(task_id, status, f"task execution failed: {exc}")

    async def _run_task(self, task_id: str) -> None:
        async with self.semaphore:
            task = self.store.get_task(task_id)
            if task["cancel_requested"]:
                self._terminal(task_id, "cancelled", "cancelled before execution")
                return
            if task["status"] == "retrying":
                await self._deliver(task_id)
                return
            self.store.update_task(task_id, status="checking", next_run_at=None)
            if task["mode"] == "strict":
                result = await self._strict_check(task_id, "strict-before-trigger")
                if result is None:
                    return
            result, event = await self._command_event(
                task_id,
                "trigger",
                [task["trigger_path"]],
                Path(task["task_dir"]),
                phase="checking",
                event_statuses={0: "ok", 1: "block"},
            )
            if self._cancelled(task_id):
                self._terminal(task_id, "cancelled", "cancelled during trigger check")
            elif result.timed_out:
                self._schedule(task_id, "trigger-timeout", "trigger command timed out")
            elif result.returncode == 0:
                self.store.update_task(task_id, last_result="go")
                if task["lifecycle"] == "continuous" and not task["armed"]:
                    self._schedule(task_id, "go-disarmed", None)
                else:
                    await self._deliver(task_id)
            elif result.returncode == 1:
                values: dict[str, Any] = {}
                if task["lifecycle"] == "continuous":
                    values["armed"] = 1
                self._schedule(task_id, "block", None, **values)
            else:
                self._schedule(
                    task_id,
                    "trigger-error",
                    f"trigger exited with {result.returncode}; event {event['id']}",
                )

    async def _strict_check(self, task_id: str, kind: str) -> CommandResult | None:
        task = self.store.get_task(task_id)
        requests = [
            {"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "wake-codex", "version": "0.1.0"}}},
            {"method": "initialized", "params": {}},
            {"method": "thread/loaded/list", "id": 2},
        ]
        stdin = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in requests)
        result, event = await self._command_event(
            task_id,
            kind,
            _proxy_command(self.codex, self.endpoint),
            Path(task["task_dir"]),
            phase="checking",
            stdin=stdin,
            env=_codex_env(self.codex_home),
        )
        if self._cancelled(task_id):
            self._terminal(task_id, "cancelled", "cancelled during strict loaded check")
            return None
        error: str | None = None
        if result.timed_out:
            error = "strict loaded check timed out"
        elif result.returncode != 0:
            error = f"strict loaded check exited with {result.returncode}"
        else:
            try:
                responses = [json.loads(line) for line in result.stdout.splitlines()]
                response = next(item for item in responses if isinstance(item, dict) and item.get("id") == 2)
                loaded = response.get("result", {}).get("data")
                if not isinstance(loaded, list) or task["thread_id"] not in loaded:
                    error = "target session is not loaded by the configured app-server"
            except (json.JSONDecodeError, StopIteration, AttributeError):
                error = "invalid thread/loaded/list response"
        if error:
            self._terminal(task_id, "rejected", f"{error}; event {event['id']}")
            return None
        return result

    async def _deliver(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task["mode"] == "strict":
            result = await self._strict_check(task_id, "strict-before-queue")
            if result is None:
                return
        if self._cancelled(task_id):
            self._terminal(task_id, "cancelled", "cancelled before queue")
            return
        message, error = _read_message(Path(task["message_path"]))
        if message is None:
            self._schedule_retry(task_id, f"message unavailable: {error}")
            return
        message_hash = hashlib.sha256(message.encode()).hexdigest()
        state_path = Path(task["task_dir"]) / STATE_FILENAME
        attempt = self._legacy_attempt(state_path) + 1
        _write_state(
            state_path,
            {
                **self._task_state(task, "sending", attempt),
                "message_sha256": message_hash,
                "started_at": utc_now(),
            },
        )
        self.store.update_task(task_id, status="sending")
        command = [str(self.codex), "queue"]
        if task["mode"] == "strict":
            command.extend(["--remote", self.endpoint])
        command.extend(["--thread", task["thread_id"], "--message", message])
        result, event = await self._command_event(
            task_id,
            "queue",
            command,
            Path(task["task_dir"]),
            phase="sending",
            env=_codex_env(self.codex_home),
        )
        if result.timed_out or self._cancelled(task_id):
            reason = "queue command timed out" if result.timed_out else "cancelled during queue delivery"
            self._terminal(task_id, "ambiguous", f"{reason}; event {event['id']}")
            return
        if result.returncode == 0:
            _write_state(
                state_path,
                {
                    **self._task_state(task, "delivered", attempt),
                    "message_sha256": message_hash,
                    "delivered_at": utc_now(),
                },
            )
            count = int(task["delivery_count"]) + 1
            if task["lifecycle"] == "continuous":
                self._schedule(task_id, "delivered", None, armed=0, delivery_count=count)
            else:
                self._terminal(task_id, "delivered", None, delivery_count=count)
            return
        if _is_permanent_queue_error(result):
            _write_state(
                state_path,
                {
                    **self._task_state(task, "rejected", attempt),
                    "message_sha256": message_hash,
                    "last_returncode": result.returncode,
                    "reason": "target session was archived, not loaded, or not found",
                },
            )
            self._terminal(task_id, "rejected", f"queue permanently rejected target; event {event['id']}")
        else:
            _write_state(
                state_path,
                {
                    **self._task_state(task, "retrying", attempt),
                    "message_sha256": message_hash,
                    "last_returncode": result.returncode,
                },
            )
            self._schedule_retry(task_id, f"queue exited with {result.returncode}; event {event['id']}")

    async def _command_event(
        self,
        task_id: str,
        kind: str,
        argv: list[str],
        cwd: Path,
        *,
        phase: str,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
        event_statuses: dict[int, str] | None = None,
    ) -> tuple[CommandResult, dict[str, Any]]:
        event_id = self.store.start_event(task_id, kind)
        stdout_tmp, stderr_tmp = self.store.event_temp_paths(task_id, event_id)
        started = time.monotonic()
        active = self.runs[task_id]
        active.phase = phase
        try:
            with stdout_tmp.open("wb") as stdout_handle, stderr_tmp.open("wb") as stderr_handle:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    env=env,
                    start_new_session=True,
                )
                active.process = process
                timed_out = False
                try:
                    if self._cancelled(task_id):
                        await self._terminate(process)
                    elif stdin is not None and process.stdin is not None:
                        process.stdin.write(stdin.encode())
                        await process.stdin.drain()
                        process.stdin.close()
                        await asyncio.wait_for(process.wait(), timeout=self.command_timeout)
                    else:
                        await asyncio.wait_for(process.wait(), timeout=self.command_timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    await self._terminate(process)
                except asyncio.CancelledError:
                    await self._terminate(process)
                    raise
                finally:
                    active.process = None
        except OSError as exc:
            stderr_tmp.write_text(f"cannot execute {argv[0]}: {exc}\n", encoding="utf-8")
            result = CommandResult(127, "", str(exc))
            event = self.store.finish_event(
                event_id,
                status="error",
                returncode=127,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout_tmp=stdout_tmp,
                stderr_tmp=stderr_tmp,
            )
            return result, event
        stdout = stdout_tmp.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_tmp.read_text(encoding="utf-8", errors="replace")
        result = CommandResult(None if timed_out else process.returncode, stdout, stderr, timed_out)
        if timed_out:
            event_status = "timeout"
        elif event_statuses is not None and result.returncode in event_statuses:
            event_status = event_statuses[result.returncode]
        else:
            event_status = "ok" if result.returncode == 0 else "error"
        event = self.store.finish_event(
            event_id,
            status=event_status,
            returncode=result.returncode,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout_tmp=stdout_tmp,
            stderr_tmp=stderr_tmp,
        )
        return result, event

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    def _schedule(self, task_id: str, result: str, error: str | None, **values: Any) -> None:
        task = self.store.get_task(task_id)
        self.store.update_task(
            task_id,
            status="scheduled",
            next_run_at=next_run_at(task["schedule"], task["timezone"]),
            last_result=result,
            last_error=error,
            cancel_requested=0,
            **values,
        )

    def _schedule_retry(self, task_id: str, error: str) -> None:
        due = datetime.fromtimestamp(time.time() + self.retry_interval, timezone.utc).isoformat()
        self.store.update_task(task_id, status="retrying", next_run_at=due, last_error=error)

    def _terminal(self, task_id: str, status: str, error: str | None, **values: Any) -> None:
        self.store.update_task(
            task_id, status=status, next_run_at=None, last_error=error, **values
        )
        lock = self.locks.pop(task_id, None)
        if lock:
            lock.close()

    def _cancelled(self, task_id: str) -> bool:
        return bool(self.store.get_task(task_id)["cancel_requested"])

    @staticmethod
    def _legacy_attempt(path: Path) -> int:
        try:
            state = _read_state(path)
        except RunnerSetupError:
            return 0
        return int(state.get("attempt", 0)) if state else 0

    @staticmethod
    def _task_state(task: dict[str, Any], status: str, attempt: int) -> dict[str, object]:
        config = TaskConfig(
            task_dir=Path(task["task_dir"]),
            config_path=Path(task["task_dir"]) / "task.yaml",
            name=task["name"],
            thread_id=task["thread_id"],
            trigger_path=Path(task["trigger_path"]),
            message_path=Path(task["message_path"]),
        )
        return _base_state(config, status, attempt)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            request = json.loads(line)
            if not isinstance(request, dict):
                raise DaemonError("request must be an object")
            result = await self._dispatch(request)
            response = {"ok": True, "result": result}
        except (DaemonError, StoreError, ConfigError, RunnerSetupError, ValueError, json.JSONDecodeError) as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception as exc:
            response = {"ok": False, "error": f"internal daemon error: {exc}"}
        writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> Any:
        action = request.get("action")
        if action == "submit":
            return self._submit(str(request.get("task_folder", "")), bool(request.get("force")))
        if action == "list":
            return self.store.list_tasks(bool(request.get("all")))
        if action == "show":
            return self._show(str(request.get("task_id", "")), bool(request.get("full")))
        if action == "events":
            return self._events(
                str(request.get("task_id", "")), int(request.get("limit", 20)), bool(request.get("full"))
            )
        if action == "cancel":
            return await self._cancel(str(request.get("task_id", "")))
        if action == "purge_outputs":
            return self.store.purge_outputs(
                task_id=request.get("task_id"),
                kind=request.get("kind"),
                status=request.get("status"),
                before=self._before(request.get("before")),
                confirm=bool(request.get("confirm")),
            )
        if action == "purge_tasks":
            return self.store.purge_tasks(
                status=request.get("status"),
                before=self._before(request.get("before")),
                confirm=bool(request.get("confirm")),
            )
        raise DaemonError(f"unknown action: {action}")

    @staticmethod
    def _before(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise DaemonError("--before must be an ISO-8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise DaemonError("--before must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise DaemonError("--before must include a timezone offset")
        return parsed.astimezone(timezone.utc).isoformat()

    def _submit(self, task_folder: str, force: bool) -> dict[str, Any]:
        config = load_task_config(task_folder)
        if config.schedule is None:
            raise ConfigError("schedule is required for daemon submission")
        _validate_session_exists(self.codex_home, config.thread_id)
        if config.mode == "strict":
            _proxy_command(self.codex, self.endpoint)
        state_path = config.task_dir / STATE_FILENAME
        state = _read_state(state_path)
        if state and state["status"] in {"delivered", "sending", "rejected"} and not force:
            raise DaemonError(
                f"task state is {state['status']}; use --force only after checking for duplicate delivery"
            )
        lock = TaskLock(config.task_dir / LOCK_FILENAME)
        if not lock.acquire():
            raise DaemonError(f"task folder is locked by another runner: {config.task_dir}")
        task_id = str(uuid.uuid4())
        timezone_name = config.timezone or local_timezone_name()
        resuming_retry = bool(state and state["status"] == "retrying" and not force)
        try:
            task = self.store.create_task(
                {
                    "id": task_id,
                    "name": config.name,
                    "task_dir": str(config.task_dir),
                    "thread_id": config.thread_id,
                    "trigger_path": str(config.trigger_path),
                    "message_path": str(config.message_path),
                    "schedule": config.schedule,
                    "timezone": timezone_name,
                    "lifecycle": config.lifecycle,
                    "mode": config.mode,
                    "status": "retrying" if resuming_retry else "scheduled",
                    "next_run_at": utc_now()
                    if resuming_retry
                    else next_run_at(config.schedule, timezone_name),
                }
            )
        except Exception:
            lock.close()
            raise
        self.locks[task_id] = lock
        return task

    def _show(self, identifier: str, full: bool) -> dict[str, Any]:
        task = self.store.get_task(identifier)
        task["events"] = self._events(task["id"], 20, full)
        return task

    def _events(self, identifier: str, limit: int, full: bool) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 1000:
            raise DaemonError("event limit must be between 1 and 1000")
        events = self.store.list_events(identifier, limit)
        if full:
            for event in events:
                event["stdout"] = self.store.read_artifact(event["stdout_path"])
                event["stderr"] = self.store.read_artifact(event["stderr_path"])
        return events

    async def _cancel(self, identifier: str) -> dict[str, Any]:
        task = self.store.get_task(identifier)
        if task["status"] not in ACTIVE_STATUSES:
            raise DaemonError(f"task is already terminal: {task['status']}")
        active = self.runs.get(task["id"])
        if active is None:
            self._terminal(task["id"], "cancelled", "cancelled by user")
        else:
            self.store.update_task(
                task["id"],
                status="cancelling" if active.phase == "sending" else task["status"],
                cancel_requested=1,
            )
            if active.process is not None:
                await self._terminate(active.process)
        return self.store.get_task(task["id"])

    async def _shutdown_runs(self) -> None:
        for task_id, active in list(self.runs.items()):
            if active.phase == "sending":
                self.store.update_task(task_id, status="cancelling", cancel_requested=1)
            if active.process is not None:
                await self._terminate(active.process)
        if self.runs:
            await asyncio.gather(*(run.task for run in self.runs.values()), return_exceptions=True)


def run_daemon(**kwargs: Any) -> int:
    daemon = WakeDaemon(**kwargs)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, daemon.stop)
        await daemon.serve()

    try:
        asyncio.run(main())
    except (DaemonError, RunnerSetupError) as exc:
        raise RunnerSetupError(str(exc)) from exc
    return 0
