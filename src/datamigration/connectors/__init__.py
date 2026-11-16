"""Lazy connector registry; install only the drivers used by your deployment."""
from .base import Column, Connector, ConnectorError, TableSchema


CONNECTOR_CAPABILITIES = {
    "csv": {"source": True, "target": True, "cdc": False, "status": "local-integration-tested", "notes": "Explicit typed schema; local files; bounded atomic keyed replacement and null-token contract"},
    "jsonl": {"source": True, "target": True, "cdc": False, "status": "local-integration-tested", "notes": "Explicit schema or typed sidecar; local files; lossless tagged values and bounded keyed replacement"},
    "parquet": {"source": True, "target": True, "cdc": False, "status": "local-integration-tested", "notes": "Single local Parquet file per table; scalar Arrow types; bounded atomic keyed replacement"},
    "sqlite": {"source": True, "target": True, "cdc": False, "status": "local-integration-tested", "notes": "Scalar/JSON keyed snapshots; Decimal values require an explicit lossless conversion"},
    "postgresql": {"source": True, "target": True, "cdc": False, "status": "implemented-not-live-verified", "notes": "SQLAlchemy and psycopg; keyed ON CONFLICT writes"},
    "mysql": {"source": True, "target": True, "cdc": False, "status": "implemented-not-live-verified", "notes": "SQLAlchemy; existing target with enforced unique keys; external driver required"},
    "sqlserver": {"source": True, "target": True, "cdc": False, "status": "implemented-not-live-verified", "notes": "SQLAlchemy/ODBC; existing target with enforced unique keys"},
    "mongodb": {"source": True, "target": True, "cdc": False, "status": "adapter-tested-not-live-verified", "notes": "Ordered canonical BSON Extended JSON strings; no automatic flattening"},
    "redis": {"source": True, "target": True, "cdc": False, "status": "adapter-tested-not-live-verified", "notes": "Redis 7+ hashes only; explicit prefix; binary payloads and absolute expiry retained"},
    "cassandra": {"source": True, "target": True, "cdc": False, "status": "implemented-not-live-verified", "notes": "Existing scalar tables, full partition/clustering keys; rejects TTL, counters, collections and UDTs"},
    "redshift": {"source": True, "target": True, "cdc": False, "status": "adapter-tested-not-live-verified", "notes": "Existing target; bounded staging MERGE; single writer required; bulk loads should use S3 COPY"},
    "databricks": {"source": True, "target": True, "cdc": False, "status": "adapter-tested-not-live-verified", "notes": "SQL warehouse; existing scalar Delta target; explicit business keys and single writer"},
    "delta": {"source": True, "target": True, "cdc": False, "status": "local-integration-tested", "notes": "delta-rs, version-pinned table reads, scalar columns and MERGE; no automatic schema evolution"},
}
SUPPORTED_KINDS = tuple(CONNECTOR_CAPABILITIES)


def create_connector(kind: str, url: str, options: dict | None = None) -> Connector:
    name = kind.lower().replace("-", "_")
    if name in {"csv", "jsonl", "parquet"}:
        from .flatfile import FlatFileConnector
        return FlatFileConnector(name, url, options)
    if name in {"sqlite", "postgresql", "postgres", "mysql", "mssql", "sqlserver", "azure_sql", "sqlalchemy"}:
        from .sql import SQLConnector
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return SQLConnector(url, options)
    if name in {"mongodb", "mongo"}:
        from .mongo import MongoConnector
        return MongoConnector(url, options)
    if name == "redis":
        from .redis import RedisConnector
        return RedisConnector(url, options)
    if name == "cassandra":
        from .cassandra import CassandraConnector
        return CassandraConnector(url, options)
    if name == "redshift":
        from .redshift import RedshiftConnector
        return RedshiftConnector(url, options)
    if name in {"databricks", "databricks_sql"}:
        from .databricks import DatabricksConnector
        return DatabricksConnector(url, options)
    if name in {"delta", "deltalake", "delta_lake"}:
        from .delta import DeltaConnector
        return DeltaConnector(url, options)
    raise ConnectorError(f"Unsupported connector kind {kind!r}; see docs/connectors.md")


__all__ = ["Column", "Connector", "ConnectorError", "TableSchema", "create_connector", "CONNECTOR_CAPABILITIES", "SUPPORTED_KINDS"]
