"""Reviewed SELECT/CTE sources with declared schemas and bound parameters.

Parsing is a correctness filter, not a SQL security sandbox. The connection must
use a database-enforced read-only identity, especially for server-side functions.
"""
from __future__ import annotations

import hashlib
import json

from .base import ConnectorError, declared_schema, require_dependency, validate_read


def query_definition(connector, name: str) -> dict:
    definition = connector.options.get("queries", {}).get(name)
    if not isinstance(definition, dict):
        raise ConnectorError("Reviewed queries require a definition object with SQL, columns and keys")
    if connector.options.get("read_only_queries") is not True:
        raise ConnectorError("Reviewed queries require read_only_queries=true and a database-enforced read-only source identity")
    sql = definition.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ConnectorError("Reviewed query SQL is required")
    dialect = {"postgresql": "postgres", "mssql": "tsql"}.get(connector.engine.dialect.name, connector.engine.dialect.name)
    sqlglot = require_dependency("sqlglot", "queries")
    try:
        statements = [statement for statement in sqlglot.parse(sql, read=dialect) if statement is not None]
    except Exception as exc:
        raise ConnectorError("Query could not be parsed for this SQL dialect; use a reviewed source view") from exc
    if len(statements) != 1 or not isinstance(statements[0], (sqlglot.exp.Select, sqlglot.exp.Union, sqlglot.exp.Intersect, sqlglot.exp.Except)):
        raise ConnectorError("Only one SELECT/CTE query is allowed")
    forbidden = {"Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Into", "Lock", "Command",
                 "Copy", "Grant", "Revoke", "TruncateTable", "Transaction", "Commit", "Rollback", "Execute", "Set", "Use"}
    for node in statements[0].walk():
        if type(node).__name__ in forbidden:
            raise ConnectorError("DML, DDL, SELECT INTO, locking and administrative statements are forbidden")
        if isinstance(node, sqlglot.exp.Anonymous):
            raise ConnectorError("Unrecognized or user-defined functions require a reviewed source view; arbitrary function execution is disabled")
        if isinstance(node, sqlglot.exp.Func) and node.sql_name().lower() in {
            "nextval", "setval", "pg_notify", "pg_advisory_lock", "pg_read_file", "pg_ls_dir",
            "dblink_exec", "lo_export", "xp_cmdshell", "sleep", "pg_sleep", "get_lock"
        }:
            raise ConnectorError("State-changing or privileged functions are forbidden in query sources")
    parameters = definition.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ConnectorError("Query parameters must be a named binding object")
    schema = declared_schema(name, definition, definition.get("keys"))
    if not schema.primary_key:
        raise ConnectorError("Reviewed query sources require declared stable unique keys")
    return {**definition, "sql": sql, "parameters": parameters}


def query_schema(connector, name: str):
    definition = query_definition(connector, name)
    schema = declared_schema(name, definition, definition.get("keys"))
    signature = json.dumps({"sql": definition["sql"], "parameters": definition["parameters"]},
                           sort_keys=True, default=str, separators=(",", ":"))
    schema.metadata.update({"query_signature": hashlib.sha256(signature.encode()).hexdigest(),
                            "source_type": "reviewed_select", "snapshot": "requires_quiesced_source"})
    return schema


def read_query_batches(connector, name: str, batch_size: int, limit: int | None):
    validate_read(batch_size, limit)
    definition = query_definition(connector, name)
    expected = [column.name for column in query_schema(connector, name).columns]
    if limit == 0:
        return
    dialect = connector.engine.dialect.name
    with connector.engine.connect() as connection:
        # Additional guards where supported; read-only database credentials are
        # still required for every backend and for server-side function effects.
        if dialect == "sqlite":
            connection.exec_driver_sql("PRAGMA query_only=ON")
        elif dialect in {"postgresql", "mysql"}:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        result = connection.execution_options(stream_results=True).execute(
            connector.sa.text(definition["sql"]), definition["parameters"])
        actual = list(result.keys())
        if len(actual) != len(expected) or set(actual) != set(expected):
            raise ConnectorError("Query result columns do not match the explicit schema; alias every expression uniquely")
        count = 0
        try:
            while raw := result.fetchmany(batch_size):
                rows = [dict(row._mapping) for row in raw]
                if limit is not None:
                    rows = rows[:limit - count]
                if rows:
                    yield rows
                count += len(rows)
                if limit is not None and count >= limit:
                    break
        finally:
            result.close()
