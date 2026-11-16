"""Cloud execution contracts and durable submission accounting.

These adapters submit *previously deployed, approved* jobs. Compilation never
creates cloud resources. A submission receipt is not a reconciliation result.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


class CloudConfigurationError(ValueError):
    """A plan cannot be executed by the selected cloud adapter."""


class SubmissionUnknown(RuntimeError):
    """A request might have committed; reconcile it before another submission."""


class Executor(Protocol):
    def start(self, plan: dict[str, Any], idempotency_key: str) -> str: ...

    def status(self, execution_id: str) -> dict[str, Any]: ...


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def config_for(spec: dict[str, Any], provider: str) -> dict[str, Any]:
    config = spec.get("cloud", {})
    if not isinstance(config, dict):
        raise CloudConfigurationError("cloud must be an object")
    if provider in config and isinstance(config[provider], dict):
        config = config[provider]
    elif config.get("provider", provider) != provider:
        raise CloudConfigurationError(f"cloud.provider must be {provider}")
    if {"password", "token", "access_token", "secret", "client_secret", "aws_secret_access_key"}.intersection(config):
        raise CloudConfigurationError("Cloud credentials belong in workload identity/environment, not the migration manifest")
    return dict(config)


def require(config: dict[str, Any], *keys: str) -> None:
    missing = [key for key in keys if config.get(key) is None or config.get(key) == ""]
    if missing:
        raise CloudConfigurationError("Missing cloud configuration: " + ", ".join(missing))


def operation_token(key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise CloudConfigurationError("A stable, nonempty idempotency key is required")
    return hashlib.sha256(key.encode()).hexdigest()


def make_bundle(provider: str, spec: dict[str, Any], artifacts: dict, submission: dict,
                preconditions: list[str]) -> dict[str, Any]:
    return {
        "bundle_version": 1,
        "provider": provider,
        "plan_hash": digest(spec),
        "artifacts": artifacts,
        "submission": submission,
        "preconditions": preconditions,
        "execution_is_validation": False,
    }


def validate_bundle(plan: dict[str, Any], provider: str) -> None:
    if plan.get("provider") != provider or plan.get("bundle_version") != 1:
        raise CloudConfigurationError(f"Expected a compiled {provider} bundle version 1")
    require(plan, "plan_hash", "submission")


def ordered_tables(tables: list[dict]) -> list[dict]:
    pending = {table["target"]: table for table in tables}
    if len(pending) != len(tables):
        raise CloudConfigurationError("Each target table can appear only once")
    done: set[str] = set()
    result: list[dict] = []
    while pending:
        ready = [table for table in pending.values() if set(table.get("depends_on", [])) <= done]
        if not ready:
            raise CloudConfigurationError("Table dependencies contain a cycle or unknown target")
        for table in ready:
            result.append(table)
            done.add(table["target"])
            del pending[table["target"]]
    return result


class SubmissionLedger:
    """Single-host durable claim ledger for APIs without idempotent submission.

    The unique key is reserved before the external call. An abandoned claim is
    UNKNOWN, never permission to retry. For multiple hosts, replace this with a
    shared database and equivalent atomic unique-key claims and fencing.
    """

    def __init__(self, path: str):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS cloud_submissions (
                operation TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                state TEXT NOT NULL, execution_id TEXT, created_at TEXT NOT NULL
            )""")

    def _db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def claim(self, operation: str, payload_hash: str) -> tuple[dict[str, Any], bool]:
        with self._db() as db:
            created = db.execute(
                "INSERT OR IGNORE INTO cloud_submissions VALUES (?, ?, 'PENDING', NULL, ?)",
                (operation, payload_hash, datetime.now(timezone.utc).isoformat()),
            ).rowcount == 1
            row = dict(db.execute(
                "SELECT * FROM cloud_submissions WHERE operation = ?", (operation,)
            ).fetchone())
            if row["payload_hash"] != payload_hash:
                raise CloudConfigurationError("Operation key was already bound to a different payload")
            return row, created

    def record(self, operation: str, execution_id: str) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE cloud_submissions SET state='SUBMITTED', execution_id=? WHERE operation=?",
                (execution_id, operation),
            )

    def unknown(self, operation: str) -> None:
        with self._db() as db:
            db.execute("UPDATE cloud_submissions SET state='UNKNOWN' WHERE operation=?", (operation,))
