from __future__ import annotations

import os
from pathlib import Path


def resolve_state_dir(value: str | None = None) -> Path:
    raw = value or os.environ.get("WAKE_CODEX_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return (base / "wake-codex").resolve()


def socket_path(state_dir: Path) -> Path:
    return state_dir / "daemon.sock"
