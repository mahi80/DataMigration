"""Versioned migration contracts. Secrets are resolved only at execution boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DataType = Literal["integer", "float", "decimal", "string", "boolean", "datetime", "date", "json", "binary"]
Transform = Literal["identity", "trim", "lower", "upper", "to_string", "to_integer", "to_decimal", "to_datetime", "json_encode"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


def reject_inline_secrets(value: Any) -> Any:
    forbidden = {"password", "passwd", "secret", "clientsecret", "token", "accesstoken", "apikey",
                 "accesskey", "secretkey", "awsaccesskeyid", "awssecretaccesskey", "sastoken", "accountkey",
                 "connectionstring", "connectionuri", "credentials"}
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in forbidden:
                raise ValueError("Inline credentials are forbidden; use environment references or workload identity")
            reject_inline_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_inline_secrets(item)
    elif isinstance(value, str) and "://" in value:
        from urllib.parse import urlparse
        if urlparse(value).password is not None:
            raise ValueError("Credential-bearing URLs belong in environment variables")
    return value


class Connection(Contract):
    kind: str
    url_env: str
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("options")
    @classmethod
    def no_inline_credentials(cls, value: dict) -> dict:
        return reject_inline_secrets(value)

    @field_validator("url_env")
    @classmethod
    def environment_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("url_env must be an environment variable name, not a connection string")
        return value

    def resolve(self) -> str:
        value = os.environ.get(self.url_env)
        if not value:
            raise ValueError(f"Required environment variable {self.url_env} is not set")
        sql_kinds = {"sqlite": {"sqlite"}, "postgresql": {"postgres", "postgresql"},
                     "postgres": {"postgres", "postgresql"}, "mysql": {"mysql"},
                     "sqlserver": {"mssql", "sqlserver"}, "mssql": {"mssql", "sqlserver"}}
        actual_scheme = value.split(":", 1)[0].split("+")[0]
        if self.kind.lower() in sql_kinds and actual_scheme not in sql_kinds[self.kind.lower()]:
            raise ValueError("Connection URI scheme does not match the declared SQL connector kind")
        if value.startswith("sqlite"):
            from sqlalchemy.engine import make_url
            parsed = make_url(value)
            if not parsed.database or parsed.database == ":memory:" or parsed.query.get("mode") == "memory":
                raise ValueError("Migrations require a durable SQLite file; in-memory endpoints are unsupported")
            if parsed.database.startswith("file:") or parsed.query.get("uri"):
                raise ValueError("Use a plain filesystem SQLite URL; SQLite file URI mode is unsupported")
            return parsed.set(database=str(Path(parsed.database).expanduser().resolve())).render_as_string(hide_password=False)
        if self.kind.lower() in {"delta", "deltalake", "delta_lake", "csv", "jsonl", "parquet"}:
            if value.startswith("file://"):
                return str(Path(value.removeprefix("file://")).expanduser().resolve())
            if "://" not in value:
                return str(Path(value).expanduser().resolve())
        return value

    def resource_identity(self) -> tuple:
        from urllib.parse import unquote, urlparse
        value = self.resolve()
        if self.kind.lower() in {"delta", "deltalake", "delta_lake", "csv", "jsonl", "parquet"} and "://" not in value:
            return ("filesystem", str(Path(value).resolve()))
        parsed = urlparse(value)
        scheme = parsed.scheme.split("+")[0]
        scheme = {"postgres": "postgresql", "mssql": "sqlserver"}.get(scheme, scheme)
        if scheme == "sqlite":
            from sqlalchemy.engine import make_url
            return ("filesystem", str(Path(make_url(value).database).resolve()))
        defaults = {"postgresql": 5432, "mysql": 3306, "sqlserver": 1433, "redis": 6379,
                    "rediss": 6379, "mongodb": 27017, "cassandra": 9042, "redshift": 5439, "https": 443}
        return (scheme, (parsed.hostname or "").lower(), parsed.port or defaults.get(scheme), unquote(parsed.path).rstrip("/"))


class ColumnMapping(Contract):
    source: str
    target: str
    data_type: DataType
    nullable: bool = True
    transform: Transform = "identity"
    description: str | None = None
    precision: int | None = Field(default=None, ge=1, le=76)
    scale: int | None = Field(default=None, ge=0, le=76)
    timezone: str | None = None

    @model_validator(mode="after")
    def decimal_contract(self) -> "ColumnMapping":
        if (self.precision is None) != (self.scale is None):
            raise ValueError("Decimal precision and scale must be supplied together")
        if self.precision is not None and (self.data_type != "decimal" or self.scale > self.precision):
            raise ValueError("Precision/scale must describe a valid decimal column")
        if self.timezone is not None:
            from zoneinfo import ZoneInfo
            if self.data_type != "datetime":
                raise ValueError("timezone applies only to datetime columns")
            ZoneInfo(self.timezone)
        return self


class QualityRule(Contract):
    name: str = Field(min_length=1, max_length=100)
    column: str
    check: Literal["not_null", "non_blank", "unique", "regex", "range", "allowed_values"]
    correction: Literal["none", "trim", "lower", "upper"] = "none"
    pattern: str | None = Field(default=None, max_length=512)
    minimum: str | None = None
    maximum: str | None = None
    values: list[Any] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def rule_parameters(self) -> "QualityRule":
        if self.check == "regex" and not self.pattern:
            raise ValueError("A regex quality rule requires a pattern")
        if self.check == "regex":
            import regex
            try:
                regex.compile(self.pattern)
            except regex.error as exc:
                raise ValueError("Invalid quality-rule regular expression") from exc
        if self.check == "range" and self.minimum is None and self.maximum is None:
            raise ValueError("A range quality rule requires a minimum or maximum")
        if self.check == "range":
            from decimal import Decimal
            bounds = [Decimal(v) for v in (self.minimum, self.maximum) if v is not None]
            if not all(v.is_finite() for v in bounds):
                raise ValueError("Quality rule range bounds must be finite")
            if len(bounds) == 2 and bounds[0] > bounds[1]:
                raise ValueError("Quality range minimum exceeds maximum")
        if self.check == "allowed_values" and not self.values:
            raise ValueError("allowed_values requires explicit accepted values")
        return self


class TableMapping(Contract):
    source: str
    target: str
    keys: list[str] = Field(min_length=1)
    columns: list[ColumnMapping] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    description: str | None = None
    quality_rules: list[QualityRule] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_columns(self) -> "TableMapping":
        if len(set(self.keys)) != len(self.keys):
            raise ValueError("Duplicate target keys are not allowed")
        names = [c.target for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("Each target column can be mapped only once")
        if self.columns and not set(self.keys) <= set(names):
            raise ValueError("All keys must refer to mapped TARGET columns")
        if len({rule.name for rule in self.quality_rules}) != len(self.quality_rules):
            raise ValueError("Quality rule names must be unique within a table")
        for rule in self.quality_rules:
            if self.columns and rule.column not in names:
                raise ValueError("Quality rules refer to mapped TARGET columns")
            if rule.column in self.keys and rule.correction != "none":
                raise ValueError("Automatic quality corrections cannot modify migration keys")
        return self


class Policy(Contract):
    source_quiesced: bool = False
    batch_size: int = Field(default=500, ge=1, le=10000)
    max_rows_per_table: int = Field(default=100000, ge=1, le=10000000)
    profile_rows: int = Field(default=1000, ge=1, le=10000)
    target_scope: Literal["mapped_keys", "entire_table"] = "entire_table"
    allow_create_tables: bool = True


class AdvisorConfig(Contract):
    provider: Literal["disabled", "openai_compatible", "azure", "databricks", "bedrock"] = "disabled"
    model: str | None = None
    base_url_env: str = "MIGRATION_LLM_BASE_URL"
    api_key_env: str = "MIGRATION_LLM_API_KEY"
    region: str | None = None
    max_tokens: int = Field(default=2048, ge=128, le=8192)


class MigrationSpec(Contract):
    spec_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=128)
    source: Connection
    target: Connection
    mode: Literal["snapshot", "full_load_cdc"] = "snapshot"
    workload: Literal["oltp", "olap", "star_schema", "document", "key_value", "wide_column", "lakehouse"] = "oltp"
    tables: list[TableMapping] = Field(min_length=1)
    policy: Policy = Field(default_factory=Policy)
    advisor: AdvisorConfig = Field(default_factory=AdvisorConfig)
    cloud: dict[str, Any] = Field(default_factory=dict)

    @field_validator("cloud")
    @classmethod
    def cloud_credentials(cls, value: dict) -> dict:
        return reject_inline_secrets(value)

    @model_validator(mode="after")
    def dependency_graph(self) -> "MigrationSpec":
        names = [t.target for t in self.tables]
        if len(set(names)) != len(names):
            raise ValueError("Each target table may appear only once in a plan")
        for table in self.tables:
            if not set(table.depends_on) <= set(names):
                raise ValueError(f"Unknown dependency for target {table.target}")
        self.ordered_tables()
        return self

    def ordered_tables(self) -> list[TableMapping]:
        pending = {t.target: t for t in self.tables}
        completed: set[str] = set()
        ordered: list[TableMapping] = []
        while pending:
            ready = [t for t in pending.values() if set(t.depends_on) <= completed]
            if not ready:
                raise ValueError("Cyclic dependencies require a separately reviewed staging strategy")
            for table in ready:
                ordered.append(table)
                completed.add(table.target)
                del pending[table.target]
        return ordered

    def connection_binding(self) -> dict[str, str]:
        return {
            role: hashlib.sha256(getattr(self, role).resolve().encode()).hexdigest()
            for role in ("source", "target")
        }


def load_spec(path: str | Path) -> MigrationSpec:
    with Path(path).open(encoding="utf-8") as stream:
        return MigrationSpec.model_validate(yaml.safe_load(stream))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)
