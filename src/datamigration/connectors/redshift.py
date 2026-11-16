"""Redshift DB-API snapshots and bounded staging-table MERGE writes.

For large warehouses use native S3 COPY orchestration. This adapter deliberately
does not apply PostgreSQL ON CONFLICT to Redshift and does not auto-create DDL.
"""
from __future__ import annotations

import json
from typing import Any, Iterator
from urllib.parse import unquote, urlparse
import uuid

from .base import Column, ConnectorError, TableSchema, identifier_parts, normalize_datetime, require_dependency, validate_read, validate_rows


def warehouse_type(native: str) -> str:
    name = native.lower().split("(")[0].strip()
    mapping = {"smallint": "integer", "integer": "integer", "int": "integer", "bigint": "integer",
               "int2": "integer", "int4": "integer", "int8": "integer", "tinyint": "integer",
               "decimal": "decimal", "numeric": "decimal", "real": "float", "double precision": "float",
               "double": "float", "float": "float", "boolean": "boolean", "bool": "boolean",
               "character varying": "string", "character": "string", "varchar": "string",
               "char": "string", "string": "string", "text": "string", "date": "date",
               "timestamp without time zone": "datetime", "timestamp with time zone": "datetime",
               "timestamp": "datetime", "timestamp_ntz": "datetime", "timestamp_ltz": "datetime",
               "timestamp with local time zone": "datetime", "super": "json", "binary": "binary"}
    if name not in mapping:
        raise ConnectorError(f"Unsupported warehouse type {native!r}; use an explicit typed view")
    return mapping[name]


