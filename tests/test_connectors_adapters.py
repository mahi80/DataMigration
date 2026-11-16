"""Contract tests exercise adapters with fake services, not live credentials."""
import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from datamigration.connectors import Column, ConnectorError, TableSchema
from datamigration.connectors.redis import RedisConnector
from datamigration.connectors.redshift import RedshiftConnector
from datamigration.connectors.databricks import DatabricksConnector


class RedisFake:
    def __init__(self):
        self.data = {b"safe:1": {b"raw\xff": b"value\x00"}}
        self.expiries = {b"safe:1": 1999999999123}

    def scan_iter(self, **kwargs):
        yield from [b"safe:1", b"safe:1"]  # SCAN may repeat a key.

    def type(self, key):
        return b"hash"

    def pexpiretime(self, key):
        return self.expiries[key]

    def hgetall(self, key):
        return self.data[key]

    def eval(self, script, number_of_keys, key, expiry, *values):
        assert "PEXPIREAT" in script and number_of_keys == 1
        key = key.encode()
        self.data[key] = dict(zip(values[::2], values[1::2]))
        self.expiries[key] = expiry


def redis_adapter():
    connector = RedisConnector.__new__(RedisConnector)
    connector.table = "hashes"
    connector.prefix = "safe:"
    connector.pattern = "safe:*"
    connector.client = RedisFake()
    return connector


def test_redis_binary_hash_and_absolute_expiry_survive_replay():
    connector = redis_adapter()
    batches = list(connector.read_batches("hashes"))
    assert len(batches) == 1 and len(batches[0]) == 1
    row = batches[0][0]
    assert row["_expires_at_ms"] == 1999999999123
    assert row["payload"] == {base64.b64encode(b"raw\xff").decode(): base64.b64encode(b"value\x00").decode()}
    connector.write_batch("hashes", [row], ["_key"])
    connector.write_batch("hashes", [row], ["_key"])
    assert list(connector.read_batches("hashes")) == batches


def test_redis_refuses_unbounded_namespace_and_outside_writes():
    with pytest.raises(ConnectorError, match="restricted prefix"):
        RedisConnector("redis://localhost", {"pattern": "*"})
    connector = redis_adapter()
    with pytest.raises(ConnectorError, match="outside"):
        connector.write_batch("hashes", [{"_key": "other:1", "payload": {"YQ==": "Yg=="}, "_expires_at_ms": -1}], ["_key"])


def test_redis_rejects_per_field_ttl():
    connector = redis_adapter()
    connector.supports_field_ttl = True
    connector.client.execute_command = lambda *args: [1999999999000]
    with pytest.raises(ConnectorError, match="per-field TTL"):
        list(connector.read_batches("hashes"))


def test_mongo_envelope_preserves_bson_types_and_replay():
    bson = pytest.importorskip("bson")
    from bson import json_util
    from bson.decimal128 import Decimal128
    from datamigration.connectors.mongo import MongoConnector
    identifier = bson.ObjectId("507f1f77bcf86cd799439011")
    document = {"_id": identifier, "amount": Decimal128("123456789.123456789"), "nested": {"raw": b"\x00\xff"}}
    calls = []

    class Collection:
        write_concern = SimpleNamespace(acknowledged=True)

        def replace_one(self, predicate, doc, upsert):
            calls.append((predicate, doc, upsert))
            return SimpleNamespace(acknowledged=True)

    connector = MongoConnector.__new__(MongoConnector)
    connector.json_util = json_util
    connector.db = {"documents": Collection()}
    row = {"_id": connector._canonical(identifier), "document": connector._canonical(document)}
    connector.write_batch("documents", [row], ["_id"])
    connector.write_batch("documents", [row], ["_id"])
    assert calls[0] == calls[1]
    assert calls[0][0] == {"_id": identifier}
    assert calls[0][1]["amount"].to_decimal() == document["amount"].to_decimal()
    assert calls[0][1]["nested"]["raw"] == b"\x00\xff"
    with pytest.raises(ConnectorError, match="identifier"):
        connector.write_batch("documents", [{**row, "_id": '"wrong"'}], ["_id"])


