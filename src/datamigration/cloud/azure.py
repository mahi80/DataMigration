"""Azure Data Factory SQL-copy compiler and safely accounted job submission."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import (
    CloudConfigurationError, SubmissionLedger, SubmissionUnknown, config_for,
    digest, make_bundle, operation_token, ordered_tables, require, validate_bundle,
)


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise CloudConfigurationError(f"Use simple reviewed SQL identifiers in the ADF template: {value!r}")
    return value


def _table_parts(name: str, default_schema: str) -> tuple[str, str]:
    parts = name.split(".")
    if len(parts) == 1:
        return _identifier(default_schema), _identifier(parts[0])
    if len(parts) == 2:
        return _identifier(parts[0]), _identifier(parts[1])
    raise CloudConfigurationError("ADF table names must be table or schema.table")


def arm_template(factory_name: str, datasets: list[dict], pipeline: dict) -> dict:
    """Deploy only datasets/pipeline into an existing factory; no credentials."""
    resources = []
    for dataset in datasets:
        resources.append({"type": "Microsoft.DataFactory/factories/datasets", "apiVersion": "2018-06-01",
                          "name": "[concat(parameters('factoryName'), '/" + dataset["name"] + "')]",
                          "properties": dataset["properties"]})
    resources.append({"type": "Microsoft.DataFactory/factories/pipelines", "apiVersion": "2018-06-01",
                      "name": "[concat(parameters('factoryName'), '/" + pipeline["name"] + "')]",
                      "dependsOn": ["[resourceId('Microsoft.DataFactory/factories/datasets', parameters('factoryName'), '" + dataset["name"] + "')]" for dataset in datasets],
                      "properties": pipeline["properties"]})
    return {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
            "contentVersion": "1.0.0.0", "parameters": {"factoryName": {"type": "string", "defaultValue": factory_name}},
            "resources": resources}


def compile_plan(spec: dict[str, Any]) -> dict[str, Any]:
    config = config_for(spec, "azure")
    require(config, "subscription_id", "resource_group", "factory_name", "pipeline_name",
            "source_linked_service", "target_linked_service")
    if spec.get("mode", "snapshot") not in {"snapshot", "batch"}:
        raise CloudConfigurationError("ADF SQL Copy template is snapshot/batch only; it does not implement CDC")
    source_kind = spec.get("source", {}).get("kind", "sqlserver")
    target_kind = spec.get("target", {}).get("kind", "azure_sql")
    source_types = {
        "sqlserver": ("SqlServerTable", "SqlSource"),
        "mssql": ("SqlServerTable", "SqlSource"),
        "azure_sql": ("AzureSqlTable", "AzureSqlSource"),
    }
    if source_kind not in source_types or target_kind != "azure_sql":
        raise CloudConfigurationError("ADF generator currently supports SQL Server/Azure SQL sources to Azure SQL; other connectors need reviewed templates")
    tables = spec.get("tables", [])
    if not tables:
        raise CloudConfigurationError("At least one explicitly mapped table is required")
    datasets: list[dict] = []
    activities: list[dict] = []
    for index, table in enumerate(ordered_tables(tables)):
        require(table, "source", "target", "keys", "columns")
        if table.get("quality_rules"):
            raise CloudConfigurationError("ADF Copy does not implement quality_rules or corrections; use a reviewed validation/transform worker")
        if not table["keys"] or not table["columns"]:
            raise CloudConfigurationError("ADF upsert needs explicit target keys and column mappings")
        source_schema, source_table = _table_parts(table["source"], config.get("source_schema", "dbo"))
        target_schema, target_table = _table_parts(table["target"], config.get("target_schema", "dbo"))
        mappings = []
        for column in table["columns"]:
            require(column, "source", "target")
            if any(column.get(key) is not None for key in ("precision", "scale", "timezone")):
                raise CloudConfigurationError("ADF Copy template cannot enforce declared precision/scale/timezone; use a reviewed typed worker")
            if column.get("transform", "identity") not in {None, "identity"}:
                raise CloudConfigurationError("ADF Copy generator supports rename/identity only; transformations require a reviewed Data Flow/job")
            mappings.append({"source": {"name": column["source"]}, "sink": {"name": column["target"]}})
        target_columns = [column["target"] for column in table["columns"]]
        if len(target_columns) != len(set(target_columns)):
            raise CloudConfigurationError("Duplicate target columns")
        if not set(table["keys"]).issubset(target_columns):
            raise CloudConfigurationError("Every upsert key must have a target column mapping")
        dataset_prefix = "migration_" + digest(spec)[:16]
        source_name, target_name = f"{dataset_prefix}_source_{index}", f"{dataset_prefix}_target_{index}"
        for name, dataset_type, linked_service, schema, table_name in (
            (source_name, source_types[source_kind][0], config["source_linked_service"], source_schema, source_table),
            (target_name, "AzureSqlTable", config["target_linked_service"], target_schema, target_table),
        ):
            datasets.append({"name": name, "properties": {
                "type": dataset_type,
                "linkedServiceName": {"referenceName": linked_service, "type": "LinkedServiceReference"},
                "typeProperties": {"schema": schema, "table": table_name},
                "annotations": ["migration-workbench", "review-before-deployment"],
            }})
        name = f"Copy_{index}_{target_table}"
        activities.append({
            "name": name, "type": "Copy",
            "dependsOn": ([{"activity": activities[-1]["name"], "dependencyConditions": ["Succeeded"]}] if activities else []),
            "policy": {"timeout": "0.12:00:00", "retry": 0, "retryIntervalInSeconds": 30,
                       "secureOutput": True, "secureInput": True},
            "inputs": [{"referenceName": source_name, "type": "DatasetReference"}],
            "outputs": [{"referenceName": target_name, "type": "DatasetReference"}],
            "typeProperties": {
                "source": {"type": source_types[source_kind][1]},
                "sink": {"type": "AzureSqlSink", "writeBehavior": "upsert",
                         "upsertSettings": {"useTempDB": True, "keys": table["keys"]}},
                "translator": {"type": "TabularTranslator", "mappings": mappings,
                               "typeConversion": True,
                               "typeConversionSettings": {"allowDataTruncation": False, "treatBooleanAsNumber": False}},
                "enableSkipIncompatibleRow": False,
            },
        })
    pipeline = {"name": config["pipeline_name"], "properties": {
        "activities": activities, "concurrency": 1,
        "parameters": {"migration_operation_id": {"type": "String"}, "migration_plan_hash": {"type": "String"}},
        "annotations": ["migration-workbench", "snapshot-upsert", "requires-reconciliation"],
    }}
    return make_bundle("azure", spec, {"datasets": datasets, "pipeline": pipeline,
                                       "arm_template": arm_template(config["factory_name"], datasets, pipeline)}, {
        key: config[key] for key in ("subscription_id", "resource_group", "factory_name", "pipeline_name")
    }, [
        "Deploy reviewed datasets and pipeline before submission; linked services must already exist.",
        "Use private networking/self-hosted integration runtime where needed; secrets stay in linked services/Key Vault.",
        "Target tables, compatible column types and stable unique keys must already exist.",
        "Quiesce or snapshot the source; Copy upsert does not propagate deletes or establish cross-table consistency.",
        "ADF has no general create_run idempotency token; preserve the submission ledger and reconcile unknown calls.",
    ])


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class AzureExecutor:
    def __init__(self, config: dict[str, Any], client: Any = None):
        self.config = dict(config)
        self.client = client
        self.ledger = SubmissionLedger(config.get("ledger_path", ".migration/cloud-submissions.sqlite3"))

    def _client(self, subscription_id: str | None = None):
        if self.client is None:
            try:
                from azure.identity import DefaultAzureCredential
                from azure.mgmt.datafactory import DataFactoryManagementClient
            except ImportError as exc:
                raise ImportError("Install the azure extra: pip install -e '.[azure]'") from exc
            subscription_id = subscription_id or self.config.get("subscription_id")
            if not subscription_id:
                raise CloudConfigurationError("subscription_id is required")
            self.client = DataFactoryManagementClient(DefaultAzureCredential(), subscription_id)
        return self.client

    @staticmethod
    def _scope(submission: dict) -> str:
        return ":".join(str(submission[key]) for key in ("subscription_id", "resource_group", "factory_name", "pipeline_name"))

    @staticmethod
    def _properties(resource: Any) -> dict:
        if not isinstance(resource, dict):
            if hasattr(resource, "serialize"):
                resource = resource.serialize(keep_readonly=False)
            else:
                raise CloudConfigurationError("Cannot inspect deployed Azure resource")
        return resource.get("properties", resource)

    def _verify_deployment(self, plan: dict) -> None:
        submission = plan["submission"]
        client = self._client(submission["subscription_id"])
        deployed = self._properties(client.pipelines.get(
            submission["resource_group"], submission["factory_name"], submission["pipeline_name"]
        ))
        expected = plan["artifacts"]["pipeline"]["properties"]
        for key in ("activities", "parameters", "concurrency"):
            if deployed.get(key) != expected[key]:
                raise CloudConfigurationError(f"Deployed ADF pipeline {key} differs from the reviewed artifact")
        for dataset in plan["artifacts"]["datasets"]:
            actual = self._properties(client.datasets.get(
                submission["resource_group"], submission["factory_name"], dataset["name"]
            ))
            for key in ("type", "typeProperties", "linkedServiceName"):
                if actual.get(key) != dataset["properties"][key]:
                    raise CloudConfigurationError(f"Deployed dataset {dataset['name']} differs from the reviewed artifact")

    def _query(self, submission: dict, row: dict, token: str) -> list[str]:
        after = datetime.fromisoformat(row["created_at"]) - timedelta(minutes=5)
        before = datetime.now(timezone.utc) + timedelta(minutes=1)
        client = self._client(submission["subscription_id"])
        matches: list[str] = []
        continuation = None
        while True:
            params: Any = {"lastUpdatedAfter": after.isoformat(), "lastUpdatedBefore": before.isoformat(),
                           "filters": [{"operand": "PipelineName", "operator": "Equals", "values": [submission["pipeline_name"]]}]}
            if continuation:
                params["continuationToken"] = continuation
            try:
                from azure.mgmt.datafactory.models import RunFilterParameters, RunQueryFilter
            except ImportError:
                # Dictionary fallback only serves injected test clients. A real
                # SDK client necessarily has these model classes installed.
                pass
            else:
                params = RunFilterParameters(
                    last_updated_after=after, last_updated_before=before,
                    filters=[RunQueryFilter(operand="PipelineName", operator="Equals", values=[submission["pipeline_name"]])],
                    continuation_token=continuation,
                )
            result = client.pipeline_runs.query_by_factory(
                submission["resource_group"], submission["factory_name"], params
            )
            for run in _value(result, "value", []):
                parameters = _value(run, "parameters", {}) or {}
                if _value(run, "pipeline_name", submission["pipeline_name"]) == submission["pipeline_name"] and parameters.get("migration_operation_id") == token:
                    if parameters.get("migration_plan_hash") != row["payload_hash"]:
                        raise CloudConfigurationError("ADF operation matches a different plan hash")
                    matches.append(_value(run, "run_id"))
            continuation = _value(result, "continuation_token")
            if not continuation:
                return list(dict.fromkeys(matches))

    def start(self, plan: dict[str, Any], idempotency_key: str) -> str:
        validate_bundle(plan, "azure")
        submission = plan["submission"]
        token = operation_token(idempotency_key)
        operation = "azure:" + self._scope(submission) + ":" + token
        # The native pipeline parameters bind each execution to the complete
        # bundle, including artifact definitions and approved source/target.
        bundle_hash = digest(plan)
        self._verify_deployment(plan)
        row, created = self.ledger.claim(operation, bundle_hash)
        if row["execution_id"]:
            return row["execution_id"]
        if not created:
            matches = self._query(submission, row, token)
            if len(matches) == 1:
                self.ledger.record(operation, matches[0])
                return matches[0]
            raise SubmissionUnknown("ADF submission unresolved: inspect the native run history; do not delete the ledger or use a new key")
        client = self._client(submission["subscription_id"])
        try:
            response = client.pipelines.create_run(
                submission["resource_group"], submission["factory_name"], submission["pipeline_name"],
                parameters={"migration_operation_id": token, "migration_plan_hash": bundle_hash},
            )
            run_id = _value(response, "run_id")
            if not run_id:
                raise SubmissionUnknown("ADF did not return a run ID")
            self.ledger.record(operation, run_id)
            return run_id
        except Exception as exc:
            self.ledger.unknown(operation)
            raise SubmissionUnknown("ADF create_run outcome is uncertain; repeat with the same key to reconcile, never blindly resubmit") from exc

    def status(self, execution_id: str) -> dict[str, Any]:
        require(self.config, "subscription_id", "resource_group", "factory_name")
        result = self._client().pipeline_runs.get(
            self.config["resource_group"], self.config["factory_name"], execution_id
        )
        status = _value(result, "status")
        return {"execution_id": execution_id, "status": status,
                "terminal": status in {"Succeeded", "Failed", "Cancelled"},
                "message": _value(result, "message")}