class RedshiftConnector:
    def __init__(self, url: str, options: dict | None = None):
        options = options or {}
        parsed = urlparse(url)
        self.conn = require_dependency("redshift_connector", "redshift").connect(
            host=parsed.hostname, port=parsed.port or 5439, database=unquote(parsed.path.strip("/")),
            user=unquote(parsed.username or ""), password=unquote(parsed.password or ""),
            ssl=True, timeout=options.get("timeout", 30))
        self.default_schema = options.get("schema", "public")
        identifier_parts(self.default_schema, 1)
        self._schemas: dict[str, TableSchema] = {}
        with self.conn.cursor() as cursor:
            cursor.execute("SET timezone TO 'UTC'")
        self.conn.commit()

    def _parts(self, table: str) -> tuple[str, str]:
        parts = identifier_parts(table)
        return (parts[0], parts[1]) if len(parts) == 2 else (self.default_schema, parts[0])

    def _quoted(self, table: str) -> str:
        return ".".join(f'"{part}"' for part in self._parts(table))

    def list_tables(self) -> list[str]:
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", (self.default_schema,))
            return [f"{self.default_schema}.{row[0]}" for row in cursor.fetchall()]

    def inspect_table(self, table: str) -> TableSchema:
        namespace, name = self._parts(table)
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT column_name, data_type, is_nullable, numeric_precision, numeric_scale FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (namespace, name))
            rows = cursor.fetchall()
        if not rows:
            raise ConnectorError(f"Redshift table {table!r} must already exist")
        for row in rows:
            identifier_parts(row[0], 1)
        return TableSchema(table, [Column(row[0], warehouse_type(row[1]), row[2] == "YES") for row in rows], [],
                           {"decimal_types": {row[0]: {"precision": row[3], "scale": row[4]} for row in rows if warehouse_type(row[1]) == "decimal"},
                            "timestamp_timezones": {row[0]: "UTC" if row[1].lower() == "timestamp with time zone" else None for row in rows if warehouse_type(row[1]) == "datetime"},
                            "native_types": {row[0]: row[1] for row in rows},
                            "snapshot": "requires_quiesced_source", "key_constraints": "not_enforced_by_redshift"})

    def read_batches(self, table: str, batch_size: int = 500,
                     limit: int | None = None) -> Iterator[list[dict[str, Any]]]:
        validate_read(batch_size, limit)
        schema = self.inspect_table(table)
        statement = f"SELECT * FROM {self._quoted(table)}"
        if limit is not None:
            statement += f" LIMIT {int(limit)}"
        with self.conn.cursor() as cursor:
            cursor.execute(statement)
            names = [column[0] for column in cursor.description]
            json_columns = {c.name for c in schema.columns if c.data_type == "json"}
            while raw := cursor.fetchmany(batch_size):
                batch = [dict(zip(names, row)) for row in raw]
                for row in batch:
                    for name in json_columns:
                        if isinstance(row[name], str):
                            row[name] = json.loads(row[name])
                    for name, zone in schema.metadata["timestamp_timezones"].items():
                        row[name] = normalize_datetime(row[name], bool(zone), driver_naive_utc=bool(zone))
                yield batch

    def prepare_table(self, schema: TableSchema, keys: list[str]) -> None:
        actual = self.inspect_table(schema.name)
        if not keys or not set(keys) <= {c.name for c in schema.columns}:
            raise ConnectorError("Redshift requires explicit target business keys")
        if {c.name: c.data_type for c in actual.columns} != {c.name: c.data_type for c in schema.columns}:
            raise ConnectorError("Redshift target must be provisioned with matching mapped columns")
        for key in keys:
            identifier_parts(key, 1)
        grouping = ", ".join(f'"{key}"' for key in keys)
        with self.conn.cursor() as cursor:
            cursor.execute(f"SELECT 1 FROM {self._quoted(schema.name)} GROUP BY {grouping} HAVING COUNT(*)>1 LIMIT 1")
            if cursor.fetchone():
                raise ConnectorError("Redshift target has duplicate business keys")
        self.conn.commit()
        self._schemas[schema.name] = actual

    def write_batch(self, table: str, rows: list[dict[str, Any]], keys: list[str]) -> int:
        if not rows:
            return 0
        validate_rows(rows, keys)
        if table not in self._schemas:
            raise ConnectorError("prepare_table must validate the Redshift target first")
        schema = self._schemas[table]
        columns = [c.name for c in schema.columns]
        if any(set(row) != set(columns) for row in rows):
            raise ConnectorError("Redshift writes require all mapped columns")
        rows = [dict(row) for row in rows]
        for row in rows:
            for name, zone in schema.metadata.get("timestamp_timezones", {}).items():
                row[name] = normalize_datetime(row[name], bool(zone))
        staging = f'"migration_stage_{uuid.uuid4().hex}"'
        target = self._quoted(table)
        fields = ", ".join(f'"{name}"' for name in columns)
        expression = ", ".join("JSON_PARSE(%s)" if c.data_type == "json" else "%s" for c in schema.columns)
        values = [[json.dumps(row[c.name]) if c.data_type == "json" and row[c.name] is not None else row[c.name] for c in schema.columns] for row in rows]
        predicate = " AND ".join(f'{target}."{key}"={staging}."{key}"' for key in keys)
        updates = ", ".join(f'"{name}"={staging}."{name}"' for name in columns if name not in keys)
        # With key-only tables, assigning a key to its identical source value
        # gives MERGE a valid matched clause without changing identity.
        if not updates:
            updates = f'"{keys[0]}"={staging}."{keys[0]}"'
        inserts = ", ".join(f'{staging}."{name}"' for name in columns)
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(f"CREATE TEMP TABLE {staging} (LIKE {target})")
                cursor.executemany(f"INSERT INTO {staging} ({fields}) VALUES ({expression})", values)
                cursor.execute(f"MERGE INTO {target} USING {staging} ON {predicate} WHEN MATCHED THEN UPDATE SET {updates} WHEN NOT MATCHED THEN INSERT ({fields}) VALUES ({inserts})")
                cursor.execute(f"DROP TABLE {staging}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(rows)

    def close(self) -> None:
        self.conn.close()
