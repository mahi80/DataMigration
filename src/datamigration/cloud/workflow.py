"""LangGraph approvals and receipt tracking around native cloud execution.

Graph checkpoints do not make provider API effects exactly once. The adapters
retain stable operation keys and reconcile uncertain effects independently.
"""

from __future__ import annotations

import os
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import compile_plan, get_executor
from .base import CloudConfigurationError, digest, operation_token


class CloudState(TypedDict, total=False):
    spec: dict[str, Any]
    operation_id: str
    operation_binding: str
    provider: str
    executor_config_hash: str
    bundle: dict[str, Any]
    bundle_hash: str
    approval: dict[str, Any]
    execution_id: str
    native_result: dict[str, Any]
    status: str


def build_cloud_graph(checkpointer, provider: str, executor_config: dict[str, Any], *, executor=None):
    """Build a persistent graph; no cloud action happens during construction.

    Initial input: ``{"spec": spec_dict, "operation_id": stable_unique_key}``.
    Approval resume: ``{"approve": True, "reviewer": "name", "bundle_hash": hash}``.
    Poll resume: ``{"action": "poll"}``.
    Rejected approvals end without submitting. SDK errors propagate; reuse the
    same checkpoint and operation key to reconcile/retry through the adapter.
    """
    if provider not in {"aws", "azure", "databricks"}:
        raise CloudConfigurationError("Unknown cloud provider")
    config = dict(executor_config)
    if provider == "databricks" and executor is None:
        config["host"] = config.get("host") or os.environ.get("DATABRICKS_HOST")
        if not config["host"]:
            raise CloudConfigurationError("Bind an explicit Databricks host (or DATABRICKS_HOST) before starting the graph")
    bound_config_hash = digest(config)

    def check_bindings(state: CloudState, *, require_bundle: bool = True) -> None:
        if state.get("provider", provider) != provider:
            raise CloudConfigurationError("Cloud provider changed after graph creation")
        if state.get("executor_config_hash", bound_config_hash) != bound_config_hash:
            raise CloudConfigurationError("Executor configuration changed; create a new reviewed run")
        token = operation_token(state["operation_id"])
        if state.get("operation_binding", token) != token:
            raise CloudConfigurationError("Operation ID changed after planning")
        if require_bundle and digest(state["bundle"]) != state["bundle_hash"]:
            raise CloudConfigurationError("Compiled bundle changed after review was requested")

    def native_executor(state: CloudState):
        if executor is not None:
            return executor
        effective = dict(config)
        # Status calls must use the same explicit ADF scope as submission.
        if provider == "azure":
            for key in ("subscription_id", "resource_group", "factory_name"):
                value = state["bundle"]["submission"][key]
                if key in effective and effective[key] != value:
                    raise CloudConfigurationError(f"Executor {key} conflicts with the approved bundle")
                effective[key] = value
        return get_executor(provider, effective)

    def compile_node(state: CloudState) -> dict:
        check_bindings(state, require_bundle=False)
        bundle = compile_plan(provider, state["spec"])
        return {"bundle": bundle, "bundle_hash": digest(bundle), "provider": provider,
                "executor_config_hash": bound_config_hash,
                "operation_binding": operation_token(state["operation_id"]),
                "status": "AWAITING_APPROVAL"}

    def approval_node(state: CloudState) -> dict:
        check_bindings(state)
        answer = interrupt({"kind": "cloud_approval", "provider": provider,
                            "operation_id": state["operation_id"], "bundle_hash": state["bundle_hash"],
                            "preconditions": state["bundle"]["preconditions"],
                            "message": "Approval authorizes the reviewed cloud job to write data and incur cloud cost."})
        if not isinstance(answer, dict) or type(answer.get("approve")) is not bool:
            raise CloudConfigurationError("Approval requires an explicit boolean approve field")
        if answer["approve"] is False:
            return {"status": "REJECTED"}
        if answer.get("bundle_hash") != state["bundle_hash"]:
            raise CloudConfigurationError("Approval must match the exact compiled bundle hash")
        reviewer = answer.get("reviewer")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise CloudConfigurationError("A reviewer identity is required")
        return {"approval": {"reviewer": reviewer.strip(), "bundle_hash": state["bundle_hash"]},
                "status": "APPROVED"}

    def submit_node(state: CloudState) -> dict:
        check_bindings(state)
        if state.get("approval", {}).get("bundle_hash") != state["bundle_hash"]:
            raise CloudConfigurationError("No approval exists for this bundle")
        execution_id = native_executor(state).start(state["bundle"], state["operation_id"])
        return {"execution_id": execution_id, "status": "WAITING_FOR_NATIVE_STATUS"}

    def wait_node(state: CloudState) -> dict:
        check_bindings(state)
        signal = interrupt({"kind": "cloud_status", "provider": provider,
                            "execution_id": state["execution_id"], "action": "poll"})
        if not isinstance(signal, dict) or signal.get("action") != "poll":
            raise CloudConfigurationError("Resume native monitoring with action=poll")
        return {}

    def status_node(state: CloudState) -> dict:
        check_bindings(state)
        result = native_executor(state).status(state["execution_id"])
        if result.get("terminal") is not True:
            return {"native_result": result, "status": "WAITING_FOR_NATIVE_STATUS"}
        successful = (
            provider == "aws" and result.get("status") == "SUCCEEDED"
            or provider == "azure" and result.get("status") == "Succeeded"
            or provider == "databricks" and result.get("status") == "TERMINATED" and result.get("result") == "SUCCESS"
        )
        return {"native_result": result,
                "status": "EXECUTION_SUCCEEDED_REQUIRES_RECONCILIATION" if successful else "FAILED"}

    builder = StateGraph(CloudState)
    for name, node in (("compile", compile_node), ("approval", approval_node), ("submit", submit_node),
                       ("wait_native", wait_node), ("native_status", status_node)):
        builder.add_node(name, node)
    builder.add_edge(START, "compile")
    builder.add_edge("compile", "approval")
    builder.add_conditional_edges("approval", lambda s: "submit" if s["status"] == "APPROVED" else END,
                                  {"submit": "submit", END: END})
    builder.add_edge("submit", "wait_native")
    builder.add_edge("wait_native", "native_status")
    builder.add_conditional_edges("native_status",
                                  lambda s: "wait_native" if s["status"] == "WAITING_FOR_NATIVE_STATUS" else END,
                                  {"wait_native": "wait_native", END: END})
    return builder.compile(checkpointer=checkpointer)
