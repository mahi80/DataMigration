"""Small, synchronous, batch-oriented connector contract.

Connectors are deterministic data-plane workers. They never invoke an LLM.
Snapshot reads require a quiesced source unless the deployment supplies a
database-native consistent snapshot. No connector in this package implements CDC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterator, Protocol
import re


class ConnectorError(RuntimeError):
    """An actionable connector error without embedded credentials."""


@dataclass
class Column:
    name: str
    data_type: str
    nullable: bool = True


@dataclass
class TableSchema:
    name: str
    columns: list[Column]
    primary_key: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class Connector(Protocol):
    def list_tables(self) -> list[str]: ...
    def inspect_table(self, table: str) -> TableSchema: ...
    def read_batches(self, table: str, batch_size: int = 500,
                     limit: int | None = None) -> Iterator[list[dict[str, Any]]]: ...
    def prepare_table(self, schema: TableSchema, keys: list[str]) -> None: ...
    def write_batch(self, table: str, rows: list[dict[str, Any]], keys: list[str]) -> int: ...
    def close(self) -> None: ...


def identifier_parts(name: str, max_parts: int = 2) -> list[str]:
    """Restrict identifier syntax; never interpolate arbitrary user SQL."""
    parts = name.split(".")
    if not 1 <= len(parts) <= max_parts or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts
    ):
        raise ConnectorError(f"Unsupported identifier {name!r}; use simple qualified names")
    return parts


def validate_read(batch_size: int, limit: int | None) -> None:
    if batch_size < 1 or (limit is not None and limit < 0):
        raise ConnectorError("batch_size must be positive and limit must be nonnegative")


def validate_rows(rows: list[dict[str, Any]], keys: list[str]) -> None:
    if not keys:
        raise ConnectorError("An explicit non-null target key is required for repeatable upserts")
    seen = set()
    for row in rows:
        if any(key not in row or row[key] is None for key in keys):
            raise ConnectorError("Every row must contain all non-null target key columns")
        try:
            identity = tuple(row[key] for key in keys)
            if identity in seen:
                raise ConnectorError("Duplicate business key in one input batch")
            seen.add(identity)
        except TypeError as exc:
            raise ConnectorError("Target business keys must contain scalar hashable values") from exc


def infer_type(value: Any) -> str:
    if value is None:
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    if isinstance(value, (bytes, bytearray)):
        return "binary"
    if isinstance(value, (list, dict)):
        return "json"
    if isinstance(value, str):
        return "string"
    raise ConnectorError(f"Unsupported value type: {type(value).__name__}; configure an explicit conversion")


def infer_columns(rows: list[dict[str, Any]]) -> list[Column]:
    """Sample inference is evidence, not a guarantee about unsampled rows."""
    columns: list[Column] = []
    for name in sorted({key for row in rows for key in row}):
        values = [row.get(name) for row in rows]
        observed = {infer_type(value) for value in values if value is not None}
        if len(observed) > 1:
            raise ConnectorError(f"Mixed types in field {name!r}; provide a normalized source view")
        columns.append(Column(name, next(iter(observed), "string"), any(v is None for v in values)))
    return columns


def require_dependency(module: str, extra: str) -> Any:
    from importlib import import_module
    try:
        return import_module(module)
    except ImportError as exc:
        raise ConnectorError(f"Missing optional dependency; install the '{extra}' project extra") from exc


def normalize_datetime(value: Any, has_timezone: bool, *, driver_naive_utc: bool = False) -> Any:
    """Normalize an instant only when the driver/session explicitly guarantees UTC.

    An ordinary wall-clock timestamp must never be implicitly interpreted as UTC.
    ``driver_naive_utc`` is used only for a native instant type read through a UTC
    session, or Cassandra's documented UTC epoch representation.
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ConnectorError("Driver returned an unsupported timestamp representation")
    aware = value.tzinfo is not None and value.utcoffset() is not None
    if has_timezone:
        if not aware:
            if not driver_naive_utc:
                raise ConnectorError("Timestamp timezone mismatch; an explicit conversion is required")
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if aware:
        raise ConnectorError("Timestamp timezone mismatch; an explicit conversion is required")
    return value


def declared_schema(name: str, definition: Any, keys: list[str] | None = None) -> TableSchema:
    """Validate a file/query schema without inferring business types from values."""
    from ..spec import ColumnMapping
    if isinstance(definition, list):
        definition = {"columns": definition}
    if not isinstance(definition, dict) or not isinstance(definition.get("columns"), list):
        raise ConnectorError("An explicit columns schema is required for this source")
    columns = []
    metadata = dict(definition.get("metadata", {}))
    decimals = dict(metadata.get("decimal_types", {}))
    zones = dict(metadata.get("timestamp_timezones", {}))
    for item in definition["columns"]:
        if not isinstance(item, dict):
            raise ConnectorError("Column definitions must be objects")
        identifier_parts(item.get("name", ""), 1)
        try:
            checked = ColumnMapping(source=item["name"], target=item["name"], data_type=item["data_type"],
                                    nullable=item.get("nullable", True), precision=item.get("precision"),
                                    scale=item.get("scale"), timezone=item.get("timezone"))
        except (ValueError, KeyError) as exc:
            raise ConnectorError("Invalid explicit column schema") from exc
        columns.append(Column(checked.target, checked.data_type, checked.nullable))
        if checked.precision is not None:
            decimals[checked.target] = {"precision": checked.precision, "scale": checked.scale}
        if checked.data_type == "datetime" and checked.timezone is not None:
            zones[checked.target] = checked.timezone
    names = [column.name for column in columns]
    resolved_keys = list(keys or definition.get("primary_key", definition.get("keys", [])))
    if not columns or len(set(names)) != len(names) or not set(resolved_keys) <= set(names):
        raise ConnectorError("Schema columns must be unique and contain every declared key")
    metadata.update({"decimal_types": decimals, "timestamp_timezones": zones})
    return TableSchema(name, columns, resolved_keys, metadata)
