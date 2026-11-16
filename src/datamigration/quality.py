"""Deterministic quality gates and explicitly approved, auditable corrections."""

from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation

import regex

from .spec import TableMapping, canonical_json
from .transforms import coerce, encode_value, equality_value


def check_quality(row: dict, table: TableMapping, db: sqlite3.Connection, statistics: dict) -> dict:
    result = dict(row)
    types = {column.target: column.data_type for column in table.columns}
    # Normalize first, then validate every constraint against the final row.
    # Otherwise a later lowercase correction could invalidate an earlier uniqueness check.
    for rule in table.quality_rules:
        item = statistics.setdefault(rule.name, {"column": rule.column, "checked": 0, "corrected": 0, "check": rule.check})
        item["checked"] += 1
        value = result[rule.column]
        if rule.correction != "none" and value is not None:
            if not isinstance(value, str):
                raise ValueError(f"Quality correction {rule.name} requires a text column")
            updated = {"trim": str.strip, "lower": str.lower, "upper": str.upper}[rule.correction](value)
            if updated != value:
                item["corrected"] += 1
                value = coerce(updated, types[rule.column])
                result[rule.column] = value
    for rule in table.quality_rules:
        value = result[rule.column]
        passed = False
        try:
            if rule.check == "not_null":
                passed = value is not None
            elif rule.check == "non_blank":
                passed = isinstance(value, str) and bool(value.strip())
            elif rule.check == "regex":
                passed = isinstance(value, str) and regex.fullmatch(rule.pattern, value, timeout=0.05) is not None
            elif rule.check == "range":
                number = Decimal(str(value))
                passed = number.is_finite() and (rule.minimum is None or number >= Decimal(rule.minimum)) and (rule.maximum is None or number <= Decimal(rule.maximum))
            elif rule.check == "allowed_values":
                candidate = canonical_json(encode_value(equality_value(value)))
                passed = candidate in {canonical_json(encode_value(equality_value(coerce(item, types[rule.column])))) for item in rule.values}
            elif rule.check == "unique":
                unique_value = equality_value(value)
                if unique_value is not None:
                    db.execute("INSERT INTO quality_seen VALUES(?,?)", (rule.name, canonical_json(encode_value(unique_value))))
                    passed = True
        except (ValueError, TypeError, InvalidOperation, sqlite3.IntegrityError, TimeoutError):
            passed = False
        if not passed:
            raise ValueError(f"Data-quality rule {rule.name} failed on target {table.target}.{rule.column}; no values are logged")
    return result
