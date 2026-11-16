"""LangGraph lifecycle with hash-bound approval and restartable execution."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from filelock import FileLock
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from .advisor import advise
from .connectors.base import ConnectorError
from .console import utc_now
from .engine import build_plan, discover, execute_snapshot, validate_plan
from .ledger import Ledger
from .maintenance import submission_guard
from .spec import MigrationSpec, write_json


class MigrationState(TypedDict, total=False):
    run_id: str
    spec: dict[str, Any]
    phase: str
    plan_hash: str
    catalog_ref: str
    plan_ref: str
    advice_ref: str
    validation_ref: str
    approved: bool
    reviewer: str


def safe_error(exc: Exception, spec: dict | None = None) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(".".join(str(p) for p in item["loc"]) + ": " + item["msg"] for item in exc.errors(include_input=False, include_url=False))[:1500]
    if isinstance(exc, (ValueError, FileExistsError, FileNotFoundError, ConnectorError)):
        message = str(exc)
        if spec:
            import os
            for name in ("source", "target"):
                uri = os.environ.get(spec.get(name, {}).get("url_env", ""))
                if uri:
                    message = message.replace(uri, "[connection redacted]")
        message = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[credentials-redacted]@", message)
        return message[:1500]
    return f"{type(exc).__name__}: operation failed. Check connector prerequisites and run state; raw service errors are not exposed."


class RunManager:
    def __init__(self, workspace: str | Path = ".migration"):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)

    def run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
            raise ValueError("run_id must contain 1-64 ASCII letters, digits, underscores, or hyphens")
        return self.workspace / "runs" / run_id

    @contextmanager
    def graph(self):
        conn = sqlite3.connect(self.workspace / "checkpoints.sqlite", check_same_thread=False, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield self._build_graph(SqliteSaver(conn))
        finally:
            conn.close()

    def _build_graph(self, saver):
        def discover_node(state):
            spec = MigrationSpec.model_validate(state["spec"])
            path = self.run_dir(state["run_id"]) / "catalog.json"
            write_json(path, discover(spec))
            return {"phase": "DISCOVERED", "catalog_ref": str(path)}

        def context_node(state):
            spec = MigrationSpec.model_validate(state["spec"])
            path = self.run_dir(state["run_id"]) / "advice.json"
            catalog = json.loads(Path(state["catalog_ref"]).read_text())
            # Exclude native/query metadata and parameters from model input.
            model_catalog = {"source_kind": catalog["source_kind"], "raw_samples_included": False,
                "tables": [{key: table[key] for key in ("name", "columns", "primary_key", "profile") if key in table}
                           for table in catalog["tables"]]}
            write_json(path, advise(spec.advisor, model_catalog))
            return {"phase": "CONTEXT_REVIEW", "advice_ref": str(path)}

        def plan_node(state):
            spec = MigrationSpec.model_validate(state["spec"])
            plan = build_plan(spec, json.loads(Path(state["catalog_ref"]).read_text()))
            path = self.run_dir(state["run_id"]) / "plan.json"
            write_json(path, plan)
            return {"phase": "AWAITING_APPROVAL", "plan_ref": str(path), "plan_hash": plan["plan_hash"]}

        def approval_node(state):
            decision = interrupt({
                "action": "review_migration_plan", "run_id": state["run_id"],
                "plan_hash": state["plan_hash"], "plan_ref": state["plan_ref"],
                "advice_ref": state["advice_ref"], "target_writes": True,
            })
            if not isinstance(decision, dict) or type(decision.get("approved")) is not bool:
                raise ValueError("Approval response requires an explicit boolean approved field")
            if decision.get("plan_hash") != state["plan_hash"]:
                raise ValueError("Approval does not match the exact plan hash")
            reviewer = decision.get("reviewer", "")
            if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 200:
                raise ValueError("A reviewer identity of 1-200 characters is required")
            if decision["approved"]:
                validate_plan(json.loads(Path(state["plan_ref"]).read_text()))
                ledger = Ledger(self.workspace / "ledger.sqlite")
                try:
                    ledger.approve(state["run_id"], state["plan_hash"], reviewer)
                finally:
                    ledger.close()
            return {"approved": decision["approved"], "reviewer": reviewer,
                    "phase": "APPROVED" if decision["approved"] else "REJECTED"}

        def execute_node(state):
            ledger = Ledger(self.workspace / "ledger.sqlite")
            try:
                execute_snapshot(json.loads(Path(state["plan_ref"]).read_text()), state["run_id"], self.run_dir(state["run_id"]), ledger)
            finally:
                ledger.close()
            return {"phase": "VALIDATED", "validation_ref": str(self.run_dir(state["run_id"]) / "validation.json")}

        def finish_node(state):
            return {"phase": "COMPLETED"}

        graph = StateGraph(MigrationState)
        graph.add_node("discover", discover_node)
        graph.add_node("semantic_advisor", context_node)
        graph.add_node("plan", plan_node)
        graph.add_node("approval", approval_node)
        graph.add_node("execute_and_reconcile", execute_node)
        graph.add_node("complete", finish_node)
        graph.add_edge(START, "discover")
        graph.add_edge("discover", "semantic_advisor")
        graph.add_edge("semantic_advisor", "plan")
        graph.add_edge("plan", "approval")
        graph.add_conditional_edges("approval", lambda s: "execute" if s["approved"] else "reject", {"execute": "execute_and_reconcile", "reject": END})
        graph.add_edge("execute_and_reconcile", "complete")
        graph.add_edge("complete", END)
        return graph.compile(checkpointer=saver)

    def _invoke(self, run_id: str, value) -> dict:
        meta_path = self.run_dir(run_id) / "run.json"
        metadata = json.loads(meta_path.read_text())
        metadata.pop("error", None)
        metadata["status"] = "RUNNING"
        metadata["updated_at"] = utc_now()
        with submission_guard(self.workspace):
            write_json(meta_path, metadata)
        try:
            with self.graph() as graph:
                state = graph.invoke(value, {"configurable": {"thread_id": run_id}}, durability="sync")
                metadata["status"] = state.get("phase", "UNKNOWN")
                metadata["plan_hash"] = state.get("plan_hash")
        except Exception as exc:
            metadata["status"] = "FAILED"
            metadata["error"] = safe_error(exc, metadata.get("spec"))
            metadata["updated_at"] = utc_now()
            write_json(meta_path, metadata)
            raise
        metadata["updated_at"] = utc_now()
        write_json(meta_path, metadata)
        return self.status(run_id)

    def start(self, spec: MigrationSpec, run_id: str | None = None) -> dict:
        run_id = run_id or str(uuid4())
        directory = self.run_dir(run_id)
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(str(directory) + ".lock", timeout=0):
            directory.mkdir(mode=0o700)
            created_at = utc_now()
            write_json(directory / "run.json", {"run_id": run_id, "status": "CREATED", "spec": spec.model_dump(mode="json"), "created_at": created_at, "updated_at": created_at})
            return self._invoke(run_id, {"run_id": run_id, "spec": spec.model_dump(mode="json"), "phase": "CREATED"})

    def decide(self, run_id: str, *, approved: bool, plan_hash: str, reviewer: str) -> dict:
        if type(approved) is not bool or not reviewer.strip() or len(reviewer) > 200:
            raise ValueError("An explicit approval decision and a reviewer identity of 1-200 characters are required")
        directory = self.run_dir(run_id)
        with FileLock(str(directory) + ".lock", timeout=0):
            state = self.status(run_id)
            if state["status"] != "AWAITING_APPROVAL":
                raise ValueError("Run is not awaiting approval")
            if state.get("plan_hash") != plan_hash:
                raise ValueError("Approval does not match the exact plan hash")
            return self._invoke(run_id, Command(resume={"approved": approved, "plan_hash": plan_hash, "reviewer": reviewer}))

    def retry(self, run_id: str) -> dict:
        directory = self.run_dir(run_id)
        with FileLock(str(directory) + ".lock", timeout=0):
            status = self.status(run_id)
            if status["status"] not in {"FAILED", "RUNNING", "CREATED"}:
                raise ValueError("Only failed or interrupted execution can be retried")
            with self.graph() as graph:
                snapshot = graph.get_state({"configurable": {"thread_id": run_id}})
                if not snapshot.values:
                    metadata = json.loads((directory / "run.json").read_text())
                    initial = {"run_id": run_id, "spec": metadata["spec"], "phase": "CREATED"}
                else:
                    initial = None
            return self._invoke(run_id, initial)

    def status(self, run_id: str) -> dict:
        directory = self.run_dir(run_id)
        metadata = json.loads((directory / "run.json").read_text())
        result = {k: v for k, v in metadata.items() if k != "spec"}
        result["artifact_directory"] = str(directory)
        with self.graph() as graph:
            snapshot = graph.get_state({"configurable": {"thread_id": run_id}})
            result["next_nodes"] = list(snapshot.next)
        validation = directory / "validation.json"
        if validation.exists():
            result["validation"] = json.loads(validation.read_text())
        return result
