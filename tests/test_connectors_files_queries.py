from datetime import date, datetime, timezone
from decimal import Decimal
import json

import pytest

from datamigration.connectors import Column, ConnectorError, TableSchema, create_connector
from datamigration.connectors.sql_inventory import inventory_sql


@pytest.mark.parametrize("kind", ["csv", "jsonl", "parquet"])
def test_typed_files_preserve_values_and_replay_keyed_updates(tmp_path, kind):
    if kind == "parquet":
        pytest.importorskip("pyarrow")
    connector = create_connector(kind, str(tmp_path))
    columns = [Column("id", "string", False), Column("name", "string"), Column("amount", "decimal"),
               Column("active", "boolean"), Column("born", "date"), Column("created", "datetime"),
               Column("binary", "binary"), Column("optional", "string"), Column("fraction", "float")]
    if kind != "parquet":
        columns.append(Column("payload", "json"))
    schema = TableSchema("people", columns, ["id"], {"decimal_types": {"amount": {"precision": 28, "scale": 9}},
                                                     "timestamp_timezones": {"created": "UTC"}})
    row = {"id": "001", "name": "Mahendra सिंह", "amount": Decimal("123456789012345678.123456789"),
           "active": False, "born": date(1980, 6, 19), "created": datetime(2026, 11, 16, 7, 8, 9, tzinfo=timezone.utc),
           "binary": b"\x00\xff", "optional": None, "fraction": 0.1}
    if kind != "parquet":
        row["payload"] = {"$decimal": "user content", "nested": {"fraction": 0.1, "exact": Decimal("0.1234567890123456789")}}
    connector.prepare_table(schema, ["id"])
    assert list(connector.read_batches("people")) == []
    connector.write_batch("people", [row], ["id"])
    connector.write_batch("people", [row], ["id"])
    assert list(connector.read_batches("people")) == [[row]]
    connector.prepare_table(schema, ["id"])
    updated = {**row, "name": "Mahi"}
    connector.write_batch("people", [updated], ["id"])
    assert list(connector.read_batches("people")) == [[updated]]
    assert connector.list_tables() == ["people"]


def test_plain_csv_requires_schema_and_preserves_zero_prefixed_ids(tmp_path):
    (tmp_path / "customers.csv").write_text("id,name,amount,optional\n001,Mahi,120.50,\\N\n", encoding="utf-8")
    connector = create_connector("csv", str(tmp_path))
    with pytest.raises(ConnectorError, match="types are never guessed"):
        connector.inspect_table("customers")
    columns = [{"name": "id", "data_type": "string", "nullable": False}, {"name": "name", "data_type": "string"},
               {"name": "amount", "data_type": "decimal", "precision": 12, "scale": 2},
               {"name": "optional", "data_type": "string"}]
    connector = create_connector("csv", str(tmp_path), {"schemas": {"customers": columns}, "keys": {"customers": ["id"]}})
    assert list(connector.read_batches("customers")) == [[{"id": "001", "name": "Mahi", "amount": Decimal("120.50"), "optional": None}]]
    assert connector.inspect_table("customers").metadata["decimal_types"]["amount"] == {"precision": 12, "scale": 2}


def test_file_atomic_write_preserves_original_on_conversion_or_budget_error(tmp_path):
    connector = create_connector("csv", str(tmp_path), {"max_rows": 1})
    schema = TableSchema("customers", [Column("id", "integer", False), Column("name", "string")], ["id"])
    connector.prepare_table(schema, ["id"])
    connector.write_batch("customers", [{"id": 1, "name": "safe"}], ["id"])
    original = (tmp_path / "customers.csv").read_bytes()
    with pytest.raises(ConnectorError, match="null_token"):
        connector.write_batch("customers", [{"id": 1, "name": "\\N"}], ["id"])
    assert (tmp_path / "customers.csv").read_bytes() == original
    with pytest.raises(ConnectorError, match="max_rows"):
        connector.write_batch("customers", [{"id": 2, "name": "excess"}], ["id"])
    assert (tmp_path / "customers.csv").read_bytes() == original


