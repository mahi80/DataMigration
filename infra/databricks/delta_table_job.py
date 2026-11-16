"""Databricks Runtime job: pinned Delta tables -> reviewed keyed Delta tables.

Run on Databricks Runtime with Spark and Delta available. This example supports
identity/rename mappings of primitive columns within one workspace. It does not
implement CDC, SCD2, arbitrary SQL, inferred star-schema construction or deletes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def identifier(value: str) -> str:
    parts = value.split(".")
    if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
        raise ValueError("Use fully qualified simple Unity Catalog identifiers: catalog.schema.table")
    return ".".join(f"`{part}`" for part in parts)


def column_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("This job requires simple explicit column identifiers")
    return f"`{value}`"


def sha(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def ordered_tables(tables: list[dict]) -> list[dict]:
    pending = {table["target"]: table for table in tables}
    if len(pending) != len(tables):
        raise ValueError("Duplicate target tables")
    completed, result = set(), []
    while pending:
        ready = [table for table in pending.values() if set(table.get("depends_on", [])) <= completed]
        if not ready:
            raise ValueError("Cyclic or unresolved table dependencies")
        for table in ready:
            result.append(table)
            completed.add(table["target"])
            del pending[table["target"]]
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--plan-hash", required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    if not args.manifest.startswith("/Volumes/"):
        raise ValueError("Example job reads a reviewed JSON manifest from a Unity Catalog volume")
    spec = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if sha(spec) != args.plan_hash:
        raise ValueError("Manifest changed after approval")
    if spec.get("mode", "snapshot") != "snapshot":
        raise ValueError("This example handles a pinned snapshot only")
    if spec["source"]["kind"] != "databricks" or spec["target"]["kind"] != "databricks":
        raise ValueError("This job handles Delta catalog tables in one workspace only")
    if any(table.get("quality_rules") for table in spec.get("tables", [])):
        raise ValueError("This reference job does not implement quality_rules or corrections")
    if not re.fullmatch(r"[a-f0-9]{64}", args.operation_id):
        raise ValueError("Expected stable hashed migration operation ID")
    from pyspark.sql import SparkSession, functions as F
    from pyspark.sql.types import (
        BinaryType, BooleanType, ByteType, DateType, DecimalType, DoubleType,
        FloatType, IntegerType, LongType, ShortType, StringType, TimestampType,
    )
    from delta.tables import DeltaTable

    spark = SparkSession.builder.getOrCreate()
    spark.conf.set("spark.sql.ansi.enabled", "true")
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    versions = spec["cloud"].get("source_versions", {})
    policy = spec.get("policy", {})
    max_rows = int(policy.get("max_rows_per_table", 100000))
    scope = policy.get("target_scope", "entire_table")
    if scope not in {"entire_table", "mapped_keys"} or max_rows < 1:
        raise ValueError("Unsupported target scope or row budget")
    reports = []
    type_groups = {
        "integer": (ByteType, ShortType, IntegerType, LongType), "float": (FloatType, DoubleType),
        "decimal": (DecimalType,), "string": (StringType,), "boolean": (BooleanType,),
        "datetime": (TimestampType,), "date": (DateType,), "binary": (BinaryType,),
    }
    tables = ordered_tables(spec["tables"])
    if {table["source"] for table in tables} & {table["target"] for table in tables}:
        raise ValueError("Source and target tables must not overlap")
    for table in tables:
        identifier(table["source"])
        identifier(table["target"])
        version = versions.get(table["source"])
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ValueError("Each source must have an explicitly approved Delta version")
        columns, keys = table["columns"], table["keys"]
        if not columns or not keys:
            raise ValueError("Explicit columns and keys are required")
        source = spark.read.format("delta").option("versionAsOf", version).table(identifier(table["source"]))
        fields = {field.name: field.dataType for field in source.schema}
        for column in columns:
            column_name(column["source"])
            column_name(column["target"])
            if column.get("transform", "identity") != "identity":
                raise ValueError("Use a reviewed custom job for transformations beyond identity/rename")
            expected_types = type_groups.get(column["data_type"])
            if not expected_types or not isinstance(fields.get(column["source"]), expected_types):
                raise ValueError(f"Source column type does not match the approved mapping: {column['source']}")
            field_type = fields[column["source"]]
            if isinstance(field_type, DecimalType):
                if column.get("precision") is not None and field_type.precision != column["precision"]:
                    raise ValueError("Decimal precision differs from the approved mapping")
                if column.get("scale") is not None and field_type.scale != column["scale"]:
                    raise ValueError("Decimal scale differs from the approved mapping")
            if column.get("timezone") not in {None, "UTC"}:
                raise ValueError("This reference job supports UTC timestamp semantics only")
        names = [column["target"] for column in columns]
        if len(set(names)) != len(names) or not set(keys) <= set(names):
            raise ValueError("Invalid target keys or duplicate mapped names")
        for key in keys:
            column_name(key)
        source = source.select(*[F.col(column_name(column["source"])).alias(column["target"]) for column in columns])
        count = source.count()
        if count > max_rows:
            raise ValueError("Reviewed source row budget exceeded")
        null_key = " OR ".join(f"{column_name(key)} IS NULL" for key in keys)
        if source.where(null_key).limit(1).count() or source.groupBy(*keys).count().where("count > 1").limit(1).count():
            raise ValueError("Null or duplicate source keys")
        for column in columns:
            if not column.get("nullable", True) and source.where(F.col(column_name(column["target"])).isNull()).limit(1).count():
                raise ValueError(f"Non-null constraint failed: {column['target']}")
        target_name = identifier(table["target"])
        if not spark.catalog.tableExists(table["target"]):
            if not policy.get("allow_create_tables", False):
                raise ValueError("Target table is missing and creation is not approved")
            source.limit(0).write.format("delta").mode("errorifexists").saveAsTable(target_name)
        target = spark.table(target_name)
        target_types = {field.name: field.dataType for field in target.schema}
        if any(target_types.get(field.name) != field.dataType for field in source.schema):
            raise ValueError("Target Delta schema differs; no implicit casting or evolution is allowed")
        if target.where(null_key).limit(1).count() or target.groupBy(*keys).count().where("count > 1").limit(1).count():
            raise ValueError("Null or duplicate target keys")
        assignments = {name: f"source.{column_name(name)}" for name in names}
        predicate = " AND ".join(f"target.{column_name(key)} = source.{column_name(key)}" for key in keys)
        (DeltaTable.forName(spark, target_name).alias("target").merge(source.alias("source"), predicate)
         .whenMatchedUpdate(set=assignments).whenNotMatchedInsert(values=assignments).execute())
        # Re-read the committed target. Compare complete typed projected rows,
        # retaining duplicates via exceptAll, not only row counts or model scores.
        target = spark.table(target_name).select(*[F.col(column_name(name)) for name in names])
        if target.count() > max_rows:
            raise ValueError("Reviewed target reconciliation row budget exceeded")
        compared = target.join(source.select(*keys), keys, "left_semi").select(*[F.col(column_name(name)) for name in names]) if scope == "mapped_keys" else target
        missing_or_changed = source.exceptAll(compared).count()
        extra_or_changed = compared.exceptAll(source).count()
        report = {"table": table["target"], "source_version": version, "expected_rows": count,
                  "missing_or_changed": missing_or_changed, "extra_or_changed": extra_or_changed,
                  "passed": missing_or_changed == 0 and extra_or_changed == 0}
        reports.append(report)
        print(json.dumps(report, sort_keys=True))
        if not report["passed"]:
            raise ValueError("Delta reconciliation failed; target writes are retained for investigation")
    print(json.dumps({"operation_id": args.operation_id, "plan_hash": args.plan_hash,
                      "passed": True, "tables": reports}, sort_keys=True))


if __name__ == "__main__":
    main()
