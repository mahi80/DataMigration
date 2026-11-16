"""Databricks Jobs adapter for existing approved Delta/lakehouse jobs."""

from __future__ import annotations

from typing import Any

from .base import (
    CloudConfigurationError, SubmissionLedger, config_for, digest, make_bundle,
    operation_token, require, validate_bundle,
)


def compile_plan(spec: dict[str, Any]) -> dict[str, Any]:
    config = config_for(spec, "databricks")
    require(config, "job_id", "manifest_uri")
    if isinstance(config["job_id"], bool) or int(config["job_id"]) <= 0:
        raise CloudConfigurationError("Databricks job_id must be a positive existing job ID")
    if spec.get("mode", "snapshot") not in {"snapshot", "batch"}:
        raise CloudConfigurationError("This job contract supports snapshot/batch; streaming CDC needs a separately reviewed job")
    if any(table.get("quality_rules") for table in spec.get("tables", [])):
        raise CloudConfigurationError("This Databricks job contract does not implement quality_rules or corrections; use a separately reviewed worker")
    parameters = config.get("job_parameters", {})
    if not isinstance(parameters, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parameters.items()):
        raise CloudConfigurationError("job_parameters must be a string-to-string object")
    reserved = {"migration_manifest_uri", "migration_plan_hash", "migration_operation_id"}
    if reserved.intersection(parameters):
        raise CloudConfigurationError("Reserved migration parameters cannot be overridden")
    bundle = make_bundle("databricks", spec, {
        "job_contract": {
            "job_id": int(config["job_id"]),
            "required_job_parameters": sorted(reserved),
            "target_catalog": config.get("catalog"), "target_schema": config.get("schema"),
        },
    }, {
        "job_id": int(config["job_id"]),
        "job_parameters": {**parameters, "migration_manifest_uri": config["manifest_uri"]},
    }, [
        "Provision the reviewed job and compute policy; this compiler does not create clusters/jobs.",
        "Configure workload identity/OAuth and Unity Catalog grants outside the manifest.",
        "The job must verify the immutable manifest hash and perform keyed idempotent Delta MERGE.",
        "Match Delta protocol/features to the runtime; schema evolution and deletes require explicit policies.",
        "A successful job run is followed by reconciliation; Mosaic AI is an optional reasoning endpoint, not the data mover.",
    ])
    bundle["submission"]["job_parameters"]["migration_plan_hash"] = bundle["plan_hash"]
    return bundle


def _enum(value: Any) -> Any:
    return getattr(value, "value", value)


def _value(value: Any, field: str, default=None):
    return value.get(field, default) if isinstance(value, dict) else getattr(value, field, default)


class DatabricksExecutor:
    def __init__(self, config: dict[str, Any], client: Any = None):
        self.config = dict(config)
        self.client = client
        self.ledger = SubmissionLedger(config.get("ledger_path", ".migration/cloud-submissions.sqlite3"))

    def _client(self):
        if self.client is None:
            try:
                from databricks.sdk import WorkspaceClient
            except ImportError as exc:
                raise ImportError("Install the databricks extra: pip install -e '.[databricks]'") from exc
            kwargs = {key: self.config[key] for key in ("host", "profile") if self.config.get(key)}
            # SDK unified authentication obtains credentials from the supported
            # environment/workload identity. Tokens are never stored in plans.
            self.client = WorkspaceClient(**kwargs)
        return self.client

    def start(self, plan: dict[str, Any], idempotency_key: str) -> str:
        validate_bundle(plan, "databricks")
        token = operation_token(idempotency_key)
        operation = "databricks:" + str(self.config.get("host", self.config.get("profile", "default"))) + ":" + str(plan["submission"]["job_id"]) + ":" + token
        record, _ = self.ledger.claim(operation, digest(plan))
        if record["execution_id"]:
            return record["execution_id"]
        parameters = dict(plan["submission"]["job_parameters"], migration_operation_id=token)
        response = self._client().jobs.run_now(
            job_id=plan["submission"]["job_id"], job_parameters=parameters,
            idempotency_token=token,
        )
        # SDK run_now returns a Wait object with a nonblocking .response receipt.
        receipt = getattr(response, "response", response)
        run_id = _value(receipt, "run_id")
        if run_id is None:
            raise RuntimeError("Databricks returned no run ID; retry only with the same idempotency key")
        self.ledger.record(operation, str(run_id))
        return str(run_id)

    def status(self, execution_id: str) -> dict[str, Any]:
        result = self._client().jobs.get_run(run_id=int(execution_id))
        state = _value(result, "state", {}) or {}
        lifecycle = _enum(_value(state, "life_cycle_state"))
        result_state = _enum(_value(state, "result_state"))
        return {
            "execution_id": execution_id, "status": lifecycle,
            "result": result_state, "terminal": lifecycle in {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"},
            "message": _value(state, "state_message"), "run_page_url": _value(result, "run_page_url"),
        }