def test_files_reject_path_escape_remote_writes_and_duplicate_json_fields(tmp_path):
    with pytest.raises(ConnectorError, match="local filesystem"):
        create_connector("csv", "s3://bucket/prefix")
    connector = create_connector("jsonl", str(tmp_path), {"schemas": {"rows": [{"name": "id", "data_type": "integer"}]}})
    with pytest.raises(ConnectorError, match="identifier"):
        connector.inspect_table("../escape")
    (tmp_path / "rows.jsonl").write_text('{"id":1,"id":2}\n')
    with pytest.raises(ConnectorError, match="Duplicate JSON"):
        list(connector.read_batches("rows"))
    outside = tmp_path.parent / (tmp_path.name + "-outside.csv")
    outside.write_text("id\n1\n")
    (tmp_path / "outside.csv").symlink_to(outside)
    with pytest.raises(ConnectorError, match="symlinks"):
        create_connector("csv", str(tmp_path)).inspect_table("outside")


def test_parquet_uses_physical_schema_and_rejects_nanosecond_loss(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    pq.write_table(pa.table({"id": [1], "amount": pa.array([Decimal("12.34")], type=pa.decimal128(10, 2))}), tmp_path / "sales.parquet")
    connector = create_connector("parquet", str(tmp_path))
    schema = connector.inspect_table("sales")
    assert schema.metadata["decimal_types"]["amount"] == {"precision": 10, "scale": 2}
    assert list(connector.read_batches("sales"))[0][0]["amount"] == Decimal("12.34")
    pq.write_table(pa.table({"id": [1], "instant": pa.array([1], type=pa.timestamp("ns"))}), tmp_path / "nano.parquet")
    with pytest.raises(ConnectorError, match="Nanosecond"):
        connector.inspect_table("nano")


def test_files_enforce_datetime_contract_and_reject_symlinked_locks(tmp_path):
    connector = create_connector("jsonl", str(tmp_path))
    schema = TableSchema("events", [Column("id", "integer", False), Column("created", "datetime")], ["id"])
    connector.prepare_table(schema, ["id"])
    with pytest.raises(ConnectorError, match="timezone mismatch"):
        connector.write_batch("events", [{"id": 1, "created": datetime(2026, 1, 1, tzinfo=timezone.utc)}], ["id"])
    assert list(connector.read_batches("events")) == []
    (tmp_path / "events.jsonl.lock").unlink()
    (tmp_path / "events.jsonl.lock").symlink_to(tmp_path / "outside.lock")
    with pytest.raises(ConnectorError, match="lock files"):
        connector.write_batch("events", [{"id": 1, "created": datetime(2026, 1, 1)}], ["id"])


def test_parquet_naive_timestamp_can_be_prepared_repeatedly(tmp_path):
    pytest.importorskip("pyarrow")
    connector = create_connector("parquet", str(tmp_path))
    schema = TableSchema("events", [Column("id", "integer", False), Column("created", "datetime")], ["id"])
    connector.prepare_table(schema, ["id"])
    connector.write_batch("events", [{"id": 1, "created": datetime(2026, 1, 1)}], ["id"])
    connector.prepare_table(schema, ["id"])
    assert list(connector.read_batches("events"))[0][0]["created"] == datetime(2026, 1, 1)


def test_csv_parsing_semantics_are_bound_into_the_source_schema(tmp_path):
    from dataclasses import asdict
    from datamigration.engine import schema_signature

    connector = create_connector("csv", str(tmp_path))
    schema = TableSchema("people", [Column("id", "integer", False), Column("note", "string")], ["id"])
    connector.prepare_table(schema, ["id"])
    connector.write_batch("people", [{"id": 1, "note": None}], ["id"])
    before = schema_signature({"tables": [asdict(connector.inspect_table("people"))]})
    manifest_path = tmp_path / "people.csv.schema.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["csv_null_token"] = "NULL"
    manifest_path.write_text(json.dumps(manifest))
    after = schema_signature({"tables": [asdict(connector.inspect_table("people"))]})
    assert before != after
    assert list(connector.read_batches("people")) == [[{"id": 1, "note": "\\N"}]]


def query_connector(tmp_path, sql=None):
    pytest.importorskip("sqlglot")
    url = f"sqlite:///{tmp_path / 'query.db'}"
    setup = create_connector("sqlite", url)
    with setup.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, amount INTEGER)")
        connection.exec_driver_sql("INSERT INTO orders VALUES (1,10,50),(2,10,60),(3,20,30)")
        connection.exec_driver_sql("CREATE VIEW order_totals AS SELECT customer_id, SUM(amount) total FROM orders GROUP BY customer_id")
    setup.close()
    definition = {"sql": sql or "WITH totals AS (SELECT customer_id, SUM(amount) AS total FROM orders GROUP BY customer_id) SELECT customer_id, total FROM totals WHERE total >= :minimum",
                  "parameters": {"minimum": 100}, "columns": [{"name": "customer_id", "data_type": "integer", "nullable": False},
                                                            {"name": "total", "data_type": "integer"}], "keys": ["customer_id"]}
    return create_connector("sqlite", url, {"read_only_queries": True, "queries": {"high_value_customers": definition}})


