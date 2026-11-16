"""Single-host drain barrier shared by API, CLI and protocol entrypoints."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

from .spec import write_json


def status(workspace: str | Path) -> dict:
    root = Path(workspace).resolve()
    active = 0
    for folder in ("runs", "cloud-runs"):
        for file in (root / folder).glob("*/run.json"):
            try:
                if json.loads(file.read_text()).get("status") == "RUNNING":
                    active += 1
            except (OSError, json.JSONDecodeError):
                # Unknown state blocks replacement until an operator investigates.
                active += 1
    return {"draining": (root / "maintenance.json").exists(), "active_runs": active}


def set_maintenance(workspace: str | Path, enabled: bool) -> dict:
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(root / "submission.lock"), timeout=30):
        if enabled:
            write_json(root / "maintenance.json", {"draining": True})
        else:
            (root / "maintenance.json").unlink(missing_ok=True)
        return status(root)


@contextmanager
def submission_guard(workspace: str | Path):
    root = Path(workspace).resolve()
    with FileLock(str(root / "submission.lock"), timeout=30):
        if (root / "maintenance.json").exists():
            raise ValueError("New work is paused for maintenance; status remains available")
        yield
