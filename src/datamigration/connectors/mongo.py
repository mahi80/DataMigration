"""MongoDB snapshots through a lossless BSON Extended JSON envelope."""
from __future__ import annotations

from typing import Any, Iterator

from .base import Column, ConnectorError, TableSchema, identifier_parts, require_dependency, validate_read, validate_rows


class MongoConnector:
    """Rows are {_id: ordered EJSON identifier string, document: EJSON string}.

    This deliberately avoids silently flattening nested documents or coercing
    BSON ObjectId/Decimal128/date/binary values into ordinary JSON strings.
    """
    def __init__(self, url: str, options: dict | None = None):
        options = options or {}
        pymongo = require_dependency("pymongo", "mongo")
        self.json_util = require_dependency("bson.json_util", "mongo")
        self.client = pymongo.MongoClient(url, serverSelectionTimeoutMS=options.get("timeout_ms", 10000))
        database = options.get("database")
        self.db = self.client[database] if database else self.client.get_default_database()
        if self.db is None:
            raise ConnectorError("MongoDB requires an explicit database in the URI or options")

    def _canonical(self, value: Any) -> str:
        return self.json_util.dumps(value, json_options=self.json_util.CANONICAL_JSON_OPTIONS,
                                    sort_keys=False, separators=(",", ":"))

    def list_tables(self) -> list[str]:
        return sorted(self.db.list_collection_names())

    def inspect_table(self, table: str) -> TableSchema:
        identifier_parts(table, 1)
        if table not in self.list_tables():
            raise ConnectorError(f"MongoDB collection {table!r} does not exist")
        return TableSchema(table, [Column("_id", "string", False), Column("document", "string", False)],
                           ["_id"], {"representation": "ordered-bson-extended-json-string-envelope",
                                     "snapshot": "requires_quiesced_source"})

    def read_batches(self, table: str, batch_size: int = 500,
                     limit: int | None = None) -> Iterator[list[dict[str, Any]]]:
        validate_read(batch_size, limit)
        identifier_parts(table, 1)
        if limit == 0:
            return
        cursor = self.db[table].find({}).sort("_id", 1).batch_size(batch_size)
        if limit is not None:
            cursor = cursor.limit(limit)
        batch: list[dict[str, Any]] = []
        try:
            for doc in cursor:
                batch.append({"_id": self._canonical(doc["_id"]),
                              "document": self._canonical(doc)})
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
            if batch:
                yield batch
        finally:
            cursor.close()

    def prepare_table(self, schema: TableSchema, keys: list[str]) -> None:
        identifier_parts(schema.name, 1)
        if keys != ["_id"] or {c.name: c.data_type for c in schema.columns} != {"_id": "string", "document": "string"}:
            raise ConnectorError("MongoDB targets require the {_id: string, document: ordered EJSON string} envelope and _id key")
        if not self.db[schema.name].write_concern.acknowledged:
            raise ConnectorError("MongoDB targets require acknowledged write concern; w=0 is forbidden")
        if schema.name not in self.list_tables():
            self.db.create_collection(schema.name)

    def write_batch(self, table: str, rows: list[dict[str, Any]], keys: list[str]) -> int:
        identifier_parts(table, 1)
        validate_rows(rows, keys)
        if keys != ["_id"]:
            raise ConnectorError("MongoDB writes require the _id envelope key")
        collection = self.db[table]
        if not collection.write_concern.acknowledged:
            raise ConnectorError("MongoDB targets require acknowledged write concern; w=0 is forbidden")
        documents = []
        for row in rows:
            if set(row) != {"_id", "document"} or not isinstance(row["document"], str):
                raise ConnectorError("MongoDB writes require an ordered canonical Extended JSON document string")
            doc = self.json_util.loads(row["document"])
            if not isinstance(doc, dict):
                raise ConnectorError("MongoDB Extended JSON string must encode a document")
            if "_id" not in doc or self._canonical(doc["_id"]) != row["_id"]:
                raise ConnectorError("Envelope identifier does not match the BSON document _id")
            documents.append(doc)
        for doc in documents:
            result = collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
            if not result.acknowledged:
                raise ConnectorError("MongoDB did not acknowledge the replacement; this batch must not be recorded as complete")
        return len(documents)

    def close(self) -> None:
        self.client.close()
