from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


def local_timezone_name() -> str:
    configured = os.environ.get("TZ")
    if configured:
        try:
            ZoneInfo(configured)
            return configured
        except (ValueError, OSError, ZoneInfoNotFoundError):
            pass
    local = datetime.now().astimezone().tzinfo
    key = getattr(local, "key", None)
    if key:
        return key
    try:
        target = (Path("/etc/localtime").resolve()).relative_to("/usr/share/zoneinfo")
        candidate = str(target)
        ZoneInfo(candidate)
        return candidate
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return "UTC"


def next_run_at(expression: str, timezone_name: str, after: datetime | None = None) -> str:
    zone = ZoneInfo(timezone_name)
    base = (after or datetime.now(timezone.utc)).astimezone(zone)
    next_local = croniter(expression, base).get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=zone)
    return next_local.astimezone(timezone.utc).isoformat()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
