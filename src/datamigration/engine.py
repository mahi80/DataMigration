"""Bounded snapshot migration with durable staging and exact deterministic reconciliation.

The caller must quiesce sources. This worker does not claim cross-table snapshot isolation
or CDC. Staging is completed for every table before the first destination write.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any
from filelock import FileLock

from .connectors import create_connector
from .connectors.base import Column, TableSchema
from .ledger import Ledger
from .quality import check_quality
from .spec import ColumnMapping, MigrationSpec, TableMapping, fingerprint, write_json
from .transforms import canonical_row, coerce, decode_value, encode_value, row_key, transform_row


def connector(connection):
    return create_connector(connection.kind, connection.resolve(), connection.options)


def discover(spec: MigrationSpec) -> dict[str, Any]:
    source = connector(spec.source)
    tables = []
    try:
        for mapping in spec.tables:
            schema = asdict(source.inspect_table(mapping.source))
            nulls = {c["name"]: 0 for c in schema["columns"]}
            sampled = 0
            for rows in source.read_batches(mapping.source, batch_size=min(500, spec.policy.profile_rows), limit=spec.policy.profile_rows):
                for row in rows:
                    sampled += 1
                    for name in nulls:
                        nulls[name] += row.get(name) is None
            schema["profile"] = {"sampled_rows": sampled, "nulls_in_sample": nulls, "population_statistics": False}
            tables.append(schema)
    finally:
        source.close()
    return {"source_kind": spec.source.kind, "tables": tables, "scope": "requested_tables", "raw_samples_included": False}


def schema_signature(catalog: dict) -> str:
    return fingerprint([{k: v for k, v in table.items() if k != "profile"} for table in catalog["tables"]])


def build_plan(spec: MigrationSpec, catalog: dict) -> dict[str, Any]:
    if spec.mode != "snapshot":
        raise ValueError("The local worker supports snapshot mode. Compile a reviewed native cloud CDC workflow instead")
    if not spec.policy.source_quiesced:
        raise ValueError("Snapshot migration requires policy.source_quiesced=true after arranging a write freeze or immutable snapshot")
    by_name = {t["name"]: t for t in catalog["tables"]}
    resolved = spec.model_copy(deep=True)
    for table in resolved.tables:
        source_columns = by_name[table.source]["columns"]
        available = {c["name"] for c in source_columns}
        if not table.columns:
            table.columns = [ColumnMapping(source=c["name"], target=c["name"], data_type=c["data_type"], nullable=c.get("nullable", True)) for c in source_columns]
        decimal_types = by_name[table.source].get("metadata", {}).get("decimal_types", {})
        timestamp_timezones = by_name[table.source].get("metadata", {}).get("timestamp_timezones", {})
        for column in table.columns:
            if column.data_type == "decimal" and column.precision is None and column.source in decimal_types:
                decimal_type = decimal_types[column.source]
                if decimal_type.get("precision") is not None and decimal_type.get("scale") is not None:
                    column.precision, column.scale = decimal_type["precision"], decimal_type["scale"]
            if column.data_type == "datetime" and column.timezone is None:
                column.timezone = timestamp_timezones.get(column.source)
        if any(c.source not in available for c in table.columns):
            raise ValueError(f"Mapping contains unknown source column for {table.source}")
        TableMapping.model_validate(table.model_dump())
    if spec.source.resource_identity() == spec.target.resource_identity():
        raise ValueError("The snapshot runner requires distinct source and destination resources; use a reviewed native job for changes within one database/workspace")
    body = {
        "spec": resolved.model_dump(mode="json"),
        "connection_binding": spec.connection_binding(),
        "schema_signature": schema_signature(catalog),
        "contract": "snapshot_upsert_v1",
        "lineage": [{"source": t.source, "target": t.target,
                     "columns": [{"source": c.source, "target": c.target, "transform": c.transform} for c in t.columns],
                     "quality_rules": [rule.model_dump(mode="json") for rule in t.quality_rules]} for t in resolved.tables],
    }
    return {**body, "plan_hash": fingerprint(body)}


def validate_plan(plan: dict) -> MigrationSpec:
    supplied = plan.get("plan_hash")
    if supplied != fingerprint({k: v for k, v in plan.items() if k != "plan_hash"}):
        raise ValueError("Plan integrity check failed")
    spec = MigrationSpec.model_validate(plan["spec"])
    if spec.connection_binding() != plan["connection_binding"]:
        raise ValueError("Connection environment changed after planning; create a new plan and approval")
    return spec


def stage_path(run_dir: Path, table: str) -> Path:
    return run_dir / "staging" / (hashlib.sha256(table.encode()).hexdigest()[:24] + ".sqlite")


def stage_table(source, table: TableMapping, spec: MigrationSpec, run_dir: Path, plan_hash: str) -> dict:
    path = stage_path(run_dir, table.target)
    receipt = path.with_suffix(".json")
    if receipt.exists():
        metadata = json.loads(receipt.read_text())
        if metadata["plan_hash"] != plan_hash or not path.exists():
            raise ValueError("Staging receipt does not match this plan")
        with sqlite3.connect(path) as db:
            count = db.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
            if count != metadata["row_count"]:
                raise ValueError("Staging receipt row count mismatch")
            actual_digest = hashlib.sha256()
            for payload, digest in db.execute("SELECT payload,digest FROM rows ORDER BY sequence"):
                row = {k: decode_value(v) for k, v in json.loads(payload).items()}
                if hashlib.sha256(canonical_row(row, table.columns).encode()).hexdigest() != digest:
                    raise ValueError("Staged row integrity check failed")
                actual_digest.update(digest.encode())
            if actual_digest.hexdigest() != metadata["content_digest"]:
                raise ValueError("Staged data integrity check failed")
        return metadata
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # An incomplete staging database has never been used for writes; rebuild it.
    if path.exists():
        path.unlink()
    count = 0
    content_digest = hashlib.sha256()
    quality_statistics = {}
    transformation_counts = {column.target: 0 for column in table.columns}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE rows (sequence INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL)")
        db.execute("CREATE TABLE quality_seen (rule TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(rule,value))")
        for batch in source.read_batches(table.source, batch_size=spec.policy.batch_size):
            for source_row in batch:
                count += 1
                if count > spec.policy.max_rows_per_table:
                    raise ValueError("Snapshot row budget exceeded; increase the reviewed budget or use a cloud worker")
                try:
                    row = transform_row(source_row, table)
                    row = check_quality(row, table, db, quality_statistics)
                except ValueError as exc:
                    write_json(run_dir / "quality_failure.json", {
                        "plan_hash": plan_hash, "table": table.target,
                        "source_row_ordinal": count, "error": str(exc), "raw_values_included": False,
                    })
                    raise
                for column in table.columns:
                    if encode_value(source_row[column.source]) != encode_value(row[column.target]):
                        transformation_counts[column.target] += 1
                digest = hashlib.sha256(canonical_row(row, table.columns).encode()).hexdigest()
                payload = json.dumps({k: encode_value(v) for k, v in row.items()}, sort_keys=True, allow_nan=False)
                try:
                    db.execute("INSERT INTO rows VALUES(?,?,?,?)", (count, row_key(row, table.keys), payload, digest))
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f"Duplicate migration keys in source mapping for {table.target}") from exc
                content_digest.update(digest.encode())
    metadata = {"plan_hash": plan_hash, "table": table.target, "row_count": count, "content_digest": content_digest.hexdigest(),
                "quality_rules": quality_statistics, "transformed_values_by_column": transformation_counts}
    write_json(receipt, metadata)
    return metadata


def reconcile_table(target, table: TableMapping, spec: MigrationSpec, run_dir: Path) -> dict:
    mismatches = []
    mismatch_count = 0
    actual_count, extra_count, duplicate_count = 0, 0, 0
    with sqlite3.connect(stage_path(run_dir, table.target)) as db:
        db.execute("CREATE TEMP TABLE seen (key TEXT PRIMARY KEY)")
        expected_count = db.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        for batch in target.read_batches(table.target, batch_size=spec.policy.batch_size):
            for original in batch:
                actual_count += 1
                if actual_count > spec.policy.max_rows_per_table:
                    raise ValueError("Target reconciliation exceeds reviewed row budget")
                missing = [c.target for c in table.columns if c.target not in original]
                if missing:
                    raise ValueError(f"Target row lacks mapped columns for {table.target}")
                row = {c.target: coerce(original[c.target], c.data_type) for c in table.columns}
                key = row_key(row, table.keys)
                try:
                    db.execute("INSERT INTO seen VALUES(?)", (key,))
                except sqlite3.IntegrityError:
                    duplicate_count += 1
                expected = db.execute("SELECT digest FROM rows WHERE key=?", (key,)).fetchone()
                if expected is None:
                    extra_count += 1
                elif hashlib.sha256(canonical_row(row, table.columns).encode()).hexdigest() != expected[0]:
                    mismatch_count += 1
                    if len(mismatches) < 10:
                        mismatches.append(hashlib.sha256(key.encode()).hexdigest())
        missing_count = db.execute("SELECT COUNT(*) FROM rows WHERE key NOT IN (SELECT key FROM seen)").fetchone()[0]
    passed = not missing_count and not mismatch_count and not duplicate_count and (not extra_count or spec.policy.target_scope == "mapped_keys")
    staged = json.loads(stage_path(run_dir, table.target).with_suffix(".json").read_text())
    return {
        "table": table.target, "passed": bool(passed), "expected_rows": expected_count,
        "target_rows": actual_count, "missing_keys": missing_count, "extra_keys": extra_count,
        "duplicate_keys": duplicate_count, "mismatched_rows": mismatch_count,
        "mismatched_key_hashes": mismatches[:10], "scope": spec.policy.target_scope,
        "comparison": "all_mapped_columns_after_approved_transforms",
        "quality_rules": staged.get("quality_rules", {}),
        "transformed_values_by_column": staged.get("transformed_values_by_column", {}),
    }


def execute_snapshot(plan: dict, run_id: str, run_dir: Path, ledger: Ledger) -> dict:
    spec = validate_plan(plan)
    locks = ledger.path.parent / "target-locks"
    locks.mkdir(parents=True, exist_ok=True)
    with FileLock(str(locks / (fingerprint(spec.target.resource_identity()) + ".lock")), timeout=0):
        return _execute_snapshot(plan, run_id, run_dir, ledger)


def _execute_snapshot(plan: dict, run_id: str, run_dir: Path, ledger: Ledger) -> dict:
    spec = validate_plan(plan)
    if not ledger.approved(run_id, plan["plan_hash"]):
        raise ValueError("This exact migration plan has not been approved")
    source, target = connector(spec.source), None
    try:
        current = {"tables": [asdict(source.inspect_table(t.source)) for t in spec.tables]}
        if schema_signature(current) != plan["schema_signature"]:
            raise ValueError("Source schema changed after approval; create and approve a new plan")
        for table in spec.ordered_tables():
            stage_table(source, table, spec, run_dir, plan["plan_hash"])
    finally:
        source.close()
    target = connector(spec.target)
    report = {"run_id": run_id, "plan_hash": plan["plan_hash"], "mode": "snapshot", "tables": []}
    try:
        for table in spec.ordered_tables():
            if not spec.policy.allow_create_tables:
                target.inspect_table(table.target)
            decimal_types = {c.target: {"precision": c.precision, "scale": c.scale} for c in table.columns if c.precision is not None}
            timestamp_timezones = {c.target: c.timezone for c in table.columns if c.data_type == "datetime"}
            schema = TableSchema(name=table.target, columns=[Column(c.target, c.data_type, c.nullable) for c in table.columns], primary_key=table.keys, metadata={"decimal_types": decimal_types, "timestamp_timezones": timestamp_timezones})
            target.prepare_table(schema, table.keys)
            with sqlite3.connect(stage_path(run_dir, table.target)) as db:
                cursor = db.execute("SELECT payload,digest FROM rows ORDER BY sequence")
                batch_no = 0
                while batch := cursor.fetchmany(spec.policy.batch_size):
                    digest = fingerprint([row[1] for row in batch])
                    if not ledger.completed(run_id, plan["plan_hash"], table.target, batch_no, digest):
                        rows = [{k: decode_value(v) for k, v in json.loads(payload).items()} for payload, _ in batch]
                        written = target.write_batch(table.target, rows, table.keys)
                        if written != len(rows):
                            raise ValueError("Connector did not acknowledge the complete batch")
                        ledger.complete(run_id, plan["plan_hash"], table.target, batch_no, digest, len(rows))
                    batch_no += 1
            report["tables"].append(reconcile_table(target, table, spec, run_dir))
    finally:
        target.close()
    report["passed"] = all(t["passed"] for t in report["tables"])
    write_json(run_dir / "validation.json", report)
    if not report["passed"]:
        raise ValueError("Reconciliation failed; inspect validation.json. Target writes are retained for investigation")
    return report
