"""Small, explicit OpenMetadata REST integration.

The database service, database, and schema must already exist in OpenMetadata.
All remote writes require an explicit approval flag. That flag is an accidental
write guard; the calling application's authenticated approval workflow and the
server's own authorization remain the security boundaries.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID


class OpenMetadataError(RuntimeError):
    """A catalog request failed, without exposing its bearer token or body."""


_DATA_TYPES = {
    "integer": "BIGINT",
    "float": "DOUBLE",
    "decimal": "DECIMAL",
    "string": "STRING",
    "boolean": "BOOLEAN",
    "datetime": "TIMESTAMP",
    "date": "DATE",
    "json": "JSON",
    "binary": "BINARY",
}


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} cannot contain control characters")
    return value


def _approval(approved: bool) -> None:
    if approved is not True:
        raise PermissionError("OpenMetadata publication requires explicit approved=True")


def _fqn_member(name: str) -> str:
    name = _text(name, "column name")
    if '"' in name:
        raise ValueError("A column containing a quote needs its server-returned fullyQualifiedName")
    return f'"{name}"' if "." in name else name


def build_table_payload(
    table: Mapping[str, Any],
    schema_fqn: str,
    *,
    description: str | None = None,
) -> dict[str, Any]:
    """Convert one normalized catalog table into a CreateTable/PUT request.

    ``table['name']`` is the physical table name within ``schema_fqn``. Do not
    pass ``public.customers`` when the physical name is ``customers``; the
    parent schema is supplied separately. A literal dot in a physical name is
    retained, not silently stripped.
    """
    schema_fqn = _text(schema_fqn, "schema_fqn")
    name = _text(table.get("name"), "table name")
    raw_columns = table.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValueError("A table must contain at least one normalized column")
    columns: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw in raw_columns:
        if not isinstance(raw, Mapping):
            raise ValueError("Each column must be a mapping")
        column_name = _text(raw.get("name"), "column name")
        if any(character in column_name for character in ('"', ">")) or "::" in column_name:
            raise ValueError(
                "OpenMetadata column names cannot contain a double quote, >, or ::; "
                "use an explicitly named projection rather than silently renaming metadata"
            )
        if column_name in names:
            raise ValueError(f"Duplicate catalog column: {column_name}")
        names.add(column_name)
        data_type = raw.get("data_type")
        if data_type not in _DATA_TYPES:
            raise ValueError(f"Unsupported normalized catalog type: {data_type!r}")
        nullable = raw.get("nullable", True)
        if not isinstance(nullable, bool):
            raise ValueError("Column nullable must be a boolean")
        column: dict[str, Any] = {
            "name": column_name,
            "dataType": _DATA_TYPES[data_type],
            "constraint": "NULL" if nullable else "NOT_NULL",
        }
        if raw.get("description"):
            column["description"] = _text(raw["description"], "column description")
        # Carry only an explicit native length, never invent a measured length.
        if raw.get("data_length") is not None:
            length = raw["data_length"]
            if isinstance(length, bool) or not isinstance(length, int) or length < 1:
                raise ValueError("data_length must be a positive integer")
            column["dataLength"] = length
        columns.append(column)

    keys = table.get("primary_key", [])
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise ValueError("primary_key must be a list of column names")
    if len(set(keys)) != len(keys) or not set(keys).issubset(names):
        raise ValueError("primary_key must reference distinct existing columns")
    payload: dict[str, Any] = {
        "name": name,
        "databaseSchema": schema_fqn,
        "tableType": "Regular",
        "columns": columns,
    }
    if keys:
        payload["tableConstraints"] = [{"constraintType": "PRIMARY_KEY", "columns": keys}]
        for column in columns:
            if column["name"] in keys:
                column["constraint"] = "NOT_NULL"
    final_description = description if description is not None else table.get("description")
    if final_description:
        payload["description"] = _text(final_description, "table description")
    return payload


def _entity_reference(entity: Mapping[str, Any]) -> dict[str, str]:
    raw_id = _text(entity.get("id"), "entity id")
    try:
        entity_id = str(UUID(raw_id))
    except ValueError as error:
        raise ValueError("OpenMetadata lineage requires a server-issued entity UUID") from error
    return {"id": entity_id, "type": "table"}


def _column_fqn(entity: Mapping[str, Any], name: str) -> str:
    _text(name, "mapped column name")
    columns = entity.get("columns")
    if columns is not None:
        matched = [column for column in columns if column.get("name") == name]
        if len(matched) != 1:
            raise ValueError(f"Mapped column {name!r} is absent or ambiguous in catalog entity")
        if matched[0].get("fullyQualifiedName"):
            return _text(matched[0]["fullyQualifiedName"], "column fullyQualifiedName")
    table_fqn = _text(entity.get("fullyQualifiedName"), "table fullyQualifiedName")
    return f"{table_fqn}.{_fqn_member(name)}"


def build_lineage_payload(
    source_entity: Mapping[str, Any],
    target_entity: Mapping[str, Any],
    column_mappings: Sequence[Mapping[str, Any]],
    *,
    description: str = "Approved migration mapping",
) -> dict[str, Any]:
    """Build table and column lineage using resolved entity IDs and FQNs.

    The input mappings use ``source`` and ``target`` top-level column names.
    No SQL is inferred. Request the actual catalog entity first so the IDs and
    fully-qualified names refer to verified assets.
    """
    source_ref = _entity_reference(source_entity)
    target_ref = _entity_reference(target_entity)
    if source_ref["id"] == target_ref["id"]:
        raise ValueError("Source and target lineage entities must differ")
    lineage: list[dict[str, Any]] = []
    targets: set[str] = set()
    for mapping in column_mappings:
        target_name = _text(mapping.get("target"), "target column")
        if target_name in targets:
            raise ValueError("Use an explicit multi-source lineage builder for repeated target columns")
        targets.add(target_name)
        lineage.append(
            {
                "fromColumns": [_column_fqn(source_entity, mapping.get("source"))],
                "toColumn": _column_fqn(target_entity, target_name),
            }
        )
    if not lineage:
        raise ValueError("At least one column mapping is required")
    return {
        "edge": {
            "fromEntity": source_ref,
            "toEntity": target_ref,
            "description": _text(description, "lineage description"),
            "lineageDetails": {"columnsLineage": lineage},
        }
    }


def _api_url(base_url: str) -> str:
    parts = urlsplit(_text(base_url, "base_url"))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("OpenMetadata base_url must be an HTTP(S) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("OpenMetadata URL cannot contain credentials, a query, or a fragment")
    path = parts.path.rstrip("/")
    if path.endswith("/api/v1"):
        path = path[:-3]
    elif not path.endswith("/api"):
        path += "/api"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class OpenMetadataClient:
    """Scoped synchronous API client; no automatic publication or retries.

    ``base_url`` can be the server origin, an ``/api`` base, or an ``/api/v1``
    base. ``transport`` supports httpx.MockTransport in offline tests.
    """

    def __init__(
        self,
        base_url: str,
        token_env: str = "OPENMETADATA_TOKEN",
        *,
        timeout: float = 30.0,
        transport: Any = None,
    ) -> None:
        import httpx

        self.base_url = _api_url(base_url)
        token = os.environ.get(_text(token_env, "token_env"))
        if not token:
            raise ValueError(f"Set the OpenMetadata bearer token in environment variable {token_env}")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        import httpx

        try:
            response = self._client.request(method, self.base_url + path, **kwargs)
        except httpx.HTTPError as error:
            raise OpenMetadataError(
                f"OpenMetadata {method} {path} failed before a confirmed response"
            ) from error
        if not response.is_success:
            raise OpenMetadataError(f"OpenMetadata {method} {path} returned HTTP {response.status_code}")
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as error:
            raise OpenMetadataError("OpenMetadata returned a non-JSON response") from error
        if not isinstance(data, dict):
            raise OpenMetadataError("OpenMetadata returned an unexpected response shape")
        return data

    def get_table(self, fqn: str) -> dict[str, Any]:
        return self._request("GET", "/v1/tables/name/" + quote(_text(fqn, "fqn"), safe=""))

    def get_schema(self, fqn: str) -> dict[str, Any]:
        return self._request("GET", "/v1/databaseSchemas/name/" + quote(_text(fqn, "fqn"), safe=""))

    def upsert_table(self, payload: Mapping[str, Any], *, approved: bool = False) -> dict[str, Any]:
        _approval(approved)
        if not all(payload.get(key) for key in ("name", "databaseSchema", "columns")):
            raise ValueError("Table payload requires name, databaseSchema, and columns")
        return self._request("PUT", "/v1/tables", json=dict(payload))

    def add_lineage(self, payload: Mapping[str, Any], *, approved: bool = False) -> dict[str, Any]:
        _approval(approved)
        edge = payload.get("edge", {})
        if not isinstance(edge, Mapping) or not edge.get("fromEntity") or not edge.get("toEntity"):
            raise ValueError("Lineage payload requires resolved fromEntity and toEntity")
        return self._request("PUT", "/v1/lineage", json=dict(payload))

    def publish_catalog(
        self,
        catalog: Mapping[str, Any],
        schema_fqn: str,
        *,
        approved: bool = False,
    ) -> list[dict[str, Any]]:
        """Publish one schema's catalog after validating all local table inputs.

        Publication is a sequence of idempotent table upserts, not a distributed
        transaction. A server error can leave an earlier table already updated;
        the caller should record returned IDs and reconcile before retrying.
        """
        _approval(approved)
        tables = catalog.get("tables")
        if not isinstance(tables, list) or not tables:
            raise ValueError("catalog.tables must be a nonempty list")
        payloads = [build_table_payload(table, schema_fqn) for table in tables]
        names = [payload["name"] for payload in payloads]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate table names within the requested OpenMetadata schema")
        self.get_schema(schema_fqn)
        return [self.upsert_table(payload, approved=True) for payload in payloads]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenMetadataClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