def test_mongo_document_valued_id_and_nested_field_order_survive_staging():
    pytest.importorskip("bson")
    from bson import json_util
    from datamigration.connectors.mongo import MongoConnector
    from datamigration.transforms import decode_value, encode_value
    identifier = {"z": 1, "a": 2}
    original = {"_id": identifier, "z_last_name": "Bhandari", "a_nested": {"z": 3, "a": 4}}
    written = []
    def replace(predicate, doc, upsert):
        written.append((predicate, doc))
        return SimpleNamespace(acknowledged=True)
    target = SimpleNamespace(replace_one=replace, write_concern=SimpleNamespace(acknowledged=True))
    connector = MongoConnector.__new__(MongoConnector)
    connector.json_util = json_util
    connector.db = {"documents": target}
    row = {"_id": connector._canonical(identifier), "document": connector._canonical(original)}
    # Exercise the same sorted JSON codec used by durable staging. Strings must
    # remain opaque so nested BSON document ordering remains meaningful.
    staged = json.dumps({key: encode_value(value) for key, value in row.items()}, sort_keys=True)
    restored = {key: decode_value(value) for key, value in json.loads(staged).items()}
    connector.write_batch("documents", [restored], ["_id"])
    assert list(written[0][0]["_id"]) == ["z", "a"]
    assert list(written[0][1]) == ["_id", "z_last_name", "a_nested"]
    assert list(written[0][1]["a_nested"]) == ["z", "a"]


def test_mongo_unacknowledged_writes_fail_before_sending():
    from datamigration.connectors.mongo import MongoConnector
    connector = MongoConnector.__new__(MongoConnector)
    called = []
    connector.db = {"documents": SimpleNamespace(write_concern=SimpleNamespace(acknowledged=False),
                                                  replace_one=lambda *args, **kwargs: called.append(args))}
    with pytest.raises(ConnectorError, match="w=0"):
        connector.write_batch("documents", [{"_id": "1", "document": '{"_id":1}'}], ["_id"])
    assert called == []


class WarehouseCursor:
    def __init__(self, owner):
        self.owner = owner
        self.statement = ""
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, statement, parameters=None):
        self.statement = statement
        self.owner.calls.append((statement, parameters))
        if "MERGE INTO" in statement and self.owner.fail_merge:
            raise RuntimeError("simulated merge failure")
        if statement.startswith("DESCRIBE DETAIL"):
            self.description = [("format",)]

    def executemany(self, statement, parameters):
        self.owner.calls.append((statement, parameters))

    def fetchall(self):
        if "information_schema.columns" in self.statement:
            return [("id", "bigint", "NO", None, None), ("name", "varchar", "YES", None, None)]
        if self.statement.startswith("DESCRIBE TABLE"):
            return [("id", "bigint", ""), ("name", "string", "")]
        return []

    def fetchone(self):
        return ("delta",) if self.statement.startswith("DESCRIBE DETAIL") else None


class WarehouseFake:
    def __init__(self, fail_merge=False):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_merge = fail_merge

    def cursor(self):
        return WarehouseCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def warehouse_schema():
    return TableSchema("customers", [Column("id", "integer", False), Column("name", "string")], ["id"])


def test_redshift_uses_bound_staging_merge_and_rolls_back_failure():
    connector = RedshiftConnector.__new__(RedshiftConnector)
    connector.conn = WarehouseFake()
    connector.default_schema = "public"
    connector._schemas = {}
    connector.prepare_table(warehouse_schema(), ["id"])
    dangerous_value = "O'Brien'); DROP TABLE customers; --"
    connector.write_batch("customers", [{"id": 1, "name": dangerous_value}], ["id"])
    statements = "\n".join(call[0] for call in connector.conn.calls)
    assert "MERGE INTO" in statements and "ON CONFLICT" not in statements
    assert dangerous_value not in statements
    assert any(dangerous_value in str(parameters) for _, parameters in connector.conn.calls)
    connector.conn.fail_merge = True
    with pytest.raises(RuntimeError, match="simulated"):
        connector.write_batch("customers", [{"id": 1, "name": "retry"}], ["id"])
    assert connector.conn.rollbacks == 1


