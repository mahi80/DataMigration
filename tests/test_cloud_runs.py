"""Real persistent LangGraph lifecycle with injected native execution effects."""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from filelock import Timeout

import datamigration.cloud_runs as hosting
from datamigration.cloud import CloudConfigurationError
from datamigration.cloud.base import digest
from datamigration.maintenance import set_maintenance, status as maintenance_status
from datamigration.spec import write_json


def spec():
    return {"name": "orders", "mode": "snapshot",
            "source": {"kind": "postgres", "url_env": "SOURCE_URL"},
            "target": {"kind": "redshift", "url_env": "TARGET_URL"},
            "tables": [{"source": "orders", "target": "orders", "keys": ["id"],
                        "columns": [{"source": "id", "target": "id", "data_type": "integer"}]}],
            "cloud": {"state_machine_arn": "arn:aws:states:eu-west-1:123456789012:stateMachine:migrate",
                      "glue_job_name": "approved", "manifest_uri": "s3://approved/orders.json"}}


class NativeExecutor:
    def __init__(self, workspace):
        self.workspace = workspace
        self.starts = []
        self.effects = {}
        self.results = [{"status": "SUCCEEDED", "terminal": True}]
        self.fail_submit_once = False
        self.on_start = lambda: None

    def start(self, bundle, operation_id):
        # This assertion verifies the shared drain barrier is visible before an
        # external effect, using the real metadata written by the host manager.
        assert maintenance_status(self.workspace)["active_runs"] == 1
        self.starts.append((operation_id, digest(bundle)))
        receipt = self.effects.setdefault(operation_id, "native-receipt-1")
        self.on_start()
        if self.fail_submit_once:
            self.fail_submit_once = False
            raise TimeoutError("Native submission acknowledgement was lost")
        return receipt

    def status(self, execution_id):
        assert execution_id == "native-receipt-1"
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def host(tmp_path, monkeypatch):
    executor = NativeExecutor(tmp_path)
    builder = hosting.build_cloud_graph
    def injected(checkpointer, provider, executor_config):
        return builder(checkpointer, provider, executor_config, executor=executor)
    monkeypatch.setattr(hosting, "build_cloud_graph", injected)
    return hosting.CloudRunManager(tmp_path), executor


def test_review_and_native_receipt_survive_manager_recreation(host):
    manager, executor = host
    pending = manager.start("aws", spec(), "persistent")
    assert pending["status"] == "AWAITING_APPROVAL"
    assert pending["next_nodes"] == ["approval"]
    assert executor.starts == []
    artifact = json.loads((manager.directory("persistent") / "bundle.json").read_text())
    assert digest(artifact) == pending["bundle_hash"]
    reopened = hosting.CloudRunManager(manager.workspace)
    with pytest.raises(ValueError, match="pending bundle"):
        reopened.approve("persistent", "0" * 64, "reviewer@example.test")
    assert reopened.status("persistent")["status"] == "AWAITING_APPROVAL"
    native = reopened.approve("persistent", pending["bundle_hash"], "reviewer@example.test")
    assert native["status"] == "WAITING_FOR_NATIVE_STATUS"
    assert native["execution_id"] == "native-receipt-1"
    executor.results.insert(0, {"status": "RUNNING", "terminal": False})
    reopened_again = hosting.CloudRunManager(manager.workspace)
    assert reopened_again.poll("persistent")["status"] == "WAITING_FOR_NATIVE_STATUS"
    assert reopened_again.poll("persistent")["status"] == "EXECUTION_SUCCEEDED_REQUIRES_RECONCILIATION"
    assert len(executor.starts) == 1
    with pytest.raises(FileExistsError):
        manager.start("aws", spec(), "persistent")


def test_rejection_is_terminal_and_has_no_native_effect(host):
    manager, executor = host
    pending = manager.start("aws", spec(), "reject")
    with pytest.raises(ValueError, match="Reviewer"):
        manager.approve("reject", pending["bundle_hash"], " ")
    assert manager.approve("reject", pending["bundle_hash"], "reviewer", approved=False)["status"] == "REJECTED"
    assert executor.starts == []
    with pytest.raises(ValueError, match="Only interrupted"):
        manager.retry("reject")
    with pytest.raises(ValueError, match="not awaiting"):
        manager.poll("reject")


def test_unknown_submission_replays_approved_node_with_same_operation(host):
    manager, executor = host
    pending = manager.start("aws", spec(), "lost-ack")
    executor.fail_submit_once = True
    with pytest.raises(TimeoutError):
        manager.approve("lost-ack", pending["bundle_hash"], "reviewer")
    assert manager.status("lost-ack")["status"] == "ERROR"
    restarted = hosting.CloudRunManager(manager.workspace)
    resumed = restarted.retry("lost-ack")
    assert resumed["status"] == "WAITING_FOR_NATIVE_STATUS"
    assert "error" not in resumed
    assert executor.starts == [("lost-ack", pending["bundle_hash"])] * 2
    assert len(executor.effects) == 1
    with restarted.graph("lost-ack") as graph:
        state = graph.get_state({"configurable": {"thread_id": "lost-ack"}}).values
    assert state["approval"] == {"bundle_hash": pending["bundle_hash"], "reviewer": "reviewer"}