def test_nested_cte_query_uses_bound_values_and_declared_unique_keys(tmp_path):
    connector = query_connector(tmp_path)
    assert connector.inspect_table("high_value_customers").primary_key == ["customer_id"]
    assert list(connector.read_batches("high_value_customers", batch_size=1)) == [[{"customer_id": 10, "total": 110}]]
    assert "high_value_customers" in connector.list_tables()
    with pytest.raises(ConnectorError, match="read-only sources"):
        connector.prepare_table(connector.inspect_table("high_value_customers"), ["customer_id"])


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "SELECT * FROM orders; DROP TABLE orders", "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", "SELECT mutating_function(id) FROM orders"])
def test_query_sources_reject_writes_multiple_statements_and_unrecognized_functions(tmp_path, sql):
    connector = query_connector(tmp_path, sql)
    with pytest.raises(ConnectorError):
        list(connector.read_batches("high_value_customers"))
    with connector.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM orders").scalar() == 3


def test_inventory_discovers_tables_and_views_without_executing_procedures(tmp_path):
    connector = query_connector(tmp_path)
    report = inventory_sql(connector, include_definitions=True)
    main = next(schema for schema in report["schemas"] if schema["name"] == "main")
    assert main["tables"] == ["orders"]
    assert main["views"][0]["name"] == "order_totals"
    assert "SUM(amount)" in main["views"][0]["definition"]
    assert report["stored_procedures"] == {"supported": False, "items": []}
    assert report["execution"] == "metadata_only_no_procedures_executed"
    json.dumps(report)


def test_default_postgresql_schema_is_qualified_in_scoped_discovery(monkeypatch):
    from types import SimpleNamespace
    from datamigration.connectors.sql import SQLConnector
    from datamigration.protocols.service import DiscoveryRequest, discover_connection
    from datamigration.spec import Connection

    observed = []
    inspector = SimpleNamespace(default_schema_name="public", get_table_names=lambda schema: observed.append(schema) or ["customers"])
    connector = SQLConnector.__new__(SQLConnector)
    connector.engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    connector.sa = SimpleNamespace(inspect=lambda engine: inspector)
    connector.options = {"query_views": ["customer_totals"], "queries": {"reviewed_report": {}}}
    connector.close = lambda: None
    assert connector.list_tables() == ["public.customer_totals", "public.customers", "reviewed_report"]
    assert observed == ["public"]
    monkeypatch.setenv("EXAMPLE_DISCOVERY_URL", "postgresql://example.invalid/database")
    monkeypatch.setattr("datamigration.protocols.service.connector", lambda definition: connector)
    request = DiscoveryRequest(connection=Connection(kind="postgresql", url_env="EXAMPLE_DISCOVERY_URL"),
                               tables=["public.customers"], include_schemas=False)
    assert discover_connection(request, frozenset({"public.customers"}))["table_names"] == ["public.customers"]
