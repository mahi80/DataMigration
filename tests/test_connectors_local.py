from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from datamigration.connectors import Column, ConnectorError, TableSchema, create_connector


def schema():
    return TableSchema("customers", [Column("id", "integer", False), Column("name", "string")], ["id"])


@pytest.mark.parametrize("kind", ["sqlite", "delta"])
def test_replay_updates_without_duplicate_keys(tmp_path, kind):
    if kind == "delta":
        pytest.importorskip("deltalake")
    url = f"sqlite:///{tmp_path / 'test.db'}" if kind == "sqlite" else str(tmp_path / "delta")
    connector = create_connector(kind, url)
    connector.prepare_table(schema(), ["id"])
    rows = [{"id": 1, "name": "Mahendra"}, {"id": 2, "name": "Kiran"}]
    connector.write_batch("customers", rows, ["id"])
    connector.write_batch("customers", rows, ["id"])
    connector.write_batch("customers", [{"id": 1, "name": "Mahi"}], ["id"])
    actual = [row for batch in connector.read_batches("customers", batch_size=1) for row in batch]
    assert sorted(actual, key=lambda row: row["id"]) == [{"id": 1, "name": "Mahi"}, {"id": 2, "name": "Kiran"}]
    assert list(connector.read_batches("customers", limit=0)) == []
    connector.close()


def test_sqlite_typed_values_survive_roundtrip(tmp_path):
    connector = create_connector("sqlite", f"sqlite:///{tmp_path / 'values.db'}")
    fields = [Column("id", "integer", False), Column("active", "boolean"), Column("payload", "json"),
              Column("created", "datetime"), Column("birthday", "date"), Column("attachment", "binary")]
    connector.prepare_table(TableSchema("typed", fields, ["id"]), ["id"])
    record = {"id": 8, "active": False, "payload": {"nested": ["001", None, 2]},
              "created": datetime(2026, 11, 16, 1, 2, 3), "birthday": date(1980, 6, 19), "attachment": b"\x00\xff"}
    connector.write_batch("typed", [record], ["id"])
    assert list(connector.read_batches("typed"))[0] == [record]


def test_sqlite_refuses_unconstrained_target_and_lossy_decimal(tmp_path):
    connector = create_connector("sqlite", f"sqlite:///{tmp_path / 'bad.db'}")
    with connector.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE customers (id INTEGER, name TEXT)")
    with pytest.raises(ConnectorError, match="unique constraint"):
        connector.prepare_table(schema(), ["id"])
    decimal_schema = TableSchema("money", [Column("id", "integer", False), Column("amount", "decimal")], ["id"])
    connector.prepare_table(decimal_schema, ["id"])
    with pytest.raises(ConnectorError, match="Decimal"):
        connector.write_batch("money", [{"id": 1, "amount": Decimal("999999999999999999.123456789")}], ["id"])


@pytest.mark.parametrize("kind", ["sqlite", "delta"])
def test_writes_reject_null_or_duplicate_business_keys(tmp_path, kind):
    if kind == "delta":
        pytest.importorskip("deltalake")
    url = f"sqlite:///{tmp_path / 'keys.db'}" if kind == "sqlite" else str(tmp_path / "delta")
    connector = create_connector(kind, url)
    connector.prepare_table(schema(), ["id"])
    with pytest.raises(ConnectorError, match="non-null"):
        connector.write_batch("customers", [{"id": None, "name": "bad"}], ["id"])
    with pytest.raises(ConnectorError, match="Duplicate"):
        connector.write_batch("customers", [{"id": 1, "name": "A"}, {"id": 1, "name": "B"}], ["id"])
    assert list(connector.read_batches("customers")) == []


def test_delta_pins_snapshot_and_preserves_decimal(tmp_path):
    delta = pytest.importorskip("deltalake")
    pa = pytest.importorskip("pyarrow")
    root = tmp_path / "lake"
    writer = create_connector("delta", str(root))
    money = TableSchema("money", [Column("id", "integer", False), Column("amount", "decimal")], ["id"],
                        {"decimal_types": {"amount": {"precision": 28, "scale": 9}}})
    writer.prepare_table(money, ["id"])
    first = {"id": 1, "amount": Decimal("123456789012345678.123456789")}
    writer.write_batch("money", [first], ["id"])
    reader = create_connector("delta", str(root))
    version = reader.inspect_table("money").metadata["delta_version"]
    writer.write_batch("money", [{"id": 1, "amount": Decimal("1.000000001")}], ["id"])
    assert reader.inspect_table("money").metadata["delta_version"] == version
    assert list(reader.read_batches("money"))[0] == [first]
    assert list(writer.read_batches("money"))[0][0]["amount"] == Decimal("1.000000001")
    delta.write_deltalake(str(root / "duplicates"), pa.table({"id": [1, 1], "name": ["A", "B"]}))
    with pytest.raises(ConnectorError, match="duplicate business keys"):
        writer.prepare_table(TableSchema("duplicates", schema().columns, ["id"]), ["id"])


def test_delta_rejects_implicit_nested_schema_and_path_traversal(tmp_path):
    pytest.importorskip("deltalake")
    connector = create_connector("delta", str(tmp_path))
    with pytest.raises(ConnectorError, match="Unsupported identifier"):
        connector.inspect_table("../outside")
    with pytest.raises(ConnectorError, match="Unsupported Delta target type"):
        connector.prepare_table(TableSchema("documents", [Column("id", "integer"), Column("document", "json")]), ["id"])


def test_sql_identifier_is_not_arbitrary_sql(tmp_path):
    connector = create_connector("sqlite", f"sqlite:///{tmp_path / 'safe.db'}")
    with pytest.raises(ConnectorError, match="Unsupported identifier"):
        connector.inspect_table("customers; DROP TABLE customers")


@pytest.mark.parametrize("kind", ["sqlite", "delta"])
def test_timezone_is_not_silently_removed(tmp_path, kind):
    if kind == "delta":
        pytest.importorskip("deltalake")
    url = f"sqlite:///{tmp_path / 'time.db'}" if kind == "sqlite" else str(tmp_path / "delta")
    connector = create_connector(kind, url)
    table = TableSchema("events", [Column("id", "integer", False), Column("created", "datetime")], ["id"])
    connector.prepare_table(table, ["id"])
    with pytest.raises(ConnectorError, match="timezone mismatch"):
        connector.write_batch("events", [{"id": 1, "created": datetime(2026, 11, 16, tzinfo=timezone.utc)}], ["id"])
    assert list(connector.read_batches("events")) == []
