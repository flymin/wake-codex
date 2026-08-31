from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from wake_codex.schedule import utc_now


ACTIVE_STATUSES = {"scheduled", "checking", "retrying", "sending", "cancelling"}
TERMINAL_STATUSES = {"delivered", "cancelled", "rejected", "ambiguous"}


class StoreError(ValueError):
    pass


class DaemonStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.events_dir = state_dir / "events"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state_dir, 0o700)
        self.events_dir.mkdir(exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(state_dir / "state.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                task_dir TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                trigger_path TEXT NOT NULL,
                message_path TEXT NOT NULL,
                schedule TEXT NOT NULL,
                timezone TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                armed INTEGER NOT NULL DEFAULT 1,
                next_run_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_result TEXT,
                delivery_count INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS tasks_active_dir
                ON tasks(task_dir)
                WHERE status IN ('scheduled','checking','retrying','sending','cancelling');
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                returncode INTEGER,
                duration_ms INTEGER,
                stdout_path TEXT,
                stderr_path TEXT,
                stdout_bytes INTEGER NOT NULL DEFAULT 0,
                stderr_bytes INTEGER NOT NULL DEFAULT 0,
                stdout_sha256 TEXT,
                stderr_sha256 TEXT,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS events_task_id ON events(task_id, id DESC);
            """
        )
        self.connection.commit()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_task(self, values: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        payload = {**values, "created_at": now, "updated_at": now}
        columns = ",".join(payload)
        placeholders = ",".join("?" for _ in payload)
        try:
            self.connection.execute(
                f"INSERT INTO tasks ({columns}) VALUES ({placeholders})", tuple(payload.values())
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise StoreError(f"an active task already uses this task folder: {values['task_dir']}") from exc
        return self.get_task(values["id"])

    def get_task(self, identifier: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            rows = self.connection.execute(
                "SELECT * FROM tasks WHERE id LIKE ? OR name = ? ORDER BY created_at DESC LIMIT 2",
                (f"{identifier}%", identifier),
            ).fetchall()
            if not rows:
                raise StoreError(f"task not found: {identifier}")
            if len(rows) > 1:
                raise StoreError(f"task identifier is ambiguous: {identifier}")
            row = rows[0]
        return dict(row)

    def list_tasks(self, include_terminal: bool = False) -> list[dict[str, Any]]:
        if include_terminal:
            rows = self.connection.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        else:
            placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
            rows = self.connection.execute(
                f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY next_run_at, created_at",
                tuple(sorted(ACTIVE_STATUSES)),
            ).fetchall()
        return [dict(row) for row in rows]

    def due_tasks(self, now: str, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM tasks
            WHERE status IN ('scheduled','retrying')
              AND next_run_at IS NOT NULL AND next_run_at <= ?
              AND cancel_requested = 0
            ORDER BY next_run_at LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_task(self, task_id: str, **values: Any) -> dict[str, Any]:
        if not values:
            return self.get_task(task_id)
        values["updated_at"] = utc_now()
        assignments = ",".join(f"{key} = ?" for key in values)
        self.connection.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?", (*values.values(), task_id)
        )
        self.connection.commit()
        return self.get_task(task_id)

    def recover(self, now: str) -> None:
        running_events = self.connection.execute(
            "SELECT id,task_id FROM events WHERE status='running'"
        ).fetchall()
        for event in running_events:
            stdout_tmp, stderr_tmp = self.event_temp_paths(event["task_id"], event["id"])
            self.finish_event(
                event["id"],
                status="interrupted",
                returncode=None,
                duration_ms=0,
                stdout_tmp=stdout_tmp,
                stderr_tmp=stderr_tmp,
                detail="daemon stopped before the command result was recorded",
            )
        self.connection.execute(
            """
            UPDATE tasks SET status='cancelled', next_run_at=NULL, updated_at=?,
                last_error='cancellation completed during daemon recovery'
            WHERE status IN ('scheduled','checking','retrying') AND cancel_requested=1
            """,
            (now,),
        )
        self.connection.execute(
            """
            UPDATE tasks SET status='scheduled', next_run_at=?, updated_at=?
            WHERE status='checking' AND cancel_requested=0
            """,
            (now, now),
        )
        self.connection.execute(
            """
            UPDATE tasks SET status='ambiguous', next_run_at=NULL, updated_at=?,
                last_error='daemon stopped while queue delivery was in progress'
            WHERE status IN ('sending','cancelling')
            """,
            (now,),
        )
        self.connection.execute(
            """
            UPDATE tasks SET next_run_at=?, updated_at=?
            WHERE status='retrying' AND cancel_requested=0
            """,
            (now, now),
        )
        self.connection.commit()

    def start_event(self, task_id: str, kind: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO events (task_id,kind,status,started_at) VALUES (?,?,?,?)",
            (task_id, kind, "running", utc_now()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def event_temp_paths(self, task_id: str, event_id: int) -> tuple[Path, Path]:
        directory = self.events_dir / task_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory / f"{event_id}.stdout.tmp", directory / f"{event_id}.stderr.tmp"

    @staticmethod
    def _compress(path: Path) -> tuple[str | None, int, str | None, str]:
        if not path.exists():
            return None, 0, None, ""
        final = path.with_suffix(".gz")
        digest = hashlib.sha256()
        size = 0
        tail = bytearray()
        target_path: Path | None = None
        try:
            with path.open("rb") as source, tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{final.name}.", suffix=".tmp", delete=False
            ) as raw_target:
                target_path = Path(raw_target.name)
                with gzip.GzipFile(fileobj=raw_target, mode="wb") as target:
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        tail.extend(chunk)
                        if len(tail) > 131072:
                            del tail[:-131072]
            os.replace(target_path, final)
            target_path = None
        finally:
            if target_path is not None:
                target_path.unlink(missing_ok=True)
        os.chmod(final, 0o600)
        path.unlink()
        return str(final), size, digest.hexdigest(), tail.decode("utf-8", errors="replace")

    def finish_event(
        self,
        event_id: int,
        *,
        status: str,
        returncode: int | None,
        duration_ms: int,
        stdout_tmp: Path,
        stderr_tmp: Path,
        detail: str | None = None,
    ) -> dict[str, Any]:
        stdout_path, stdout_bytes, stdout_hash, stdout_tail = self._compress(stdout_tmp)
        stderr_path, stderr_bytes, stderr_hash, stderr_tail = self._compress(stderr_tmp)
        self.connection.execute(
            """
            UPDATE events SET status=?, completed_at=?, returncode=?, duration_ms=?,
                stdout_path=?, stderr_path=?, stdout_bytes=?, stderr_bytes=?,
                stdout_sha256=?, stderr_sha256=?, detail=? WHERE id=?
            """,
            (
                status,
                utc_now(),
                returncode,
                duration_ms,
                stdout_path,
                stderr_path,
                stdout_bytes,
                stderr_bytes,
                stdout_hash,
                stderr_hash,
                detail,
                event_id,
            ),
        )
        self.connection.commit()
        event = self.get_event(event_id)
        event["stdout_tail"] = stdout_tail
        event["stderr_tail"] = stderr_tail
        return event

    def get_event(self, event_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise StoreError(f"event not found: {event_id}")
        return dict(row)

    def list_events(self, task_id: str, limit: int = 20) -> list[dict[str, Any]]:
        task = self.get_task(task_id)
        rows = self.connection.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task["id"], limit),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def read_artifact(path: str | None) -> str:
        if not path or not Path(path).is_file():
            return ""
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def purge_outputs(
        self,
        *,
        task_id: str | None,
        kind: str | None,
        status: str | None,
        before: str | None,
        confirm: bool,
    ) -> dict[str, int]:
        clauses = ["(stdout_path IS NOT NULL OR stderr_path IS NOT NULL)"]
        params: list[Any] = []
        if task_id:
            task_id = self.get_task(task_id)["id"]
            clauses.append("task_id=?")
            params.append(task_id)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if status:
            clauses.append("status=?")
            params.append(status)
        if before:
            clauses.append("completed_at < ?")
            params.append(before)
        rows = self.connection.execute(
            f"SELECT id,stdout_path,stderr_path,stdout_bytes,stderr_bytes FROM events WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
        result = {
            "events": len(rows),
            "bytes": sum(int(row["stdout_bytes"]) + int(row["stderr_bytes"]) for row in rows),
        }
        if confirm:
            for row in rows:
                for key in ("stdout_path", "stderr_path"):
                    if row[key]:
                        Path(row[key]).unlink(missing_ok=True)
                self.connection.execute(
                    "UPDATE events SET stdout_path=NULL,stderr_path=NULL WHERE id=?", (row["id"],)
                )
            self.connection.commit()
        return result

    def purge_tasks(
        self, *, status: str | None, before: str | None, confirm: bool
    ) -> dict[str, int]:
        clauses = ["status IN ('delivered','cancelled','rejected','ambiguous')"]
        params: list[Any] = []
        if status:
            if status not in TERMINAL_STATUSES:
                raise StoreError("purge tasks only accepts terminal statuses")
            clauses.append("status=?")
            params.append(status)
        if before:
            clauses.append("updated_at < ?")
            params.append(before)
        rows = self.connection.execute(
            f"SELECT id FROM tasks WHERE {' AND '.join(clauses)}", params
        ).fetchall()
        task_ids = [row["id"] for row in rows]
        bytes_total = 0
        for task_id in task_ids:
            events = self.connection.execute(
                "SELECT stdout_path,stderr_path,stdout_bytes,stderr_bytes FROM events WHERE task_id=?",
                (task_id,),
            ).fetchall()
            bytes_total += sum(int(e["stdout_bytes"]) + int(e["stderr_bytes"]) for e in events)
            if confirm:
                for event in events:
                    for key in ("stdout_path", "stderr_path"):
                        if event[key]:
                            Path(event[key]).unlink(missing_ok=True)
                self.connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                shutil.rmtree(self.events_dir / task_id, ignore_errors=True)
        if confirm:
            self.connection.commit()
        return {"tasks": len(task_ids), "bytes": bytes_total}
