from __future__ import annotations

import copy

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from datamigration.cloud import CloudConfigurationError
from datamigration.cloud.workflow import build_cloud_graph


def spec():
    return {"name": "test", "mode": "snapshot", "cloud": {
        "state_machine_arn": "arn:aws:states:eu-west-1:123456789012:stateMachine:migrate",
        "glue_job_name": "approved", "manifest_uri": "s3://approved/test.json",
    }}


class FakeExecutor:
    def __init__(self, results=None):
        self.starts = []
        self.results = results or [{"status": "SUCCEEDED", "terminal": True}]

    def start(self, bundle, idempotency_key):
        self.starts.append((bundle, idempotency_key))
        return "execution-1"

    def status(self, execution_id):
        assert execution_id == "execution-1"
        return self.results.pop(0)


def approve(state):
    return Command(resume={"approve": True, "reviewer": "reviewer@example.test", "bundle_hash": state["bundle_hash"]})


def test_cloud_graph_approval_pending_and_success_do_not_claim_validation():
    executor = FakeExecutor([{"status": "RUNNING", "terminal": False}, {"status": "SUCCEEDED", "terminal": True}])
    graph = build_cloud_graph(InMemorySaver(), "aws", {}, executor=executor)
    config = {"configurable": {"thread_id": "cloud-1"}}
    state = graph.invoke({"spec": spec(), "operation_id": "operation-1"}, config)
    assert state["status"] == "AWAITING_APPROVAL"
    assert not executor.starts
    state = graph.invoke(approve(state), config)
    assert state["status"] == "WAITING_FOR_NATIVE_STATUS"
    state = graph.invoke(Command(resume={"action": "poll"}), config)
    assert state["status"] == "WAITING_FOR_NATIVE_STATUS"
    state = graph.invoke(Command(resume={"action": "poll"}), config)
    assert state["status"] == "EXECUTION_SUCCEEDED_REQUIRES_RECONCILIATION"
    assert len(executor.starts) == 1
    assert executor.starts[0][1] == "operation-1"


def test_rejected_approval_has_no_external_effect():
    executor = FakeExecutor()
    graph = build_cloud_graph(InMemorySaver(), "aws", {}, executor=executor)
    config = {"configurable": {"thread_id": "reject"}}
    graph.invoke({"spec": spec(), "operation_id": "op"}, config)
    state = graph.invoke(Command(resume={"approve": False}), config)
    assert state["status"] == "REJECTED"
    assert not executor.starts


def test_changed_hash_or_executor_config_is_rejected():
    executor = FakeExecutor()
    checkpoint = InMemorySaver()
    graph = build_cloud_graph(checkpoint, "aws", {"region": "eu-west-1"}, executor=executor)
    config = {"configurable": {"thread_id": "binding"}}
    state = graph.invoke({"spec": spec(), "operation_id": "op"}, config)
    other = build_cloud_graph(checkpoint, "aws", {"region": "us-east-1"}, executor=executor)
    with pytest.raises(CloudConfigurationError, match="configuration changed"):
        other.invoke(approve(state), config)
    assert not executor.starts


def test_mutating_bundle_after_review_request_is_rejected():
    executor = FakeExecutor()
    graph = build_cloud_graph(InMemorySaver(), "aws", {}, executor=executor)
    config = {"configurable": {"thread_id": "tamper"}}
    state = graph.invoke({"spec": spec(), "operation_id": "op"}, config)
    changed = copy.deepcopy(state["bundle"])
    changed["submission"]["input"]["manifest_uri"] = "s3://unreviewed/other.json"
    graph.update_state(config, {"bundle": changed})
    with pytest.raises(CloudConfigurationError, match="bundle changed"):
        graph.invoke(approve(state), config)
    assert not executor.starts


def test_failed_native_execution_ends_failed():
    executor = FakeExecutor([{"status": "FAILED", "terminal": True, "error": "worker_failure"}])
    graph = build_cloud_graph(InMemorySaver(), "aws", {}, executor=executor)
    config = {"configurable": {"thread_id": "failure"}}
    state = graph.invoke({"spec": spec(), "operation_id": "op"}, config)
    graph.invoke(approve(state), config)
    state = graph.invoke(Command(resume={"action": "poll"}), config)
    assert state["status"] == "FAILED"
