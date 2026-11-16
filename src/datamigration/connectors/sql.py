"""Relational snapshot reads and keyed, transactional target writes."""
from __future__ import annotations

from typing import Any, Iterator

from .base import Column, ConnectorError, TableSchema, identifier_parts, require_dependency, validate_read, validate_rows


class SQLConnector:
    def __init__(self, url: str, options: dict | None = None):
        self.sa = require_dependency("sqlalchemy", "sql")
        self.options = options or {}
        self.engine = self.sa.create_engine(url, **self.options.get("engine_options", {}))
        self._schemas: dict[str, TableSchema] = {}

    def _parts(self, name: str) -> tuple[str | None, str]:
        parts = identifier_parts(name)
        return (parts[0], parts[1]) if len(parts) == 2 else (self.options.get("schema"), parts[0])

    def _table(self, name: str):
        schema, table = self._parts(name)
        return self.sa.Table(table, self.sa.MetaData(), schema=schema, autoload_with=self.engine)

    def list_tables(self) -> list[str]:
        inspector = self.sa.inspect(self.engine)
        prefix = self.options.get("schema")
        if prefix is None and self.engine.dialect.name != "sqlite":
            prefix = inspector.default_schema_name
        names = inspector.get_table_names(schema=prefix)
        names = [f"{prefix}.{name}" if prefix else name for name in names]
        names.extend(self.options.get("queries", {}))
        names.extend(f"{prefix}.{name}" if prefix and "." not in name else name
                     for name in self.options.get("query_views", []))
        return sorted(set(names))

    def _type(self, native: Any) -> str:
        sa = self.sa
        for cls, kind in ((sa.Boolean, "boolean"), (sa.Integer, "integer"),
                          (sa.Float, "float"), (sa.Numeric, "decimal"),
                          (sa.DateTime, "datetime"), (sa.Date, "date"),
                          (sa.JSON, "json"), (sa.LargeBinary, "binary"),
                          (sa.String, "string")):
            if isinstance(native, cls):
                return kind
        raise ConnectorError(f"Unsupported SQL type {native}; expose an explicitly cast source view")

    def inspect_table(self, table: str) -> TableSchema:
        if table in self.options.get("queries", {}):
            from .sql_queries import query_schema
            return query_schema(self, table)
        obj = self._table(table)
        decimal_types = {c.name: {"precision": c.type.precision, "scale": c.type.scale}
                         for c in obj.columns if isinstance(c.type, self.sa.Numeric)}
        return TableSchema(table,
                           [Column(c.name, self._type(c.type), c.nullable) for c in obj.columns],
                           [c.name for c in obj.primary_key.columns],
                           {"decimal_types": decimal_types,
                            "timestamp_timezones": {c.name: "UTC" if c.type.timezone else None for c in obj.columns if isinstance(c.type, self.sa.DateTime)},
                            "snapshot": "requires_quiesced_source"})

    def read_batches(self, table: str, batch_size: int = 500,
                     limit: int | None = None) -> Iterator[list[dict[str, Any]]]:
        validate_read(batch_size, limit)
        if table in self.options.get("queries", {}):
            from .sql_queries import read_query_batches
            yield from read_query_batches(self, table, batch_size, limit)
            return
        obj = self._table(table)
        query = self.sa.select(obj)
        if list(obj.primary_key.columns):
            query = query.order_by(*obj.primary_key.columns)
        if limit is not None:
            query = query.limit(limit)
        with self.engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(query)
            while rows := result.fetchmany(batch_size):
                yield [dict(row._mapping) for row in rows]

    def _native_type(self, column: Column, schema: TableSchema):
        sa = self.sa
        mapping = {"integer": sa.BigInteger, "float": sa.Float, "string": sa.Text,
                   "boolean": sa.Boolean, "datetime": sa.DateTime,
                   "date": sa.Date, "json": sa.JSON, "binary": sa.LargeBinary}
        if column.data_type == "decimal":
            spec = schema.metadata.get("decimal_types", {}).get(column.name, {})
            return sa.Numeric(precision=spec.get("precision"), scale=spec.get("scale"))
        if column.data_type == "datetime":
            return sa.DateTime(timezone=bool(schema.metadata.get("timestamp_timezones", {}).get(column.name)))
        if column.data_type not in mapping:
            raise ConnectorError(f"Unsupported target type {column.data_type!r}")
        return mapping[column.data_type]()

    def _check_keys(self, table: str, keys: list[str]) -> None:
        schema, name = self._parts(table)
        inspector = self.sa.inspect(self.engine)
        candidates = [inspector.get_pk_constraint(name, schema=schema).get("constrained_columns", [])]
        candidates += [x["column_names"] for x in inspector.get_unique_constraints(name, schema=schema)]
        candidates += [x["column_names"] for x in inspector.get_indexes(name, schema=schema) if x.get("unique")]
        if not any(set(candidate) == set(keys) for candidate in candidates):
            raise ConnectorError("Target keys must match an existing primary key or unique constraint")

    def prepare_table(self, schema: TableSchema, keys: list[str]) -> None:
        if schema.name in self.options.get("queries", {}) or schema.name in self.options.get("query_views", []):
            raise ConnectorError("Reviewed query/view registrations are read-only sources")
        if not keys or not set(keys) <= {c.name for c in schema.columns}:
            raise ConnectorError("Target key columns must be present in the mapped schema")
        schema_name, name = self._parts(schema.name)
        if name in self.sa.inspect(self.engine).get_view_names(schema=schema_name):
            raise ConnectorError("SQL views are read-only migration sources; provision a real target table")
        for column in schema.columns:
            identifier_parts(column.name, 1)
        existing = self.sa.inspect(self.engine).has_table(name, schema=schema_name)
        if not existing:
            if self.engine.dialect.name not in {"sqlite", "postgresql"}:
                raise ConnectorError("For this SQL backend, provision the target table and unique keys first")
            obj = self.sa.Table(name, self.sa.MetaData(),
                               *(self.sa.Column(c.name, self._native_type(c, schema),
                                  nullable=False if c.name in keys else c.nullable,
                                  primary_key=c.name in keys) for c in schema.columns), schema=schema_name)
            obj.create(self.engine)
        reflected = self.inspect_table(schema.name)
        actual = {c.name: c for c in reflected.columns}
        for column in schema.columns:
            if column.name not in actual or actual[column.name].data_type != column.data_type:
                raise ConnectorError(f"Target schema mismatch for column {column.name!r}")
        self._check_keys(schema.name, keys)
        self._schemas[schema.name] = schema

    def write_batch(self, table: str, rows: list[dict[str, Any]], keys: list[str]) -> int:
        if not rows:
            return 0
        validate_rows(rows, keys)
        obj = self._table(table)
        self._check_keys(table, keys)
        allowed = set(obj.c.keys())
        if any(not set(row) <= allowed for row in rows):
            raise ConnectorError("Row contains unknown target columns")
        from datetime import datetime
        for row in rows:
            for name, value in row.items():
                if isinstance(value, datetime) and isinstance(obj.c[name].type, self.sa.DateTime):
                    aware = value.tzinfo is not None and value.utcoffset() is not None
                    target_aware = bool(obj.c[name].type.timezone) and self.engine.dialect.name != "sqlite"
                    if aware != target_aware:
                        raise ConnectorError("Timestamp timezone mismatch; use an explicit approved timezone conversion")
        # SQLite's Numeric affinity can lose decimal precision. Do not silently
        # advertise faithful decimal migration through that storage format.
        if self.engine.dialect.name == "sqlite":
            from decimal import Decimal
            if any(isinstance(value, Decimal) for row in rows for value in row.values()):
                raise ConnectorError("SQLite cannot preserve arbitrary Decimal values; explicitly map to text or integer units")
        dialect = self.engine.dialect.name
        with self.engine.begin() as conn:
            for row in rows:
                if dialect in {"sqlite", "postgresql"}:
                    insert = require_dependency(f"sqlalchemy.dialects.{dialect}", "sql").insert(obj).values(**row)
                    updates = {name: insert.excluded[name] for name in row if name not in keys}
                    statement = (insert.on_conflict_do_update(index_elements=keys, set_=updates)
                                 if updates else insert.on_conflict_do_nothing(index_elements=keys))
                    conn.execute(statement)
                else:
                    # Existing unique constraint prevents races from creating
                    # duplicates; a conflicting insert rolls back this batch.
                    predicate = self.sa.and_(*(obj.c[key] == row[key] for key in keys))
                    values = {name: value for name, value in row.items() if name not in keys}
                    found = conn.execute(self.sa.select(*[obj.c[k] for k in keys]).where(predicate)).first()
                    if found is None:
                        conn.execute(obj.insert().values(**row))
                    elif values:
                        conn.execute(obj.update().where(predicate).values(**values))
        return len(rows)

    def close(self) -> None:
        self.engine.dispose()
