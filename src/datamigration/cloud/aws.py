"""AWS Step Functions Standard adapter; Glue execution and reviewed DMS plans."""

from __future__ import annotations

import json
import re
from typing import Any

from .base import (
    CloudConfigurationError, SubmissionLedger, SubmissionUnknown, canonical_json, config_for,
    digest, make_bundle, operation_token, require, validate_bundle,
)


def _glue_definition(config: dict[str, Any]) -> dict:
    require(config, "glue_job_name", "manifest_uri")
    if not str(config["manifest_uri"]).startswith("s3://"):
        raise CloudConfigurationError("AWS manifest_uri must reference an approved S3 object")
    return {
        "Comment": "Execute an approved manifest with an existing Glue job; reconcile separately.",
        "StartAt": "RunApprovedGlueJob",
        "TimeoutSeconds": int(config.get("timeout_seconds", 86400)),
        "States": {
            "RunApprovedGlueJob": {
                "Type": "Task", "Resource": "arn:aws:states:::glue:startJobRun.sync",
                "Parameters": {
                    "JobName": config["glue_job_name"],
                    "Arguments": {
                        "--migration-manifest.$": "$.manifest_uri",
                        "--migration-plan-hash.$": "$.plan_hash",
                        "--migration-operation-id.$": "$.operation_id",
                    },
                },
                "ResultPath": "$.job", "End": True,
            }
        },
    }


def _dms_definition(config: dict[str, Any]) -> dict:
    require(config, "dms_task_arn", "source_engine", "target_engine", "dms_preflight_report_uri")
    if config.get("source_target_reviewed") is not True:
        raise CloudConfigurationError("DMS requires source_target_reviewed=true and a preflight report")
    # This is deliberately not a universal source/target support assertion. The
    # report must check engine versions, endpoint limitations, permissions, logs,
    # LOBs, table mappings, baseline/CDC coordination and the cutover procedure.
    task_arn = config["dms_task_arn"]
    return {
        "Comment": "Start reviewed full-load-and-CDC task. Success means baseline loaded and CDC running; NOT cutover.",
        "StartAt": "InspectTask", "TimeoutSeconds": int(config.get("timeout_seconds", 86400)),
        "States": {
            "InspectTask": {
                "Type": "Task", "Resource": "arn:aws:states:::aws-sdk:databasemigration:describeReplicationTasks",
                "Parameters": {"Filters": [{"Name": "replication-task-arn", "Values": [task_arn]}]},
                "ResultPath": "$.dms", "Next": "TaskState",
            },
            "TaskState": {
                "Type": "Choice", "Choices": [
                    {"Variable": "$.dms.ReplicationTasks[0].Status", "StringEquals": "ready", "Next": "StartTask"},
                    {"Variable": "$.dms.ReplicationTasks[0].Status", "StringEquals": "running", "Next": "BaselineReady"},
                    {"Variable": "$.dms.ReplicationTasks[0].Status", "StringEquals": "starting", "Next": "WaitForTask"},
                ], "Default": "OperatorReview",
            },
            "StartTask": {
                "Type": "Task", "Resource": "arn:aws:states:::aws-sdk:databasemigration:startReplicationTask",
                "Parameters": {"ReplicationTaskArn": task_arn, "StartReplicationTaskType": "start-replication"},
                "ResultPath": "$.start", "Next": "WaitForTask",
            },
            "WaitForTask": {"Type": "Wait", "Seconds": 30, "Next": "InspectTask"},
            "BaselineReady": {
                "Type": "Choice", "Choices": [{
                    "And": [
                        {"Variable": "$.dms.ReplicationTasks[0].ReplicationTaskStats.FullLoadProgressPercent", "NumericEquals": 100},
                        {"Variable": "$.dms.ReplicationTasks[0].ReplicationTaskStats.TablesErrored", "NumericEquals": 0},
                        {"Variable": "$.dms.ReplicationTasks[0].ReplicationTaskStats.TablesLoading", "NumericEquals": 0},
                        {"Variable": "$.dms.ReplicationTasks[0].ReplicationTaskStats.TablesQueued", "NumericEquals": 0},
                    ], "Next": "CdcRunning",
                }], "Default": "WaitForTask",
            },
            "CdcRunning": {"Type": "Pass", "Result": {"phase": "CDC_RUNNING", "cutover_ready": False}, "End": True},
            "OperatorReview": {"Type": "Fail", "Error": "DmsOperatorReview", "Cause": "DMS task is absent, failed, or stopped; inspect before resume/restart."},
        },
    }


