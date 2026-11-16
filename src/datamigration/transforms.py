"""Small audited transformation vocabulary; no arbitrary eval or generated SQL execution."""

from __future__ import annotations

import base64
import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from zoneinfo import ZoneInfo

from .spec import ColumnMapping, TableMapping, canonical_json


def encode_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite decimal is not supported")
        return {"$decimal": format(value, "f")}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$binary": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, list):
        return [encode_value(x) for x in value]
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("JSON objects require string keys")
        # Wrap user objects so a real {"$decimal": "..."} cannot collide with our tagged codec.
        return {"$object": {k: encode_value(v) for k, v in value.items()}}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"Unsupported value type: {type(value).__name__}")


def decode_value(value: Any) -> Any:
    if isinstance(value, list):
        return [decode_value(x) for x in value]
    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            return Decimal(value["$decimal"])
        if set(value) == {"$datetime"}:
            return datetime.fromisoformat(value["$datetime"])
        if set(value) == {"$date"}:
            return date.fromisoformat(value["$date"])
        if set(value) == {"$binary"}:
            return base64.b64decode(value["$binary"], validate=True)
        if set(value) == {"$object"}:
            return {k: decode_value(v) for k, v in value["$object"].items()}
    return value


def coerce(value: Any, data_type: str) -> Any:
    if value is None:
        return None
    if data_type == "string":
        if not isinstance(value, str):
            raise ValueError("Expected text; declare to_string or json_encode explicitly")
        return value
    if data_type == "integer":
        try:
            number = Decimal(str(value))
            if not number.is_finite() or number != number.to_integral_value():
                raise ValueError("Integer conversion would lose information")
            return int(number)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("Invalid integer") from exc
    if data_type == "decimal":
        try:
            result = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("Invalid decimal") from exc
        if not result.is_finite():
            raise ValueError("Non-finite decimal")
        return result
    if data_type == "float":
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Non-finite float")
        if isinstance(value, (int, Decimal)) and Decimal(str(result)) != Decimal(value):
            raise ValueError("Float conversion would lose significant digits")
        return result
    if data_type == "boolean":
        if isinstance(value, bool):
            return value
        if value in (0, 1, "0", "1", "true", "false", "True", "False"):
            return value in (1, "1", "true", "True")
        raise ValueError("Ambiguous boolean")
    if data_type == "datetime":
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        raise ValueError("Expected ISO datetime")
    if data_type == "date":
        if isinstance(value, datetime):
            raise ValueError("Timestamp-to-date truncation requires a custom reviewed job")
        return value if isinstance(value, date) else date.fromisoformat(value)
    if data_type == "json":
        result = json.loads(value) if isinstance(value, str) else value
        encode_value(result)
        return result
    if data_type == "binary":
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ValueError("Expected binary value")
        return bytes(value)
    raise ValueError(f"Unsupported data type {data_type}")


def apply_column(value: Any, column: ColumnMapping) -> Any:
    if value is None:
        if not column.nullable:
            raise ValueError(f"Null in required target column {column.target}")
        return None
    op = column.transform
    if op in {"trim", "lower", "upper"}:
        if not isinstance(value, str):
            raise ValueError(f"{op} requires text in {column.source}")
        value = {"trim": str.strip, "lower": str.lower, "upper": str.upper}[op](value)
    elif op == "to_string":
        if isinstance(value, (dict, list, bytes, bytearray)):
            raise ValueError("Use json_encode for structured values")
        value = str(value)
    elif op == "json_encode":
        value = canonical_json(value)
    elif op == "to_integer":
        value = coerce(value, "integer")
    elif op == "to_decimal":
        value = coerce(value, "decimal")
    elif op == "to_datetime":
        value = coerce(value, "datetime")
    result = coerce(value, column.data_type)
    if isinstance(result, datetime) and column.timezone:
        if result.tzinfo is None:
            raise ValueError("A naive timestamp needs an explicitly reviewed timezone interpretation")
        result = result.astimezone(ZoneInfo(column.timezone))
    if isinstance(result, Decimal) and column.precision is not None:
        with localcontext() as context:
            context.prec = max(column.precision + 2, len(result.as_tuple().digits) + 2)
            quantized = result.quantize(Decimal(1).scaleb(-column.scale))
            if quantized != result or abs(result) >= Decimal(10) ** (column.precision - column.scale):
                raise ValueError(f"Decimal does not fit approved precision/scale for {column.target}")
            result = quantized
    return result


def transform_row(row: dict[str, Any], table: TableMapping) -> dict[str, Any]:
    missing = [c.source for c in table.columns if c.source not in row]
    if missing:
        raise ValueError(f"Source row missing columns: {', '.join(missing)}")
    result = {}
    for column in table.columns:
        try:
            result[column.target] = apply_column(row[column.source], column)
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise ValueError(f"Invalid value for target column {column.target} ({column.data_type})") from exc
    if any(result[k] is None for k in table.keys):
        raise ValueError(f"Null migration key for target {table.target}")
    return result


def canonical_row(row: dict[str, Any], columns: list[ColumnMapping]) -> str:
    values = {}
    for column in columns:
        value = coerce(row[column.target], column.data_type)
        values[column.target] = encode_value(equality_value(value))
    return canonical_json(values)


def row_key(row: dict[str, Any], keys: list[str]) -> str:
    if any(row.get(key) is None for key in keys):
        raise ValueError("Null or missing migration key")
    values = []
    for key in keys:
        value = row[key]
        values.append(encode_value(equality_value(value)))
    return canonical_json(values)


def equality_value(value: Any) -> Any:
    """Canonical scalar equality for keys, reconciliation and quality checks.

    Preserve exact Decimal digits and distinguish naive wall time from instants.
    SQL numeric equality treats signed floating-point zero as the same value.
    Text remains case-sensitive unless an approved transform changes it.
    """
    if isinstance(value, Decimal):
        return normalized_decimal(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    if isinstance(value, float) and value == 0:
        return 0.0
    return value


def normalized_decimal(value: Decimal) -> Decimal:
    if value == 0:
        return Decimal(0)
    text = format(value, "f")
    return Decimal(text.rstrip("0").rstrip(".") if "." in text else text)
