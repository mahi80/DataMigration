"""Databricks SQL warehouse snapshots and keyed MERGE into existing Delta tables."""
from __future__ import annotations

import os
import re
from typing import Any, Iterator
from urllib.parse import urlparse

from .base import Column, ConnectorError, TableSchema, identifier_parts, normalize_datetime, require_dependency, validate_read, validate_rows
from .redshift import warehouse_type


class DatabricksConnector:
    def __init__(self, url: str, options: dict | None = None):
        self.options = options or {}
        token = os.environ.get(self.options.get("token_env", "DATABRICKS_TOKEN"))
        if not token or not self.options.get("http_path"):
            raise ConnectorError("Databricks requires options.http_path and the token_env environment variable")
        parsed = urlparse(url if "://" in url else "https://" + url)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ConnectorError("Databricks URI must contain only the workspace host; credentials belong in token_env")
        self.conn = require_dependency("databricks.sql", "databricks").connect(
            server_hostname=parsed.hostname, http_path=self.options["http_path"], access_token=token)
        self._schemas: dict[str, TableSchema] = {}
        with self.conn.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")

    def _parts(self, table: str) -> list[str]:
        parts = identifier_parts(table, 3)
        if len(parts) == 1:
            if not self.options.get("catalog") or not self.options.get("schema"):
                raise ConnectorError("Databricks requires catalog.schema.table or options.catalog and options.schema")
            parts = [self.options["catalog"], self.options["schema"], parts[0]]
        elif len(parts) == 2:
            if not self.options.get("catalog"):
                raise ConnectorError("Databricks requires options.catalog for two-part names")
            parts = [self.options["catalog"], *parts]
        for part in parts:
            identifier_parts(part, 1)
        return parts

    def _quoted(self, table: str) -> str:
        return ".".join(f"`{part}`" for part in self._parts(table))

    def list_tables(self) -> list[str]:
        catalog, schema, _ = self._parts("placeholder")
        with self.conn.cursor() as cursor:
            cursor.execute(f"SHOW TABLES IN `{catalog}`.`{schema}`")
            return sorted(f"{catalog}.{schema}.{row[1]}" for row in cursor.fetchall() if not row[2])

    def inspect_table(self, table: str) -> TableSchema:
        with self.conn.cursor() as cursor:
            cursor.execute(f"DESCRIBE TABLE {self._quoted(table)}")
            records = cursor.fetchall()
        columns = []
        decimals = {}
        timezones = {}
        native_types = {}
        seen = set()
        for name, native, *_ in records:
            if not name or name.startswith("#"):
                continue
            if name in seen:
                continue
            identifier_parts(name, 1)
            seen.add(name)
            columns.append(Column(name, warehouse_type(native), True))
            native_types[name] = native
            if warehouse_type(native) == "datetime":
                timezones[name] = None if native.lower() in {"timestamp_ntz", "timestamp without time zone"} else "UTC"
            if match := re.fullmatch(r"decimal\((\d+),(\d+)\)", native.lower().replace(" ", "")):
                decimals[name] = {"precision": int(match[1]), "scale": int(match[2])}
        if not columns:
            raise ConnectorError("Databricks table has no supported scalar columns")
        return TableSchema(table, columns, [], {"decimal_types": decimals, "timestamp_timezones": timezones, "native_types": native_types,
                           "snapshot": "requires_quiesced_source", "target_format": "delta"})

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
            while rows := cursor.fetchmany(batch_size):
                batch = [dict(zip(names, row)) for row in rows]
                for row in batch:
                    for name, zone in schema.metadata["timestamp_timezones"].items():
                        row[name] = normalize_datetime(row[name], bool(zone), driver_naive_utc=bool(zone))
                yield batch

    def prepare_table(self, schema: TableSchema, keys: list[str]) -> None:
        actual = self.inspect_table(schema.name)
        if not keys or not set(keys) <= {c.name for c in schema.columns}:
            raise ConnectorError("Databricks MERGE requires explicit target business keys")
        if {c.name: c.data_type for c in actual.columns} != {c.name: c.data_type for c in schema.columns}:
            raise ConnectorError("Provision a Databricks table with matching scalar columns first")
        for key in keys:
            identifier_parts(key, 1)
        with self.conn.cursor() as cursor:
            cursor.execute(f"DESCRIBE DETAIL {self._quoted(schema.name)}")
            detail = dict(zip((field[0] for field in cursor.description), cursor.fetchone()))
            if detail.get("format") != "delta":
                raise ConnectorError("Databricks target must be a Delta table")
            grouping = ", ".join(f"`{key}`" for key in keys)
            cursor.execute(f"SELECT 1 FROM {self._quoted(schema.name)} GROUP BY {grouping} HAVING COUNT(*)>1 LIMIT 1")
            if cursor.fetchone():
                raise ConnectorError("Databricks target contains duplicate business keys")
        self._schemas[schema.name] = actual

    def write_batch(self, table: str, rows: list[dict[str, Any]], keys: list[str]) -> int:
        if not rows:
            return 0
        validate_rows(rows, keys)
        if table not in self._schemas:
            raise ConnectorError("prepare_table must validate the Databricks Delta target first")
        columns = [c.name for c in self._schemas[table].columns]
        if any(set(row) != set(columns) for row in rows):
            raise ConnectorError("Databricks rows must contain every mapped scalar column")
        rows = [dict(row) for row in rows]
        for row in rows:
            for name, zone in self._schemas[table].metadata.get("timestamp_timezones", {}).items():
                row[name] = normalize_datetime(row[name], bool(zone))
        # Native bound parameters are supported by databricks-sql-connector 3+.
        source = " UNION ALL ".join("SELECT " + ", ".join(f"? AS `{name}`" for name in columns) for _ in rows)
        predicate = " AND ".join(f"t.`{key}`=s.`{key}`" for key in keys)
        updates = ", ".join(f"`{name}`=s.`{name}`" for name in columns if name not in keys)
        if not updates:
            updates = f"`{keys[0]}`=s.`{keys[0]}`"
        fields = ", ".join(f"`{name}`" for name in columns)
        values = ", ".join(f"s.`{name}`" for name in columns)
        sql = (f"MERGE INTO {self._quoted(table)} AS t USING ({source}) AS s ON {predicate} "
               f"WHEN MATCHED THEN UPDATE SET {updates} WHEN NOT MATCHED THEN INSERT ({fields}) VALUES ({values})")
        with self.conn.cursor() as cursor:
            cursor.execute(sql, [row[name] for row in rows for name in columns])
        return len(rows)

    def close(self) -> None:
        self.conn.close()
