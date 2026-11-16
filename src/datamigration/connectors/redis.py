"""Restricted Redis 7+ hash snapshots preserving binary values and expiry."""
from __future__ import annotations

import base64
from typing import Any, Iterator

from .base import Column, ConnectorError, TableSchema, require_dependency, validate_read, validate_rows


# One key is updated atomically. Only hash keys in an explicit namespace are
# accepted. PEXPIREAT retains the absolute expiration rather than renewing TTL.
_REPLACE_HASH = """
local t = redis.call('TYPE', KEYS[1]).ok
if t ~= 'none' and t ~= 'hash' then return redis.error_reply('target key is not a hash') end
local expiry = tonumber(ARGV[1])
local now = redis.call('TIME')
if expiry ~= -1 and expiry <= tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000) then
  return redis.error_reply('source hash expired during migration')
end
redis.call('DEL', KEYS[1])
for i = 2, #ARGV, 2 do redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1]) end
if expiry ~= -1 then redis.call('PEXPIREAT', KEYS[1], expiry) end
return 1
"""


class RedisConnector:
    def __init__(self, url: str, options: dict | None = None):
        options = options or {}
        pattern = options.get("pattern", "")
        if not isinstance(pattern, str) or not pattern.endswith("*") or len(pattern) < 2 or any(c in pattern[:-1] for c in "*?[]"):
            raise ConnectorError("Redis requires a restricted prefix pattern, e.g. 'migration:customers:*'; '*' is forbidden")
        self.prefix = pattern[:-1]
        self.pattern = pattern
        self.table = options.get("table", "hashes")
        self.client = require_dependency("redis", "redis").Redis.from_url(url, decode_responses=False)
        version = self.client.info("server").get("redis_version", "0.0")
        major_minor = tuple(int(part) for part in version.split(".")[:2])
        if major_minor < (7, 0):
            raise ConnectorError("Redis 7.0 or newer is required for exact expiration timestamps")
        self.supports_field_ttl = major_minor >= (7, 4)

    def _check_table(self, table: str) -> None:
        if table != self.table:
            raise ConnectorError(f"Redis exposes only the configured logical table {self.table!r}")

    def list_tables(self) -> list[str]:
        return [self.table]

    def inspect_table(self, table: str) -> TableSchema:
        self._check_table(table)
        return TableSchema(table, [Column("_key", "string", False), Column("payload", "json", False),
                                   Column("_expires_at_ms", "integer", False)], ["_key"],
                           {"representation": "redis-hash-base64-envelope", "redis_min_version": "7.0",
                            "snapshot": "requires_quiesced_source_and_no_expiry_during_run"})

    def read_batches(self, table: str, batch_size: int = 500,
                     limit: int | None = None) -> Iterator[list[dict[str, Any]]]:
        validate_read(batch_size, limit)
        self._check_table(table)
        seen: set[bytes] = set()
        batch = []
        count = 0
        if limit == 0:
            return
        for key in self.client.scan_iter(match=self.pattern, count=batch_size):
            if key in seen:
                continue
            seen.add(key)
            if self.client.type(key) != b"hash":
                raise ConnectorError("Redis namespace contains non-hash keys; use a dedicated hash prefix")
            try:
                key_text = key.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConnectorError("Redis keys must be UTF-8; hash fields and values may be binary") from exc
            # Redis 7+ gives the exact absolute expiry. A PTTL approximation
            # would reset expiration and make reconciliation non-repeatable.
            expiry = self.client.pexpiretime(key)
            values = self.client.hgetall(key)
            if expiry == -2 or not values:
                raise ConnectorError("Redis key expired or changed during snapshot; quiesce the source")
            if getattr(self, "supports_field_ttl", False):
                field_expiry = self.client.execute_command("HPEXPIRETIME", key, "FIELDS", len(values), *values)
                if any(value != -1 for value in field_expiry):
                    raise ConnectorError("Redis hash contains expiring or changed fields; per-field TTL migration is unsupported")
            payload = {base64.b64encode(k).decode("ascii"): base64.b64encode(v).decode("ascii")
                       for k, v in values.items()}
            batch.append({"_key": key_text, "payload": payload, "_expires_at_ms": expiry})
            count += 1
            if len(batch) >= batch_size:
                yield batch
                batch = []
            if limit is not None and count >= limit:
                break
        if batch:
            yield batch

    def prepare_table(self, schema: TableSchema, keys: list[str]) -> None:
        self._check_table(schema.name)
        if keys != ["_key"] or {c.name: c.data_type for c in schema.columns} != {
            "_key": "string", "payload": "json", "_expires_at_ms": "integer"
        }:
            raise ConnectorError("Redis targets require the hash envelope and _key primary key")

    def write_batch(self, table: str, rows: list[dict[str, Any]], keys: list[str]) -> int:
        self._check_table(table)
        validate_rows(rows, keys)
        if keys != ["_key"]:
            raise ConnectorError("Redis writes require the _key envelope key")
        prepared = []
        for row in rows:
            if set(row) != {"_key", "payload", "_expires_at_ms"}:
                raise ConnectorError("Unexpected Redis envelope fields")
            if not isinstance(row["_key"], str) or not row["_key"].startswith(self.prefix):
                raise ConnectorError("Redis target key is outside the configured namespace")
            payload = row["payload"]
            if not isinstance(payload, dict) or not payload:
                raise ConnectorError("Redis hash payload must be a nonempty base64 field/value object")
            expiry = row["_expires_at_ms"]
            if isinstance(expiry, bool) or not isinstance(expiry, int) or expiry < -1 or expiry == 0:
                raise ConnectorError("Redis expiration must be -1 (persistent) or an absolute millisecond timestamp")
            values = []
            for field, value in sorted(payload.items()):
                try:
                    values.extend((base64.b64decode(field, validate=True), base64.b64decode(value, validate=True)))
                except (ValueError, TypeError) as exc:
                    raise ConnectorError("Invalid base64 in Redis payload") from exc
            prepared.append((row["_key"], expiry, values))
        for key, expiry, values in prepared:
            self.client.eval(_REPLACE_HASH, 1, key, expiry, *values)
        return len(rows)

    def close(self) -> None:
        self.client.close()