def test_databricks_quotes_uc_identifiers_and_binds_values():
    connector = DatabricksConnector.__new__(DatabricksConnector)
    connector.options = {"catalog": "main", "schema": "migration"}
    connector.conn = WarehouseFake()
    connector._schemas = {}
    connector.prepare_table(warehouse_schema(), ["id"])
    connector.write_batch("customers", [{"id": 1, "name": "O'Brien"}], ["id"])
    merge, parameters = connector.conn.calls[-1]
    assert "`main`.`migration`.`customers`" in merge
    assert "O'Brien" not in merge and parameters == [1, "O'Brien"]
    with pytest.raises(ConnectorError, match="Unsupported identifier"):
        connector._quoted("main.default.x;DROP")


def test_cassandra_rejects_cell_ttl_instead_of_losing_expiry(monkeypatch):
    from datamigration.connectors import cassandra as adapter
    connector = adapter.CassandraConnector.__new__(adapter.CassandraConnector)
    schema = TableSchema("records", [Column("id", "integer", False), Column("name", "string")], ["id"],
                         {"cql_types": {"id": "int", "name": "text"}})
    connector.inspect_table = lambda name: schema
    connector.session = SimpleNamespace(execute=lambda query: [(1, "value", 3600)])
    monkeypatch.setattr(adapter, "require_dependency", lambda module, extra: (
        SimpleNamespace(SimpleStatement=lambda *args, **kwargs: args[0])
        if module.endswith("query") else SimpleNamespace(ConsistencyLevel=SimpleNamespace(LOCAL_QUORUM=6))))
    with pytest.raises(ConnectorError, match="per-cell TTL"):
        list(connector.read_batches("records"))


def test_cassandra_ttl_alias_cannot_replace_data_and_timestamps_are_utc(monkeypatch):
    from datamigration.connectors import cassandra as adapter
    connector = adapter.CassandraConnector.__new__(adapter.CassandraConnector)
    schema = TableSchema("records", [Column("id", "integer", False), Column("__migration_ttl_0", "string"),
                                      Column("created", "datetime")], ["id"],
                         {"cql_types": {"id": "int", "__migration_ttl_0": "text", "created": "timestamp"},
                          "timestamp_timezones": {"created": "UTC"}})
    connector.inspect_table = lambda name: schema
    stamp = datetime(2026, 11, 16, 1, 2, 3)
    connector.session = SimpleNamespace(execute=lambda query: [(1, "keep", stamp, None, None)])
    monkeypatch.setattr(adapter, "require_dependency", lambda module, extra: (
        SimpleNamespace(SimpleStatement=lambda *args, **kwargs: args[0])
        if module.endswith("query") else SimpleNamespace(ConsistencyLevel=SimpleNamespace(LOCAL_QUORUM=6))))
    row = list(connector.read_batches("records"))[0][0]
    assert row["__migration_ttl_0"] == "keep"
    assert row["created"] == stamp.replace(tzinfo=timezone.utc)
    prepared = SimpleNamespace(consistency_level=None)
    writes = []
    connector._schemas = {"records": schema}
    connector.session = SimpleNamespace(prepare=lambda query: prepared,
                                        execute=lambda statement, values: writes.append(values))
    connector.write_batch("records", [row], ["id"])
    assert prepared.consistency_level == 6
    assert writes[0][-1].tzinfo is not None
    with pytest.raises(ConnectorError, match="timezone mismatch"):
        connector.write_batch("records", [{**row, "created": stamp}], ["id"])


def test_warehouse_timestamp_metadata_distinguishes_instant_from_wall_time():
    class TimestampCursor(WarehouseCursor):
        def fetchall(self):
            if self.statement.startswith("DESCRIBE TABLE"):
                return [("id", "bigint", ""), ("instant", "timestamp", ""), ("wall", "timestamp_ntz", "")]
            return [("id", "bigint", "NO", None, None),
                    ("instant", "timestamp with time zone", "YES", None, None),
                    ("wall", "timestamp without time zone", "YES", None, None)]

    fake = WarehouseFake()
    fake.cursor = lambda: TimestampCursor(fake)
    redshift = RedshiftConnector.__new__(RedshiftConnector)
    redshift.conn, redshift.default_schema = fake, "public"
    databricks = DatabricksConnector.__new__(DatabricksConnector)
    databricks.conn, databricks.options = fake, {"catalog": "main", "schema": "migration"}
    for connector in (redshift, databricks):
        metadata = connector.inspect_table("events").metadata
        assert metadata["timestamp_timezones"] == {"instant": "UTC", "wall": None}
        assert metadata["native_types"]["instant"] != metadata["native_types"]["wall"]
