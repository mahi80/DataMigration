"""Read-only catalog inventory. Stored procedure bodies are never executed."""
from __future__ import annotations

from .base import ConnectorError


def inventory_sql(connector, include_definitions: bool = False) -> dict:
    if not hasattr(connector, "engine") or not hasattr(connector, "sa"):
        raise ConnectorError("SQL inventory requires the SQLAlchemy connector")
    inspector = connector.sa.inspect(connector.engine)
    dialect = connector.engine.dialect.name
    selected = connector.options.get("inventory_schemas")
    if not selected:
        selected = [connector.options["schema"]] if connector.options.get("schema") else inspector.get_schema_names()
    if not isinstance(selected, list) or not all(isinstance(name, str) for name in selected):
        raise ConnectorError("inventory_schemas must be a list of schema names")
    selected = [name for name in selected if name not in {"information_schema", "sys", "mysql", "performance_schema"}
                and not name.startswith("pg_")]
    budget = int(connector.options.get("inventory_max_assets", 1000))
    if not 1 <= budget <= 100000:
        raise ConnectorError("inventory_max_assets must be between 1 and 100000")
    schemas, total, truncated = [], 0, False
    for namespace in selected:
        tables, views = inspector.get_table_names(schema=namespace), inspector.get_view_names(schema=namespace)
        remaining = max(0, budget - total)
        table_names = tables[:remaining]
        view_names = views[:max(0, remaining - len(table_names))]
        total += len(table_names) + len(view_names)
        truncated = truncated or len(table_names) != len(tables) or len(view_names) != len(views)
        view_items = [{"name": name, **({"definition": inspector.get_view_definition(name, schema=namespace)} if include_definitions else {})}
                      for name in view_names]
        schemas.append({"name": namespace, "tables": table_names, "views": view_items})

    definition_column = "NULL AS definition"
    if dialect == "postgresql":
        if include_definitions:
            definition_column = "pg_get_functiondef(p.oid) AS definition"
        statement = f"SELECT n.nspname AS schema_name, p.proname AS name, pg_get_function_identity_arguments(p.oid) AS arguments, {definition_column} FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE p.prokind='p' AND n.nspname=:namespace ORDER BY p.proname"
    elif dialect == "mssql":
        if include_definitions:
            definition_column = "m.definition AS definition"
        statement = f"SELECT s.name AS schema_name, p.name AS name, {definition_column} FROM sys.procedures p JOIN sys.schemas s ON p.schema_id=s.schema_id LEFT JOIN sys.sql_modules m ON p.object_id=m.object_id WHERE s.name=:namespace ORDER BY p.name"
    elif dialect == "mysql":
        if include_definitions:
            definition_column = "ROUTINE_DEFINITION AS definition"
        statement = f"SELECT ROUTINE_SCHEMA AS schema_name, ROUTINE_NAME AS name, {definition_column} FROM information_schema.ROUTINES WHERE ROUTINE_TYPE='PROCEDURE' AND ROUTINE_SCHEMA=:namespace ORDER BY ROUTINE_NAME"
    else:
        statement = None
    procedures = []
    if statement:
        with connector.engine.connect() as connection:
            for namespace in selected:
                rows = connection.execute(connector.sa.text(statement), {"namespace": namespace}).mappings()
                for row in rows:
                    if total >= budget:
                        truncated = True
                        break
                    procedures.append(dict(row))
                    total += 1
                rows.close()
    query_names = sorted(connector.options.get("queries", {}))
    selected_queries = query_names[:max(0, budget - total)]
    truncated = truncated or len(selected_queries) != len(query_names)
    return {"dialect": dialect, "schemas": schemas, "stored_procedures": {"supported": statement is not None, "items": procedures},
            "definitions_included": include_definitions, "truncated": truncated,
            "reviewed_query_sources": selected_queries,
            "execution": "metadata_only_no_procedures_executed"}
