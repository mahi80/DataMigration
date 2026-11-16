"""Contract tests use fake SDK clients; no network or cloud credentials required."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from datamigration.cloud import CloudConfigurationError, SubmissionUnknown, compile_plan
from datamigration.cloud.aws import AwsExecutor
from datamigration.cloud.azure import AzureExecutor
from datamigration.cloud.base import digest, operation_token
from datamigration.cloud.databricks import DatabricksExecutor


def base_spec():
    return {
        "name": "orders", "mode": "snapshot",
        "source": {"kind": "sqlserver", "url_env": "SOURCE_URL"},
        "target": {"kind": "azure_sql", "url_env": "TARGET_URL"},
        "tables": [{"source": "dbo.orders", "target": "dbo.orders", "keys": ["id"],
                    "columns": [{"source": "order_id", "target": "id", "data_type": "integer"}]}],
    }


def aws_spec():
    spec = base_spec()
    spec["cloud"] = {"provider": "aws", "state_machine_arn": "arn:aws:states:eu-west-1:123456789012:stateMachine:migrate",
                     "glue_job_name": "approved-job", "manifest_uri": "s3://approved/manifests/orders-v1.json"}
    return spec


def azure_spec():
    spec = base_spec()
    spec["cloud"] = {"provider": "azure", "subscription_id": "subscription", "resource_group": "rg",
                     "factory_name": "factory", "pipeline_name": "orders", "source_linked_service": "SourceSql",
                     "target_linked_service": "TargetSql"}
    return spec


class AwsFake:
    def __init__(self, definition):
        self.definition = definition
        self.starts = []
        self.executions = {}
        self.lose_ack = False

    def describe_state_machine(self, **kwargs):
        return {"type": "STANDARD", "definition": json.dumps(self.definition)}

    def start_execution(self, **kwargs):
        self.starts.append(kwargs)
        arn = AwsExecutor._execution_arn(kwargs["stateMachineArn"], kwargs["name"])
        self.executions[arn] = {"executionArn": arn, "input": kwargs["input"], "status": "SUCCEEDED", "output": "{}"}
        if self.lose_ack:
            raise TimeoutError("lost response")
        return {"executionArn": arn}

    def describe_execution(self, executionArn):
        return self.executions[executionArn]


def test_aws_submission_reconciles_a_lost_acknowledgement(tmp_path):
    plan = compile_plan("aws", aws_spec())
    client = AwsFake(plan["artifacts"]["state_machine"])
    client.lose_ack = True
    adapter = AwsExecutor({"ledger_path": str(tmp_path / "aws.sqlite3")}, client=client)
    arn = adapter.start(plan, "immutable-operation")
    assert adapter.start(plan, "immutable-operation") == arn
    assert len(client.starts) == 1
    assert adapter.status(arn)["status"] == "SUCCEEDED"
    assert json.loads(client.starts[0]["input"])["plan_hash"] == plan["plan_hash"]


def test_aws_rejects_unreviewed_deployed_definition(tmp_path):
    plan = compile_plan("aws", aws_spec())
    client = AwsFake({"unreviewed": "definition"})
    with pytest.raises(CloudConfigurationError, match="differs"):
        AwsExecutor({"ledger_path": str(tmp_path / "aws.sqlite3")}, client=client).start(plan, "op")
    assert not client.starts


def test_aws_unknown_outcome_never_starts_a_second_execution(tmp_path):
    plan = compile_plan("aws", aws_spec())
    client = AwsFake(plan["artifacts"]["state_machine"])
    def lost(**kwargs):
        client.starts.append(kwargs)
        raise TimeoutError("unknown")
    client.start_execution = lost
    adapter = AwsExecutor({"ledger_path": str(tmp_path / "aws.sqlite3")}, client=client)
    with pytest.raises(SubmissionUnknown):
        adapter.start(plan, "op")
    with pytest.raises(SubmissionUnknown):
        adapter.start(plan, "op")
    assert len(client.starts) == 1


def test_dms_requires_review_and_does_not_call_cdc_running_cutover_ready():
    spec = aws_spec()
    spec["mode"] = "full_load_cdc"
    spec["cloud"].update({"strategy": "dms_full_load_cdc", "dms_task_arn": "arn:aws:dms:eu-west-1:123456789012:task:reviewed",
                          "source_engine": "postgres", "target_engine": "postgres", "dms_preflight_report_uri": "s3://approved/preflight.json"})
    with pytest.raises(CloudConfigurationError, match="reviewed"):
        compile_plan("aws", spec)
    spec["cloud"]["source_target_reviewed"] = True
    plan = compile_plan("aws", spec)
    assert plan["artifacts"]["state_machine"]["States"]["CdcRunning"]["Result"] == {"phase": "CDC_RUNNING", "cutover_ready": False}


def test_azure_refuses_unsupported_transform_and_connector():
    spec = azure_spec()
    spec["tables"][0]["columns"][0]["transform"] = "upper"
    with pytest.raises(CloudConfigurationError, match="identity"):
        compile_plan("azure", spec)
    spec = azure_spec()
    spec["source"]["kind"] = "mongodb"
    with pytest.raises(CloudConfigurationError, match="currently supports"):
        compile_plan("azure", spec)


@pytest.mark.parametrize("provider", ["azure", "databricks"])
def test_native_compilers_do_not_ignore_quality_rules(provider):
    spec = azure_spec()
    if provider == "databricks":
        spec["cloud"] = {"job_id": 1, "manifest_uri": "/Volumes/main/migration/config/plan.json"}
    spec["tables"][0]["quality_rules"] = [{"name": "clean_id", "column": "id", "check": "not_null"}]
    with pytest.raises(CloudConfigurationError, match="quality_rules"):
        compile_plan(provider, spec)


def test_azure_rejects_type_contract_it_cannot_enforce():
    spec = azure_spec()
    spec["tables"][0]["columns"][0].update(data_type="decimal", precision=18, scale=2)
    with pytest.raises(CloudConfigurationError, match="precision/scale/timezone"):
        compile_plan("azure", spec)


def test_azure_orders_dimension_before_fact_and_uses_upsert():
    spec = azure_spec()
    fact = spec["tables"][0]
    fact["depends_on"] = ["dbo.customers"]
    dimension = copy.deepcopy(fact)
    dimension.update(source="dbo.customers", target="dbo.customers", depends_on=[])
    spec["tables"].append(dimension)
    plan = compile_plan("azure", spec)
    activities = plan["artifacts"]["pipeline"]["properties"]["activities"]
    assert activities[0]["name"].endswith("customers")
    assert activities[1]["dependsOn"][0]["activity"] == activities[0]["name"]
    assert activities[0]["typeProperties"]["sink"]["writeBehavior"] == "upsert"
    assert not activities[0]["typeProperties"]["enableSkipIncompatibleRow"]


class AzureFake:
    def __init__(self, plan):
        self.plan = plan
        self.starts = []
        self.runs = []
        self.lose_ack = False
        self.pipelines = SimpleNamespace(get=self.get_pipeline, create_run=self.create_run)
        self.datasets = SimpleNamespace(get=self.get_dataset)
        self.pipeline_runs = SimpleNamespace(query_by_factory=self.query, get=self.get_run)

    def get_pipeline(self, *args):
        return self.plan["artifacts"]["pipeline"]

    def get_dataset(self, *args):
        return next(dataset for dataset in self.plan["artifacts"]["datasets"] if dataset["name"] == args[-1])

    def create_run(self, *args, parameters):
        self.starts.append(parameters)
        self.runs.append({"run_id": "run-1", "pipeline_name": args[-1], "parameters": parameters})
        if self.lose_ack:
            raise TimeoutError("lost response")
        return SimpleNamespace(run_id="run-1")

    def query(self, *args):
        return SimpleNamespace(value=self.runs, continuation_token=None)

    def get_run(self, *args):
        return SimpleNamespace(status="Succeeded", message=None)


def test_azure_lost_response_is_reconciled_without_resubmission(tmp_path):
    plan = compile_plan("azure", azure_spec())
    client = AzureFake(plan)
    client.lose_ack = True
    config = dict(plan["submission"], ledger_path=str(tmp_path / "ledger.sqlite3"))
    first_process = AzureExecutor(config, client=client)
    with pytest.raises(SubmissionUnknown):
        first_process.start(plan, "op-1")
    restarted_process = AzureExecutor(config, client=client)
    assert restarted_process.start(plan, "op-1") == "run-1"
    assert len(client.starts) == 1
    assert restarted_process.status("run-1")["terminal"]


def test_azure_unknown_without_visible_run_is_not_permission_to_retry(tmp_path):
    plan = compile_plan("azure", azure_spec())
    client = AzureFake(plan)
    client.lose_ack = True
    adapter = AzureExecutor({"ledger_path": str(tmp_path / "ledger.sqlite3")}, client=client)
    with pytest.raises(SubmissionUnknown):
        adapter.start(plan, "op")
    client.runs.clear()
    with pytest.raises(SubmissionUnknown):
        adapter.start(plan, "op")
    assert len(client.starts) == 1


def test_azure_same_operation_cannot_refer_to_another_plan(tmp_path):
    plan = compile_plan("azure", azure_spec())
    client = AzureFake(plan)
    adapter = AzureExecutor({"ledger_path": str(tmp_path / "ledger.sqlite3")}, client=client)
    assert adapter.start(plan, "op") == "run-1"
    changed = copy.deepcopy(plan)
    changed["plan_hash"] = "different"
    with pytest.raises(CloudConfigurationError, match="different payload"):
        adapter.start(changed, "op")
    assert len(client.starts) == 1


def test_azure_refuses_changed_deployment(tmp_path):
    plan = compile_plan("azure", azure_spec())
    changed = copy.deepcopy(plan)
    changed["artifacts"]["pipeline"]["properties"]["activities"][0]["typeProperties"]["sink"]["writeBehavior"] = "insert"
    client = AzureFake(changed)
    with pytest.raises(CloudConfigurationError, match="differs"):
        AzureExecutor({"ledger_path": str(tmp_path / "ledger.sqlite3")}, client=client).start(plan, "op")
    assert not client.starts


def test_databricks_retries_keep_the_provider_idempotency_token(tmp_path):
    spec = base_spec()
    spec["cloud"] = {"provider": "databricks", "job_id": 42, "manifest_uri": "/Volumes/catalog/schema/plans/plan.json"}
    plan = compile_plan("databricks", spec)
    calls = []
    def run_now(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise TimeoutError("receipt lost")
        return SimpleNamespace(response=SimpleNamespace(run_id=123))
    client = SimpleNamespace(jobs=SimpleNamespace(run_now=run_now))
    executor = DatabricksExecutor({"ledger_path": str(tmp_path / "db.sqlite3")}, client=client)
    with pytest.raises(TimeoutError):
        executor.start(plan, "same-operation")
    assert executor.start(plan, "same-operation") == "123"
    assert executor.start(plan, "same-operation") == "123"
    assert calls[0] == calls[1]
    assert len(calls) == 2
    assert calls[0]["idempotency_token"] == operation_token("same-operation")
    assert len(calls[0]["idempotency_token"]) == 64
    changed = copy.deepcopy(plan)
    changed["plan_hash"] = "changed"
    with pytest.raises(CloudConfigurationError, match="different payload"):
        executor.start(changed, "same-operation")


def test_compilers_do_not_accept_inline_credentials():
    spec = aws_spec()
    spec["cloud"]["password"] = "must-not-be-committed"
    with pytest.raises(CloudConfigurationError, match="credentials"):
        compile_plan("aws", spec)


def test_compiled_bundle_hash_is_deterministic():
    assert compile_plan("aws", aws_spec()) == compile_plan("aws", aws_spec())
    assert len(digest(compile_plan("aws", aws_spec()))) == 64


def test_azure_artifacts_round_trip_through_installed_sdk_models():
    models = pytest.importorskip("azure.mgmt.datafactory.models")
    plan = compile_plan("azure", azure_spec())
    resource = models.PipelineResource.deserialize(plan["artifacts"]["pipeline"])
    actual = AzureExecutor._properties(resource)
    expected = plan["artifacts"]["pipeline"]["properties"]
    for key in ("activities", "parameters", "concurrency"):
        assert actual[key] == expected[key]
    for dataset in plan["artifacts"]["datasets"]:
        actual = AzureExecutor._properties(models.DatasetResource.deserialize(dataset))
        for key in ("type", "typeProperties", "linkedServiceName"):
            assert actual[key] == dataset["properties"][key]
