"""Cassandra scalar snapshots; existing target schema and full primary keys."""
from __future__ import annotations

from datetime import date
from typing import Any, Iterator
from urllib.parse import unquote, urlparse
import uuid

from .base import Column, ConnectorError, TableSchema, identifier_parts, normalize_datetime, require_dependency, validate_read, validate_rows


_TYPES = {"ascii": "string", "text": "string", "varchar": "string", "uuid": "string",
          "timeuuid": "string", "inet": "string", "bigint": "integer", "int": "integer",
          "smallint": "integer", "tinyint": "integer", "varint": "integer", "decimal": "decimal",
          "double": "float", "float": "float", "boolean": "boolean", "timestamp": "datetime",
          "date": "date", "blob": "binary"}


class CassandraConnector:
    def __init__(self, url: str, options: dict | None = None):
        options = options or {}
        parsed = urlparse(url)
        keyspace = options.get("keyspace") or parsed.path.strip("/")
        if not keyspace:
            raise ConnectorError("Cassandra requires an explicit keyspace")
        identifier_parts(keyspace, 1)
        cluster_mod = require_dependency("cassandra.cluster", "cassandra")
        auth = None
        if parsed.username:
            auth = require_dependency("cassandra.auth", "cassandra").PlainTextAuthProvider(
                unquote(parsed.username), unquote(parsed.password or ""))
        kwargs = {"contact_points": options.get("contact_points", [parsed.hostname or "localhost"]),
                  "port": parsed.port or 9042, "auth_provider": auth}
        if options.get("tls"):
            import ssl
            kwargs["ssl_context"] = ssl.create_default_context(cafile=options.get("ca_file"))
        self.cluster = cluster_mod.Cluster(**kwargs)
        self.session = self.cluster.connect(keyspace)
        self.keyspace = keyspace
        self.options = options
        self._schemas: dict[str, TableSchema] = {}

    def _metadata(self, table: str):
        identifier_parts(table, 1)
        tables = self.cluster.metadata.keyspaces[self.keyspace].tables
        if table not in tables:
            raise ConnectorError(f"Cassandra target/source table {table!r} must already exist")
        return tables[table]

    def list_tables(self) -> list[str]:
        return sorted(self.cluster.metadata.keyspaces[self.keyspace].tables)

    def inspect_table(self, table: str) -> TableSchema:
        metadata = self._metadata(table)
        columns = []
        types = {}
        for column in metadata.columns.values():
            identifier_parts(column.name, 1)
            native = column.cql_type
            if native not in _TYPES:
                raise ConnectorError(f"Unsupported Cassandra type {native!r}; counters, collections, UDTs and cell metadata require a specialized migration")
            types[column.name] = native
            columns.append(Column(column.name, _TYPES[native], column not in metadata.primary_key))
        if metadata.options.get("default_time_to_live", 0):
            raise ConnectorError("Cassandra tables with default TTL are unsupported; expiry must not be silently discarded")
        return TableSchema(table, columns, [c.name for c in metadata.primary_key],
                           {"cql_types": types, "timestamp_timezones": {name: "UTC" for name, kind in types.items() if kind == "timestamp"},
                            "snapshot": "requires_quiesced_source", "cell_ttl": "rejected"})

    def read_batches(self, table: str, batch_size: int = 500,
                     limit: int | None = None) -> Iterator[list[dict[str, Any]]]:
        validate_read(batch_size, limit)
        if limit == 0:
            return
        schema = self.inspect_table(table)
        names = [c.name for c in schema.columns]
        nonkeys = [name for name in names if name not in schema.primary_key]
        fields = [f'"{name}"' for name in names]
        fields += [f'TTL("{name}") AS "__migration_ttl_{i}"' for i, name in enumerate(nonkeys)]
        cql = f'SELECT {", ".join(fields)} FROM "{table}"'
        if limit is not None:
            cql += f" LIMIT {int(limit)}"
        query = require_dependency("cassandra.query", "cassandra")
        level = require_dependency("cassandra", "cassandra").ConsistencyLevel.LOCAL_QUORUM
        result = self.session.execute(query.SimpleStatement(cql, fetch_size=batch_size, consistency_level=level))
        batch = []
        for raw in result:
            values = list(raw)
            if any(value is not None for value in values[len(names):]):
                raise ConnectorError("Cassandra row contains per-cell TTL; this scalar snapshot adapter cannot preserve it")
            row = dict(zip(names, values[:len(names)]))
            for name, native in schema.metadata["cql_types"].items():
                value = row[name]
                if value is not None and native in {"uuid", "timeuuid", "inet"}:
                    row[name] = str(value)
                if value is not None and native == "date" and not isinstance(value, date):
                    row[name] = value.date()
                if native == "timestamp":
                    row[name] = normalize_datetime(value, True, driver_naive_utc=True)
            batch.append(row)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def prepare_table(self, schema: TableSchema, keys: list[str]) -> None:
        actual = self.inspect_table(schema.name)
        if not keys or set(keys) != set(actual.primary_key):
            raise ConnectorError("Cassandra target keys must include the full existing partition and clustering key")
        if {c.name: c.data_type for c in schema.columns} != {c.name: c.data_type for c in actual.columns}:
            raise ConnectorError("Cassandra target scalar columns must match the explicitly provisioned table")
        self._schemas[schema.name] = actual

    def write_batch(self, table: str, rows: list[dict[str, Any]], keys: list[str]) -> int:
        validate_rows(rows, keys)
        if table not in self._schemas:
            raise ConnectorError("prepare_table must validate the existing Cassandra schema before writing")
        schema = self._schemas[table]
        if set(keys) != set(schema.primary_key):
            raise ConnectorError("Cassandra primary key mismatch")
        names = [c.name for c in schema.columns]
        fields = ", ".join(f'"{name}"' for name in names)
        cql = f'INSERT INTO "{table}" ({fields}) VALUES ({", ".join("?" for _ in names)})'
        statement = self.session.prepare(cql)
        statement.consistency_level = require_dependency("cassandra", "cassandra").ConsistencyLevel.LOCAL_QUORUM
        for row in rows:
            if set(row) != set(names):
                raise ConnectorError("Cassandra rows must contain every declared target column")
            values = []
            for name in names:
                value = row[name]
                if value is not None and schema.metadata["cql_types"][name] in {"uuid", "timeuuid"}:
                    value = uuid.UUID(str(value))
                if schema.metadata["cql_types"][name] == "timestamp":
                    value = normalize_datetime(value, True)
                values.append(value)
            self.session.execute(statement, values)
        return len(rows)

    def close(self) -> None:
        self.cluster.shutdown()
