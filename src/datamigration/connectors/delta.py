"""Delta Lake tables through delta-rs, using version-pinned reads and MERGE."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from .base import Column, ConnectorError, TableSchema, identifier_parts, require_dependency, validate_read, validate_rows


class DeltaConnector:
    """URI is a table-root directory; table names identify children of that root.

    Remote object-store URIs use the SDK's credential chain/storage options.
    Root and table names are never treated as arbitrary SQL expressions.
    """
    def __init__(self, url: str, options: dict | None = None):
        self.delta = require_dependency("deltalake", "delta")
        self.pa = require_dependency("pyarrow", "delta")
        self.root = url.removeprefix("file://").rstrip("/")
        self.options = options or {}
        self.storage_options = self.options.get("storage_options", {})
        if any(any(word in key.lower() for word in ("secret", "password", "token", "access_key", "sas_key")) for key in self.storage_options):
            raise ConnectorError("Delta credentials must use environment variables or the SDK identity chain; do not put secrets in storage_options")
        self._schemas: dict[str, TableSchema] = {}
        self._versions: dict[str, int] = {}
        self._target_arrow: dict[str, Any] = {}

    def _path(self, table: str) -> str:
        identifier_parts(table, 1)
        return f"{self.root}/{table}"

    def _open(self, table: str, *, pinned: bool = False):
        kwargs = {"storage_options": self.storage_options}
        if pinned and table in self._versions:
            kwargs["version"] = self._versions[table]
        return self.delta.DeltaTable(self._path(table), **kwargs)

    def list_tables(self) -> list[str]:
        if "://" in self.root:
            tables = self.options.get("tables")
            if not tables:
                raise ConnectorError("Remote Delta roots require options.tables to avoid unbounded object-store listing")
            for table in tables:
                identifier_parts(table, 1)
            return sorted(tables)
        root = Path(self.root)
        return sorted(path.name for path in root.iterdir() if path.is_dir() and (path / "_delta_log").is_dir()) if root.exists() else []

    def _kind(self, typ) -> str:
        types = self.pa.types
        for check, kind in ((types.is_boolean, "boolean"), (types.is_integer, "integer"),
                            (types.is_floating, "float"), (types.is_decimal, "decimal"),
                            (types.is_timestamp, "datetime"), (types.is_date, "date"),
                            (types.is_string, "string"), (types.is_large_string, "string"),
                            (types.is_binary, "binary"), (types.is_large_binary, "binary")):
            if check(typ):
                return kind
        raise ConnectorError(f"Unsupported Delta type {typ}; project nested columns to a deliberate relational or JSON contract")

    def inspect_table(self, table: str) -> TableSchema:
        obj = self._open(table, pinned=True)
        self._versions.setdefault(table, obj.version())
        arrow = self.pa.schema(obj.schema().to_arrow())
        decimals = {field.name: {"precision": field.type.precision, "scale": field.type.scale}
                    for field in arrow if self.pa.types.is_decimal(field.type)}
        return TableSchema(table, [Column(field.name, self._kind(field.type), field.nullable) for field in arrow], [],
                           {"decimal_types": decimals, "timestamp_timezones": {field.name: field.type.tz for field in arrow if self.pa.types.is_timestamp(field.type)}, "delta_version": self._versions[table],
                            "snapshot": "version_pinned_per_table"})

    def read_batches(self, table: str, batch_size: int = 500,
                     limit: int | None = None) -> Iterator[list[dict[str, Any]]]:
        validate_read(batch_size, limit)
        if limit == 0:
            return
        if table not in self._versions:
            self.inspect_table(table)
        dataset = self._open(table, pinned=True).to_pyarrow_dataset()
        count = 0
        for record_batch in dataset.to_batches(batch_size=batch_size):
            batch = record_batch.to_pylist()
            if limit is not None:
                batch = batch[:limit - count]
            if batch:
                yield batch
            count += len(batch)
            if limit is not None and count >= limit:
                break

    def _arrow_schema(self, schema: TableSchema, keys: list[str]):
        pa = self.pa
        mapping = {"integer": pa.int64(), "float": pa.float64(), "string": pa.string(),
                   "boolean": pa.bool_(), "datetime": pa.timestamp("us"), "date": pa.date32(), "binary": pa.binary()}
        fields = []
        for column in schema.columns:
            identifier_parts(column.name, 1)
            if column.data_type == "decimal":
                config = schema.metadata.get("decimal_types", {}).get(column.name, {})
                precision, scale = config.get("precision"), config.get("scale")
                if precision is None or scale is None:
                    raise ConnectorError("Delta decimals require explicit precision and scale in schema.metadata.decimal_types")
                if not 1 <= precision <= 38 or not 0 <= scale <= precision:
                    raise ConnectorError("Delta decimal precision must be 1..38 with a valid nonnegative scale")
                typ = pa.decimal128(precision, scale)
            elif column.data_type == "datetime":
                typ = pa.timestamp("us", tz=schema.metadata.get("timestamp_timezones", {}).get(column.name))
            elif column.data_type in mapping:
                typ = mapping[column.data_type]
            else:
                raise ConnectorError(f"Unsupported Delta target type {column.data_type!r}; map JSON to an explicit string or use a Spark job")
            fields.append(pa.field(column.name, typ, nullable=False if column.name in keys else column.nullable))
        return pa.schema(fields)

    def prepare_table(self, schema: TableSchema, keys: list[str]) -> None:
        if not keys or not set(keys) <= {column.name for column in schema.columns}:
            raise ConnectorError("Delta targets require explicit mapped business keys")
        for key in keys:
            identifier_parts(key, 1)
        arrow = self._arrow_schema(schema, keys)
        path = self._path(schema.name)
        if self.delta.DeltaTable.is_deltatable(path, storage_options=self.storage_options):
            actual = self.inspect_table(schema.name)
            if {c.name: c.data_type for c in actual.columns} != {c.name: c.data_type for c in schema.columns}:
                raise ConnectorError("Delta target schema does not match the mapping; automatic schema evolution is disabled")
            arrow = self.pa.schema(self._open(schema.name).schema().to_arrow())
            seen = set()
            for batch in self._open(schema.name).to_pyarrow_dataset().to_batches(columns=keys, batch_size=4096):
                for row in batch.to_pylist():
                    identity = tuple(row[key] for key in keys)
                    if any(value is None for value in identity) or identity in seen:
                        raise ConnectorError("Delta target contains null or duplicate business keys")
                    seen.add(identity)
        else:
            self.delta.write_deltalake(path, self.pa.Table.from_pylist([], schema=arrow),
                                       mode="error", storage_options=self.storage_options)
        self._schemas[schema.name] = schema
        self._target_arrow[schema.name] = arrow
        # Target reads must observe the newly committed target, not a version
        # pinned during prepare. A read-only source instance keeps its snapshot.
        self._versions.pop(schema.name, None)

    def write_batch(self, table: str, rows: list[dict[str, Any]], keys: list[str]) -> int:
        if not rows:
            return 0
        validate_rows(rows, keys)
        if table not in self._schemas:
            raise ConnectorError("prepare_table must validate the Delta target before writing")
        schema = self._schemas[table]
        names = {c.name for c in schema.columns}
        if any(set(row) != names for row in rows):
            raise ConnectorError("Delta rows must contain every mapped column")
        from datetime import datetime
        for row in rows:
            for field in self._target_arrow[table]:
                value = row[field.name]
                if isinstance(value, datetime) and self.pa.types.is_timestamp(field.type):
                    aware = value.tzinfo is not None and value.utcoffset() is not None
                    if aware != bool(field.type.tz):
                        raise ConnectorError("Delta timestamp timezone mismatch; use an explicit approved conversion")
        source = self.pa.Table.from_pylist(rows, schema=self._target_arrow[table])
        predicate = " AND ".join(f'target."{key}" = source."{key}"' for key in keys)
        obj = self._open(table)
        (obj.merge(source=source, predicate=predicate, source_alias="source", target_alias="target")
             .when_matched_update_all().when_not_matched_insert_all().execute())
        self._versions.pop(table, None)
        return len(rows)

    def close(self) -> None:
        pass