def compile_plan(spec: dict[str, Any]) -> dict[str, Any]:
    config = config_for(spec, "aws")
    require(config, "state_machine_arn")
    if ":stateMachine:" not in config["state_machine_arn"]:
        raise CloudConfigurationError("state_machine_arn must be a Step Functions state machine ARN")
    strategy = config.get("strategy", "glue")
    if strategy == "glue":
        if spec.get("mode", "snapshot") not in ("snapshot", "batch"):
            raise CloudConfigurationError("The Glue manifest contract supports snapshot/batch only; CDC requires a reviewed engine-specific plan")
        definition = _glue_definition(config)
    elif strategy == "dms_full_load_cdc":
        if spec.get("mode") != "full_load_cdc":
            raise CloudConfigurationError("DMS full-load-and-CDC strategy requires mode=full_load_cdc")
        if any(table.get("quality_rules") for table in spec.get("tables", [])):
            raise CloudConfigurationError("This DMS template does not implement quality_rules or corrections")
        definition = _dms_definition(config)
    else:
        raise CloudConfigurationError(f"Unsupported AWS strategy: {strategy}")
    # Support sovereign partitions without hard-coding commercial AWS ARNs.
    partition = config["state_machine_arn"].split(":")[1]
    for state in definition["States"].values():
        if "Resource" in state:
            state["Resource"] = state["Resource"].replace("arn:aws:", f"arn:{partition}:", 1)
    submission = {
        "state_machine_arn": config["state_machine_arn"], "strategy": strategy,
        "input": {"migration_name": spec.get("name", "migration"), "manifest_uri": config.get("manifest_uri", "")},
    }
    if strategy == "dms_full_load_cdc":
        submission["dms"] = {k: config[k] for k in ("dms_task_arn", "source_engine", "target_engine", "dms_preflight_report_uri")}
    bundle = make_bundle("aws", spec, {"state_machine": definition}, submission, [
        "Deploy this exact reviewed definition as a STANDARD state machine before submission.",
        "Provision scoped worker credentials, private connectivity and native jobs/endpoints separately.",
        "Use an immutable manifest object/version; the worker must verify its approved plan hash.",
        "Approved Glue workers must implement every declared quality rule/correction and type constraint, or reject the manifest before writes.",
        "Native execution success does not certify data quality or authorize cutover.",
    ])
    bundle["submission"]["input"]["plan_hash"] = bundle["plan_hash"]
    return bundle


