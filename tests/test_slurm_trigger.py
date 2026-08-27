from __future__ import annotations

import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIGGER = PROJECT_ROOT / "tasks.example" / "slurm-jobs" / "trigger.sh"


def _fake_sacct(tmp_path: Path, output: str, returncode: int = 0) -> Path:
    path = tmp_path / "sacct"
    path.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<'EOF'\n"
        f"{output}"
        "EOF\n"
        f"exit {returncode}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _run(tmp_path: Path, output: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    fake = _fake_sacct(tmp_path, output, returncode)
    env = {**os.environ, "SACCT_BIN": str(fake)}
    return subprocess.run([str(TRIGGER)], text=True, capture_output=True, env=env, check=False)


def test_blocks_when_any_job_is_active(tmp_path: Path) -> None:
    result = _run(tmp_path, "12345|COMPLETED|\n12346|RUNNING|\n12347|FAILED|\n")
    assert result.returncode == 1


def test_goes_when_all_jobs_are_terminal(tmp_path: Path) -> None:
    result = _run(tmp_path, "12345|COMPLETED|\n12346|CANCELLED|\n12347|FAILED|\n")
    assert result.returncode == 0


def test_errors_when_job_is_missing_or_sacct_fails(tmp_path: Path) -> None:
    missing = _run(tmp_path, "12345|COMPLETED|\n12346|COMPLETED|\n")
    failed = _run(tmp_path, "relay unavailable\n", returncode=7)
    assert missing.returncode == 2
    assert failed.returncode == 2