def test_poll_error_resumes_status_node_without_new_submission(host):
    manager, executor = host
    pending = manager.start("aws", spec(), "poll-error")
    manager.approve("poll-error", pending["bundle_hash"], "reviewer")
    executor.results.insert(0, TimeoutError("Native status unavailable"))
    with pytest.raises(TimeoutError):
        manager.poll("poll-error")
    assert manager.status("poll-error")["status"] == "ERROR"
    resumed = hosting.CloudRunManager(manager.workspace).retry("poll-error")
    assert resumed["status"] == "EXECUTION_SUCCEEDED_REQUIRES_RECONCILIATION"
    assert len(executor.starts) == 1


def test_native_failed_run_requires_separate_reviewed_recovery(host):
    manager, executor = host
    pending = manager.start("aws", spec(), "native-failed")
    manager.approve("native-failed", pending["bundle_hash"], "reviewer")
    executor.results = [{"status": "FAILED", "terminal": True}]
    assert manager.poll("native-failed")["status"] == "FAILED"
    with pytest.raises(ValueError, match="native failed"):
        manager.retry("native-failed")
    assert len(executor.starts) == 1


def test_changed_executor_identity_cannot_reuse_pending_approval(host):
    manager, executor = host
    pending = manager.start("aws", spec(), "identity")
    metadata = copy.deepcopy(manager.metadata("identity"))
    metadata["executor_config"]["region"] = "us-east-1"
    write_json(manager.directory("identity") / "run.json", metadata)
    with pytest.raises(CloudConfigurationError, match="configuration changed"):
        manager.approve("identity", pending["bundle_hash"], "reviewer")
    assert executor.starts == []


def test_databricks_workspace_is_resolved_and_persisted_before_review(host, monkeypatch):
    manager, executor = host
    plan = spec()
    plan["cloud"] = {"job_id": 42, "manifest_uri": "/Volumes/main/migrations/config/plan.json"}
    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace-a.example.test")
    pending = manager.start("databricks", plan, "workspace")
    assert pending["status"] == "AWAITING_APPROVAL"
    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace-b.example.test")
    restarted = hosting.CloudRunManager(manager.workspace)
    assert restarted.metadata("workspace")["executor_config"]["host"] == "https://workspace-a.example.test"
    restarted.approve("workspace", pending["bundle_hash"], "reviewer")
    assert len(executor.starts) == 1


def test_maintenance_barrier_sees_active_invocation_and_blocks_new_effects(host):
    manager, executor = host
    pending = manager.start("aws", spec(), "active")
    entered, release = Event(), Event()
    def wait_for_release():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("Test failed to release fake native submission")
    executor.on_start = wait_for_release
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.approve, "active", pending["bundle_hash"], "reviewer")
        try:
            assert entered.wait(5)
            assert set_maintenance(manager.workspace, True) == {"draining": True, "active_runs": 1}
            assert manager.status("active")["status"] == "RUNNING"
            with pytest.raises(Timeout):
                hosting.CloudRunManager(manager.workspace).retry("active")
            with pytest.raises(ValueError, match="paused for maintenance"):
                manager.start("aws", spec(), "blocked")
            assert len(executor.starts) == 1
        finally:
            release.set()
        assert future.result(timeout=5)["status"] == "WAITING_FOR_NATIVE_STATUS"
    assert maintenance_status(manager.workspace) == {"draining": True, "active_runs": 0}
    assert manager.status("active")["execution_id"] == "native-receipt-1"
    set_maintenance(manager.workspace, False)
    assert manager.retry("blocked")["status"] == "AWAITING_APPROVAL"


def test_crash_after_checkpoint_before_metadata_write_is_recoverable(host, monkeypatch):
    manager, executor = host
    pending = manager.start("aws", spec(), "crash")
    writer = hosting.write_json
    crashed = False
    def crash_after_checkpoint(path, value):
        nonlocal crashed
        if path.name == "run.json" and value.get("status") == "WAITING_FOR_NATIVE_STATUS" and not crashed:
            crashed = True
            raise KeyboardInterrupt("Simulated process loss after durable checkpoint")
        return writer(path, value)
    monkeypatch.setattr(hosting, "write_json", crash_after_checkpoint)
    with pytest.raises(KeyboardInterrupt):
        manager.approve("crash", pending["bundle_hash"], "reviewer")
    assert maintenance_status(manager.workspace)["active_runs"] == 1
    resumed = hosting.CloudRunManager(manager.workspace).retry("crash")
    assert resumed["status"] == "WAITING_FOR_NATIVE_STATUS"
    assert maintenance_status(manager.workspace)["active_runs"] == 0
    assert len(executor.starts) == 1


@pytest.mark.parametrize("run_id", ["../escape", "with/slash", "", "x" * 65])
def test_run_paths_cannot_escape_workspace(host, run_id):
    manager, _ = host
    with pytest.raises(ValueError, match="Invalid cloud run ID"):
        manager.directory(run_id)