class AwsExecutor:
    def __init__(self, config: dict[str, Any], client: Any = None, dms_client: Any = None):
        self.config = dict(config)
        self.client = client
        self.dms_client = dms_client
        self.ledger = SubmissionLedger(config.get("ledger_path", ".migration/cloud-submissions.sqlite3"))

    def _client(self):
        if self.client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError("Install the aws extra: pip install -e '.[aws]'") from exc
            self.client = boto3.client("stepfunctions", region_name=self.config.get("region"))
        return self.client

    def _verify_dms(self, plan: dict) -> None:
        expected = plan["submission"].get("dms")
        if not expected:
            return
        if self.dms_client is None:
            import boto3
            self.dms_client = boto3.client("dms", region_name=self.config.get("region"))
        tasks = self.dms_client.describe_replication_tasks(Filters=[{
            "Name": "replication-task-arn", "Values": [expected["dms_task_arn"]]
        }]).get("ReplicationTasks", [])
        if len(tasks) != 1 or tasks[0].get("MigrationType") != "full-load-and-cdc":
            raise CloudConfigurationError("Expected one existing DMS full-load-and-cdc task")
        task = tasks[0]
        for side in ("source", "target"):
            arn = task[f"{side.title()}EndpointArn"]
            endpoints = self.dms_client.describe_endpoints(Filters=[{
                "Name": "endpoint-arn", "Values": [arn]
            }]).get("Endpoints", [])
            if len(endpoints) != 1 or endpoints[0].get("EngineName") != expected[f"{side}_engine"]:
                raise CloudConfigurationError(f"DMS {side} engine differs from reviewed plan")

    @staticmethod
    def _execution_arn(state_machine_arn: str, name: str) -> str:
        match = re.fullmatch(r"(arn:[^:]+:states:[^:]+:[^:]+):stateMachine:([^:]+)(?::[^:]+)?", state_machine_arn)
        if not match:
            raise CloudConfigurationError("Malformed state machine ARN")
        return f"{match.group(1)}:execution:{match.group(2)}:{name}"

    def start(self, plan: dict[str, Any], idempotency_key: str) -> str:
        validate_bundle(plan, "aws")
        client = self._client()
        arn = plan["submission"]["state_machine_arn"]
        deployed = client.describe_state_machine(stateMachineArn=arn)
        if deployed.get("type") != "STANDARD":
            raise CloudConfigurationError("Only STANDARD Step Functions workflows are supported")
        if json.loads(deployed["definition"]) != plan["artifacts"]["state_machine"]:
            raise CloudConfigurationError("Deployed state machine differs from the reviewed artifact")
        self._verify_dms(plan)
        name = "migration-" + operation_token(idempotency_key)[:48]
        payload = dict(plan["submission"]["input"], operation_id=operation_token(idempotency_key))
        serialized = canonical_json(payload)
        if len(serialized.encode()) > 262144:
            raise CloudConfigurationError("Step Functions input exceeds its size limit; use artifact references")
        expected_arn = self._execution_arn(arn, name)
        operation = "aws:" + arn + ":" + operation_token(idempotency_key)
        record, created = self.ledger.claim(operation, digest(plan))
        if record["execution_id"]:
            return record["execution_id"]
        if not created:
            try:
                found = client.describe_execution(executionArn=expected_arn)
            except Exception as exc:
                raise SubmissionUnknown(f"AWS submission is unresolved; inspect {expected_arn} before retrying") from exc
            if canonical_json(json.loads(found.get("input", "{}"))) != serialized:
                raise CloudConfigurationError("Existing AWS execution input differs from the approved plan")
            self.ledger.record(operation, found["executionArn"])
            return found["executionArn"]
        try:
            result = client.start_execution(stateMachineArn=arn, name=name, input=serialized)
            self.ledger.record(operation, result["executionArn"])
            return result["executionArn"]
        except Exception as exc:
            self.ledger.unknown(operation)
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code and code != "ExecutionAlreadyExists":
                raise
            # A lost acknowledgement or closed duplicate is reconciled, never
            # converted into a fresh name and a second migration.
            try:
                found = client.describe_execution(executionArn=expected_arn)
            except Exception as lookup_exc:
                raise SubmissionUnknown(f"Inspect AWS execution {expected_arn} before retrying") from lookup_exc
            if canonical_json(json.loads(found.get("input", "{}"))) != serialized:
                raise CloudConfigurationError("Execution name belongs to a different approved input") from exc
            self.ledger.record(operation, found["executionArn"])
            return found["executionArn"]

    def status(self, execution_id: str) -> dict[str, Any]:
        result = self._client().describe_execution(executionArn=execution_id)
        return {
            "execution_id": execution_id, "status": result["status"],
            "terminal": result["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"},
            "output": json.loads(result["output"]) if result.get("output") else None,
            "error": result.get("error"), "cause": result.get("cause"),
        }
