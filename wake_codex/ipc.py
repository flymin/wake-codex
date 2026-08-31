from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any


class IpcError(RuntimeError):
    pass


def request(socket_path: Path, payload: dict[str, Any], timeout: float = 10.0) -> Any:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.extend(chunk)
    except (OSError, TimeoutError) as exc:
        raise IpcError(f"cannot contact wake-codex daemon at {socket_path}: {exc}") from exc
    if not chunks:
        raise IpcError("wake-codex daemon returned an empty response")
    try:
        response = json.loads(bytes(chunks).split(b"\n", 1)[0])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IpcError(f"invalid response from wake-codex daemon: {exc}") from exc
    if not isinstance(response, dict):
        raise IpcError("invalid response from wake-codex daemon")
    if not response.get("ok"):
        raise IpcError(str(response.get("error", "daemon request failed")))
    return response.get("result")
