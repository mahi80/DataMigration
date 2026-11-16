"""Typed local CSV, JSON Lines and single-file Parquet snapshot connectors.

Target updates perform a bounded keyed merge and atomically replace one local
data file. A schema sidecar specifies representations; schemas are never guessed
from CSV strings. Remote/object-store atomicity is deliberately not claimed.
"""
from __future__ import annotations

import base64
import csv
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator

from filelock import FileLock

from .base import Column, ConnectorError, TableSchema, declared_schema, identifier_parts, normalize_datetime, require_dependency, validate_read, validate_rows
from ..transforms import coerce, decode_value, encode_value, row_key


_EXTENSIONS = {"csv": ".csv", "jsonl": ".jsonl", "parquet": ".parquet"}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConnectorError("Duplicate JSON object keys are unsupported")
        result[key] = value
    return result


class FlatFileConnector:
    def __init__(self, kind: str, url: str, options: dict | None = None):
        if kind not in _EXTENSIONS or ("://" in url and not url.startswith("file://")):
            raise ConnectorError("Flat files require a supported format and a local filesystem root")
        self.kind = kind
        self.root = Path(url.removeprefix("file://")).expanduser().resolve()
        self.options = options or {}
        self.max_rows = int(self.options.get("max_rows", 100000))
        if self.max_rows < 1:
            raise ConnectorError("Flat-file max_rows must be positive")
        self.null_token = self.options.get("csv_null_token", "\\N")
        if not isinstance(self.null_token, str) or not self.null_token:
            raise ConnectorError("csv_null_token must be a nonempty string")
        self._prepared: dict[str, TableSchema] = {}
        self.pa = require_dependency("pyarrow", "files") if kind == "parquet" else None
        self.pq = require_dependency("pyarrow.parquet", "files") if kind == "parquet" else None

    def _path(self, table: str) -> Path:
        identifier_parts(table, 1)
        path = self.root / (table + _EXTENSIONS[self.kind])
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ConnectorError("Flat-file tables must stay within the configured root; symlinks are forbidden")
        return path

    def _sidecar(self, table: str) -> Path:
        path = self._path(table).with_suffix(_EXTENSIONS[self.kind] + ".schema.json")
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ConnectorError("Flat-file schema sidecars must stay within the configured root")
        return path

    def _lock(self, table: str) -> FileLock:
        path = self._path(table).with_suffix(_EXTENSIONS[self.kind] + ".lock")
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ConnectorError("Flat-file lock files must stay within the configured root")
        return FileLock(str(path), timeout=30)

    @staticmethod
    def _type_contract(schema: TableSchema) -> tuple:
        return (
            [(column.name, column.data_type, column.nullable) for column in schema.columns],
            {column.name: schema.metadata.get("decimal_types", {}).get(column.name)
             for column in schema.columns if column.data_type == "decimal"},
            {column.name: schema.metadata.get("timestamp_timezones", {}).get(column.name)
             for column in schema.columns if column.data_type == "datetime"},
        )

    def _manifest(self, table: str) -> dict | None:
        path = self._sidecar(table)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_object)
        if data.get("version") != 1 or data.get("format") != self.kind or data.get("codec") != "typed-v1":
            raise ConnectorError("Unsupported flat-file schema manifest")
        return data

    def list_tables(self) -> list[str]:
        if not self.root.exists():
            return []
        result = []
        for path in self.root.glob("*" + _EXTENSIONS[self.kind]):
            self._path(path.stem)
            if path.is_file():
                result.append(path.stem)
        return sorted(result)

    def _parquet_schema(self, table: str) -> TableSchema:
        arrow = self.pq.ParquetFile(self._path(table)).schema_arrow
        columns, decimals, zones = [], {}, {}
        types = self.pa.types
        for field in arrow:
            identifier_parts(field.name, 1)
            typ = field.type
            if types.is_boolean(typ):
                kind = "boolean"
            elif types.is_integer(typ):
                kind = "integer"
            elif types.is_floating(typ):
                kind = "float"
            elif types.is_decimal(typ):
                kind = "decimal"
                decimals[field.name] = {"precision": typ.precision, "scale": typ.scale}
            elif types.is_timestamp(typ):
                if typ.unit == "ns":
                    raise ConnectorError("Nanosecond Parquet timestamps require an explicit reviewed precision conversion")
                kind = "datetime"
                zones[field.name] = typ.tz
            elif types.is_date(typ):
                kind = "date"
            elif types.is_string(typ) or types.is_large_string(typ):
                kind = "string"
            elif types.is_binary(typ) or types.is_large_binary(typ):
                kind = "binary"
            else:
                raise ConnectorError(f"Unsupported nested/complex Parquet type {typ}; use an explicit projection")
            columns.append(Column(field.name, kind, field.nullable))
        return TableSchema(table, columns, [], {"decimal_types": decimals, "timestamp_timezones": zones})

    def inspect_table(self, table: str) -> TableSchema:
        path = self._path(table)
        if not path.is_file():
            raise ConnectorError(f"Flat-file table {table!r} does not exist")
        manifest = self._manifest(table)
        explicit = self.options.get("schemas", {}).get(table)
        key_options = self.options.get("keys", {}).get(table)
        if manifest:
            schema = declared_schema(table, manifest["schema"], key_options)
            if explicit is not None:
                configured = declared_schema(table, explicit, key_options)
                if self._type_contract(configured) != self._type_contract(schema):
                    raise ConnectorError("Explicit schema conflicts with the file manifest")
        elif explicit is not None:
            schema = declared_schema(table, explicit, key_options)
        elif self.kind == "parquet":
            schema = self._parquet_schema(table)
            schema.primary_key = list(key_options or [])
        else:
            raise ConnectorError("CSV/JSONL sources require options.schemas or a tool-generated schema sidecar; types are never guessed")
        if self.kind == "parquet":
            physical = self._parquet_schema(table)
            if [(c.name, c.data_type) for c in physical.columns] != [(c.name, c.data_type) for c in schema.columns]:
                raise ConnectorError("Declared schema does not match physical Parquet types")
            schema.metadata.update(physical.metadata)
        if self.kind == "csv":
            with path.open(newline="", encoding="utf-8") as stream:
                header = next(csv.reader(stream), None)
            if header != [column.name for column in schema.columns]:
                raise ConnectorError("CSV header must match the explicit schema with no duplicate or missing columns")
        representation = {"format": self.kind, "codec": "typed-v1" if manifest else "plain-v1"}
        if self.kind == "csv":
            effective_null_token = manifest.get("csv_null_token", self.null_token) if manifest else self.null_token
            if not isinstance(effective_null_token, str) or not effective_null_token:
                raise ConnectorError("CSV manifest null token must be a nonempty string")
            representation["csv_null_token"] = effective_null_token
        # These parsing rules are source-schema semantics and must participate
        # in the approval signature, even if the column types did not change.
        schema.metadata.update({"file_format": self.kind, "file_representation": representation,
                                "snapshot": "requires_quiesced_source"})
        return schema

    def _row(self, raw: dict, schema: TableSchema) -> dict:
        if set(raw) != {column.name for column in schema.columns}:
            raise ConnectorError("Flat-file row has missing or unexpected columns")
        result = {}
        for column in schema.columns:
            value = raw[column.name]
            if value is None and not column.nullable:
                raise ConnectorError(f"Null in required file column {column.name!r}")
            try:
                result[column.name] = coerce(value, column.data_type)
                if column.data_type == "datetime":
                    # A missing timezone declares a wall-clock value. Do not let
                    # a file schema silently reinterpret an instant as wall time.
                    normalize_datetime(result[column.name], bool(schema.metadata.get("timestamp_timezones", {}).get(column.name)))
            except (ValueError, TypeError, ArithmeticError) as exc:
                raise ConnectorError(f"Invalid typed value in file column {column.name!r}") from exc
        return result

    def _csv_value(self, value: str, column: Column, typed: bool, null_token: str):
        if value == null_token:
            return None
        if column.data_type == "binary":
            try:
                return base64.b64decode(value, validate=True)
            except ValueError as exc:
                raise ConnectorError("CSV binary fields require valid base64") from exc
        if column.data_type == "json":
            parsed = json.loads(value, parse_float=float if typed else Decimal, object_pairs_hook=_json_object)
            return decode_value(parsed) if typed else parsed
        return value

    def read_batches(self, table: str, batch_size: int = 500,
                     limit: int | None = None) -> Iterator[list[dict[str, Any]]]:
        validate_read(batch_size, limit)
        schema = self.inspect_table(table)
        if limit == 0:
            return
        manifest = self._manifest(table)
        typed = manifest is not None
        null_token = manifest.get("csv_null_token", self.null_token) if manifest else self.null_token
        path = self._path(table)
        count, batch = 0, []

        def records():
            if self.kind == "parquet":
                for arrow_batch in self.pq.ParquetFile(path).iter_batches(batch_size=batch_size):
                    yield from arrow_batch.to_pylist()
            elif self.kind == "jsonl":
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        if not line.strip():
                            continue
                        raw = json.loads(line, parse_float=float if typed else Decimal, object_pairs_hook=_json_object)
                        if not isinstance(raw, dict):
                            raise ConnectorError("Each JSONL line must be a row object")
                        yield {key: decode_value(value) for key, value in raw.items()} if typed else raw
            else:
                with path.open(newline="", encoding="utf-8") as stream:
                    reader = csv.reader(stream)
                    next(reader)
                    for values in reader:
                        if len(values) != len(schema.columns):
                            raise ConnectorError("CSV row width differs from the explicit schema")
                        yield {column.name: self._csv_value(value, column, typed, null_token)
                               for column, value in zip(schema.columns, values)}

        for raw in records():
            count += 1
            if count > self.max_rows:
                raise ConnectorError("Flat-file row budget exceeded")
            batch.append(self._row(raw, schema))
            if len(batch) >= batch_size:
                yield batch
                batch = []
            if limit is not None and count >= limit:
                break
        if batch:
            yield batch

    def _arrow(self, schema: TableSchema):
        pa = self.pa
        types = {"integer": pa.int64(), "float": pa.float64(), "string": pa.string(),
                 "boolean": pa.bool_(), "date": pa.date32(), "binary": pa.binary()}
        fields = []
        for column in schema.columns:
            if column.data_type == "datetime":
                typ = pa.timestamp("us", tz=schema.metadata.get("timestamp_timezones", {}).get(column.name))
            elif column.data_type == "decimal":
                spec = schema.metadata.get("decimal_types", {}).get(column.name, {})
                precision, scale = spec.get("precision"), spec.get("scale")
                if precision is None or scale is None:
                    raise ConnectorError("Parquet Decimal fields require explicit precision and scale")
                typ = (pa.decimal128 if precision <= 38 else pa.decimal256)(precision, scale)
            elif column.data_type in types:
                typ = types[column.data_type]
            else:
                raise ConnectorError("Parquet nested JSON requires an explicit JSON-to-string mapping or a reviewed Spark job")
            fields.append(pa.field(column.name, typ, nullable=False if column.name in schema.primary_key else column.nullable))
        return pa.schema(fields)

    def _csv_text(self, value: Any, column: Column) -> str:
        if value is None:
            return self.null_token
        if column.data_type == "json":
            text = json.dumps(encode_value(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        elif isinstance(value, bytes):
            text = base64.b64encode(value).decode("ascii")
        elif isinstance(value, (datetime, date)):
            text = value.isoformat()
        elif isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        if text == self.null_token:
            raise ConnectorError("A literal value collides with csv_null_token; choose a different reviewed null token")
        return text

    def _atomic_data(self, table: str, rows: list[dict], schema: TableSchema) -> None:
        target = self._path(table)
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".migration-", suffix=".tmp")
        os.close(fd)
        temporary = Path(temporary)
        try:
            if self.kind == "parquet":
                arrow = self._arrow(schema)
                for row in rows:
                    for field in arrow:
                        if self.pa.types.is_timestamp(field.type):
                            normalize_datetime(row[field.name], bool(field.type.tz))
                self.pq.write_table(self.pa.Table.from_pylist(rows, schema=arrow), temporary)
            else:
                with temporary.open("w", newline="", encoding="utf-8") as stream:
                    if self.kind == "csv":
                        writer = csv.writer(stream)
                        writer.writerow([column.name for column in schema.columns])
                        for row in rows:
                            writer.writerow([self._csv_text(row[column.name], column) for column in schema.columns])
                    else:
                        for row in rows:
                            stream.write(json.dumps({key: encode_value(value) for key, value in row.items()},
                                                    ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            if hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def prepare_table(self, schema: TableSchema, keys: list[str]) -> None:
        if not keys or not set(keys) <= {column.name for column in schema.columns}:
            raise ConnectorError("Flat-file targets require explicit mapped business keys")
        for column in schema.columns:
            identifier_parts(column.name, 1)
        schema = TableSchema(schema.name, schema.columns, list(keys), dict(schema.metadata))
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(schema.name)
        with self._lock(schema.name):
            if path.exists():
                actual = self.inspect_table(schema.name)
                if [(c.name, c.data_type) for c in actual.columns] != [(c.name, c.data_type) for c in schema.columns]:
                    raise ConnectorError("Existing file schema differs from the mapped target")
                if self._manifest(schema.name) is None:
                    raise ConnectorError("Existing target files require a typed sidecar; import to a new target table first")
                if self._manifest(schema.name)["schema"].get("primary_key") != keys:
                    raise ConnectorError("Existing target file uses different business keys")
                if [(c.name, c.nullable) for c in actual.columns] != [(c.name, c.nullable) for c in schema.columns]:
                    raise ConnectorError("Existing file nullability differs from the mapped contract")
                if self._type_contract(actual) != self._type_contract(schema):
                    raise ConnectorError("Existing file precision/timezone contract differs from the mapping")
                if self.kind == "csv" and self._manifest(schema.name).get("csv_null_token") != self.null_token:
                    raise ConnectorError("Target csv_null_token differs from the existing manifest")
            else:
                # Validate representability before creating the sidecar.
                if self.kind == "parquet":
                    self._arrow(schema)
                manifest = {"version": 1, "format": self.kind, "codec": "typed-v1",
                            "csv_null_token": self.null_token, "schema": asdict(schema)}
                from ..spec import write_json
                write_json(self._sidecar(schema.name), manifest)
                self._atomic_data(schema.name, [], schema)
        self._prepared[schema.name] = schema

    def write_batch(self, table: str, rows: list[dict[str, Any]], keys: list[str]) -> int:
        if not rows:
            return 0
        validate_rows(rows, keys)
        if table not in self._prepared or keys != self._prepared[table].primary_key:
            raise ConnectorError("prepare_table must validate the file schema and keys before writing")
        schema = self._prepared[table]
        incoming = [self._row(row, schema) for row in rows]
        with self._lock(table):
            merged = {}
            for batch in self.read_batches(table):
                for row in batch:
                    key = row_key(row, keys)
                    if key in merged:
                        raise ConnectorError("Existing target file contains duplicate business keys")
                    merged[key] = row
            for row in incoming:
                merged[row_key(row, keys)] = row
            if len(merged) > self.max_rows:
                raise ConnectorError("Flat-file keyed merge exceeds max_rows; use a reviewed bulk job")
            self._atomic_data(table, list(merged.values()), schema)
        return len(rows)

    def close(self) -> None:
        pass
