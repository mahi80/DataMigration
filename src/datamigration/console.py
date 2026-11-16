"""Bounded, role-scoped read models for the browser console.

The connection registry contains references to server environment variables,
never resolved credentials. Run listing reads metadata and selected-page reports;
it does not hydrate every LangGraph checkpoint or expose another principal's
specification, connection options, paths, or error details.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import Field, field_validator, model_validator

from .connectors import CONNECTOR_CAPABILITIES
from .security import AuthorizationError, Principal, RolePolicy
from .spec import Connection, MigrationSpec, reject_inline_secrets


RunStatus = Literal[
    "CREATED",
    "RUNNING",
    "DISCOVERED",
    "CONTEXT_REVIEW",
    "AWAITING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "VALIDATED",
    "COMPLETED",
    "FAILED",
    "UNKNOWN",
]
MAX_REGISTRY_BYTES = 1_000_000
MAX_PROFILES = 500
MAX_RUNS = 5_000
MAX_DIRECTORY_ENTRIES = 20_000
MAX_METADATA_BYTES = 1_000_000
MAX_REPORT_BYTES = 4_000_000
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_STATUSES = frozenset(get_args(RunStatus))
_ALIASES = {
    "postgres": "postgresql",
    "mssql": "sqlserver",
    "azure_sql": "sqlserver",
    "mongo": "mongodb",
    "databricks_sql": "databricks",
    "deltalake": "delta",
    "delta_lake": "delta",
}


class ConnectionProfile(Connection):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=600)

    @field_validator("kind")
    @classmethod
    def supported_kind(cls, value: str) -> str:
        value = value.lower().replace("-", "_")
        value = _ALIASES.get(value, value)
        if value not in CONNECTOR_CAPABILITIES:
            raise ValueError("The registered connector kind is unsupported")
        return value

    @model_validator(mode="after")
    def credential_free_profile(self) -> "ConnectionProfile":
        reject_inline_secrets(self.model_dump(mode="json"))
        return self

    def connection(self) -> Connection:
        return Connection(kind=self.kind, url_env=self.url_env, options=self.options)


class ConnectionRegistry:
    def __init__(self, profiles: list[ConnectionProfile]):
        self._profiles = tuple(profiles)

    @classmethod
    def from_config(cls, records: list[dict[str, Any]] | None = None) -> "ConnectionRegistry":
        """Parse once at startup, failing without reflecting invalid secrets."""
        try:
            if records is None:
                raw = os.environ.get("MIGRATION_CONNECTIONS", "[]")
                if len(raw.encode("utf-8")) > MAX_REGISTRY_BYTES:
                    raise ValueError("Registry exceeds budget")
                records = json.loads(raw)
            if not isinstance(records, list) or len(records) > MAX_PROFILES:
                raise ValueError("Registry must be a bounded array")
            profiles = [ConnectionProfile.model_validate(record) for record in records]
            if len({profile.id for profile in profiles}) != len(profiles):
                raise ValueError("Duplicate connection profile ID")
        except (ValueError, TypeError):
            raise ValueError(
                "Invalid MIGRATION_CONNECTIONS configuration: provide distinct IDs, supported "
                "connector kinds and credential-free Connection records in a JSON array"
            ) from None
        return cls(profiles)

    def visible_to(self, principal: Principal) -> dict[str, Any]:
        RolePolicy.require(principal, "discover")
        visible = []
        for profile in self._profiles:
            try:
                allowed = RolePolicy.connection(principal, profile.connection())
            except AuthorizationError:
                continue
            visible.append(
                {
                    **profile.model_dump(mode="json"),
                    "allowed_tables": sorted(allowed) if allowed is not None else None,
                }
            )
        return {"connections": visible}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp(value: Any, fallback: float) -> str:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        if parsed is None or parsed.tzinfo is None:
            raise ValueError("Timestamp requires a timezone")
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return (
            datetime.fromtimestamp(fallback, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )


def _read_object(path: Path, budget: int) -> dict:
    if path.is_symlink():
        raise ValueError("Symbolic artifact links are unsupported")
    with path.open("rb") as stream:
        raw = stream.read(budget + 1)
    if len(raw) > budget:
        raise ValueError("Artifact exceeds the inventory budget")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Artifact must be an object")
    return value


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _validation_summary(path: Path, run_id: str, plan_hash: str | None) -> dict | None:
    try:
        report = _read_object(path, MAX_REPORT_BYTES)
        if report.get("run_id") != run_id or not plan_hash or report.get("plan_hash") != plan_hash:
            return None
        tables = report.get("tables")
        if not isinstance(tables, list) or not all(isinstance(table, dict) for table in tables):
            return None
        if type(report.get("passed")) is not bool:
            return None
        result = {
            "passed": report["passed"],
            "tables": len(tables),
            **{
                name: sum(_count(table.get(name)) for table in tables)
                for name in (
                    "expected_rows",
                    "target_rows",
                    "missing_keys",
                    "extra_keys",
                    "duplicate_keys",
                    "mismatched_rows",
                )
            },
            # Count individual approved rule corrections, not unique rows.
            "corrected_values": sum(
                _count(rule.get("corrected"))
                for table in tables
                for rule in (table.get("quality_rules") or {}).values()
                if isinstance(rule, dict)
            ),
        }
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def list_runs(
    workspace: Path,
    principal: Principal,
    *,
    limit: int = 25,
    offset: int = 0,
    status: RunStatus | None = None,
) -> dict[str, Any]:
    """List authorized local snapshot runs, with stable ordering and bounded I/O.

    This pilot inventory caps the workspace at 5,000 run directories. Archive
    completed work before reaching that limit; a shared indexed inventory belongs
    to the future distributed control plane. Damaged records are omitted without
    disclosing their contents or an inaccessible run's existence.
    """
    RolePolicy.require(principal, "status")
    if not 1 <= limit <= 100 or not 0 <= offset <= MAX_RUNS:
        raise ValueError("Run list limit/offset is outside the supported range")
    directory = workspace / "runs"
    if not directory.exists():
        return {"runs": [], "total": 0, "limit": limit, "offset": offset, "has_more": False}
    candidates: list[Path] = []
    with os.scandir(directory) as entries:
        for position, entry in enumerate(entries):
            if position >= MAX_DIRECTORY_ENTRIES:
                raise ValueError("Run inventory exceeds the pilot limit; archive completed runs")
            if entry.is_dir(follow_symlinks=False) and _RUN_ID.fullmatch(entry.name):
                candidates.append(Path(entry.path))
                if len(candidates) > MAX_RUNS:
                    raise ValueError("Run inventory exceeds the pilot limit; archive completed runs")
    visible = []
    for run_path in candidates:
        try:
            path = run_path / "run.json"
            metadata = _read_object(path, MAX_METADATA_BYTES)
            if metadata.get("run_id") != run_path.name or not isinstance(metadata.get("status"), str):
                continue
            spec = MigrationSpec.model_validate(metadata["spec"])
            RolePolicy.spec(principal, spec)
            run_status = metadata["status"] if metadata["status"] in _STATUSES else "UNKNOWN"
            if status is not None and run_status != status:
                continue
            modified = path.stat().st_mtime
            summary = {
                "run_id": run_path.name,
                "name": spec.name,
                "status": run_status,
                "source_kind": spec.source.kind,
                "target_kind": spec.target.kind,
                "source_connection": spec.source.url_env,
                "target_connection": spec.target.url_env,
                "table_count": len(spec.tables),
                "plan_hash": metadata.get("plan_hash")
                if isinstance(metadata.get("plan_hash"), str)
                and re.fullmatch(r"[a-f0-9]{64}", metadata["plan_hash"])
                else None,
                "created_at": _timestamp(metadata.get("created_at"), modified),
                "updated_at": _timestamp(metadata.get("updated_at"), modified),
            }
            visible.append(summary)
        except (OSError, ValueError, TypeError, KeyError, AuthorizationError):
            continue
    visible.sort(key=lambda item: (item["created_at"], item["run_id"]), reverse=True)
    selected = visible[offset : offset + limit]
    for summary in selected:
        # An old report is not presented as evidence for a currently running retry.
        if summary["status"] == "RUNNING":
            continue
        validation = _validation_summary(
            directory / summary["run_id"] / "validation.json", summary["run_id"], summary["plan_hash"]
        )
        if validation is not None:
            summary["validation"] = validation
    return {
        "runs": selected,
        "total": len(visible),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(selected) < len(visible),
    }
